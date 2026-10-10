import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def make_pdf(text="Hello PDF"):
    content = b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(("%d 0 obj\n" % number).encode("ascii"))
        result.extend(value)
        result.extend(b"\nendobj\n")
    xref = len(result)
    result.extend(("xref\n0 %d\n" % (len(objects) + 1)).encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(("%010d 00000 n \n" % offset).encode("ascii"))
    result.extend(
        (
            "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref)
        ).encode("ascii")
    )
    return bytes(result)


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, snapshot, timeout, stdout_limit):
        self.calls.append((command, snapshot, timeout, stdout_limit))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class PdfSandboxTests(unittest.TestCase):
    @staticmethod
    def _result(returncode=0, payload=None, stdout=None):
        from qq_openclaw_bridge.application import pdf_sandbox

        if stdout is None:
            stdout = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return pdf_sandbox._WorkerResult(returncode=returncode, stdout=stdout)

    def test_parent_uses_fixed_absolute_no_shell_command_and_caps_extract_request(self):
        from qq_openclaw_bridge.application import pdf_sandbox

        runner = FakeRunner(
            [
                self._result(payload={"ok": True}),
                self._result(payload={"ok": True, "text": "bounded"}),
            ]
        )
        sandbox = pdf_sandbox.PdfSandbox(runner=runner)

        sandbox.validate(b"%PDF-fixed")
        text = sandbox.extract(b"%PDF-fixed", 999999)

        self.assertEqual(text, "bounded")
        validate_command = runner.calls[0][0]
        extract_command = runner.calls[1][0]
        self.assertIsInstance(validate_command, tuple)
        self.assertEqual(validate_command[0], sys.executable)
        self.assertEqual(validate_command[1], str(Path(pdf_sandbox.__file__).resolve()))
        self.assertEqual(validate_command[2:], ("--worker", "validate", "0"))
        self.assertEqual(
            extract_command[2:],
            ("--worker", "extract", str(pdf_sandbox.MAX_EXTRACT_CHARS)),
        )
        self.assertTrue(all(isinstance(item, str) for item in validate_command))
        self.assertEqual(runner.calls[0][1], b"%PDF-fixed")
        self.assertEqual(runner.calls[0][2], pdf_sandbox.WALL_TIMEOUT_SECONDS)
        self.assertEqual(runner.calls[0][3], pdf_sandbox.MAX_STDOUT_BYTES)

    def test_parent_rejects_timeout_nonzero_signal_invalid_and_oversize_protocols(self):
        from qq_openclaw_bridge.application import pdf_sandbox
        from qq_openclaw_bridge.domain.media import MediaRejected

        cases = (
            subprocess.TimeoutExpired(["worker"], 8),
            self._result(returncode=2, payload={"ok": True}),
            self._result(returncode=-9, payload={"ok": True}),
            self._result(stdout=b"not-json"),
            self._result(stdout=b'{"ok":true}{"ok":true}'),
            self._result(stdout=b"x" * (pdf_sandbox.MAX_STDOUT_BYTES + 1)),
        )
        for index, result in enumerate(cases):
            with self.subTest(index=index):
                sandbox = pdf_sandbox.PdfSandbox(runner=FakeRunner([result]))
                with self.assertRaisesRegex(MediaRejected, "PDF sandbox rejected"):
                    sandbox.validate(b"%PDF-test")

    def test_parent_strictly_validates_json_object_fields_and_types(self):
        from qq_openclaw_bridge.application import pdf_sandbox
        from qq_openclaw_bridge.domain.media import MediaRejected

        invalid_validate = (
            [],
            {},
            {"ok": 1},
            {"ok": True, "extra": None},
            {"ok": False},
            {"ok": False, "error": 7},
            {"ok": False, "error": "parse", "extra": True},
        )
        for payload in invalid_validate:
            with self.subTest(validate=payload):
                sandbox = pdf_sandbox.PdfSandbox(
                    runner=FakeRunner([self._result(payload=payload)])
                )
                with self.assertRaisesRegex(MediaRejected, "PDF sandbox rejected"):
                    sandbox.validate(b"%PDF-test")

        invalid_extract = (
            {"ok": True},
            {"ok": True, "text": 1},
            {"ok": True, "text": "ok", "extra": True},
            {"ok": True, "text": "xxxx"},
        )
        for payload in invalid_extract:
            with self.subTest(extract=payload):
                sandbox = pdf_sandbox.PdfSandbox(
                    runner=FakeRunner([self._result(payload=payload)])
                )
                with self.assertRaisesRegex(MediaRejected, "PDF sandbox rejected"):
                    sandbox.extract(b"%PDF-test", 2)

    def test_parent_rejects_input_over_20_mib_before_invoking_runner(self):
        from qq_openclaw_bridge.application import pdf_sandbox
        from qq_openclaw_bridge.domain.media import MediaRejected

        runner = FakeRunner([])
        sandbox = pdf_sandbox.PdfSandbox(runner=runner)

        with self.assertRaisesRegex(MediaRejected, "20 MiB"):
            sandbox.validate(b"x" * (pdf_sandbox.MAX_INPUT_BYTES + 1))
        self.assertEqual(runner.calls, [])

    def test_default_runner_kills_and_reaps_timed_out_worker(self):
        from qq_openclaw_bridge.application import pdf_sandbox

        class Process:
            def __init__(self):
                self.returncode = -9
                self.communicate_calls = []
                self.killed = False

            def communicate(self, input=None, timeout=None):
                self.communicate_calls.append((input, timeout))
                if timeout is not None:
                    raise subprocess.TimeoutExpired(["worker"], timeout)
                return None, None

            def kill(self):
                self.killed = True

        process = Process()
        with mock.patch.object(
            pdf_sandbox.subprocess, "Popen", return_value=process
        ) as popen:
            with self.assertRaises(subprocess.TimeoutExpired):
                pdf_sandbox._run_worker(
                    (sys.executable, "/fixed/worker.py", "--worker", "validate", "0"),
                    b"%PDF-test",
                    0.01,
                    pdf_sandbox.MAX_STDOUT_BYTES,
                )

        self.assertTrue(process.killed)
        self.assertEqual(len(process.communicate_calls), 2)
        kwargs = popen.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdin"], subprocess.PIPE)

    def test_worker_sets_cpu_memory_and_auxiliary_limits_before_loading_pypdf(self):
        from qq_openclaw_bridge.application import pdf_sandbox

        class FakeResource:
            RLIM_INFINITY = -1
            RLIMIT_CPU = 1
            RLIMIT_AS = 2
            RLIMIT_FSIZE = 3
            RLIMIT_NOFILE = 4
            RLIMIT_CORE = 5

            def __init__(self):
                self.calls = []

            def getrlimit(self, kind):
                return self.RLIM_INFINITY, self.RLIM_INFINITY

            def setrlimit(self, kind, value):
                self.calls.append((kind, value))

        fake_resource = FakeResource()
        pdf_sandbox._set_worker_limits(fake_resource)
        self.assertEqual(
            {item[0] for item in fake_resource.calls},
            {
                fake_resource.RLIMIT_CPU,
                fake_resource.RLIMIT_AS,
                fake_resource.RLIMIT_FSIZE,
                fake_resource.RLIMIT_NOFILE,
                fake_resource.RLIMIT_CORE,
            },
        )

        events = []

        class Reader:
            def __init__(self, source, strict=False):
                self.pages = []
                self.strict = strict

        output = io.StringIO()
        result = pdf_sandbox._worker_main(
            ("validate", "0"),
            input_stream=io.BytesIO(b"%PDF-test"),
            output_stream=output,
            limit_setter=lambda: events.append("limits"),
            pdf_loader=lambda: (events.append("pypdf"), Reader)[1],
        )
        self.assertEqual(result, 0)
        self.assertEqual(events, ["limits", "pypdf"])
        self.assertEqual(json.loads(output.getvalue()), {"ok": True})

    def test_worker_fails_closed_when_required_limits_cannot_be_set(self):
        from qq_openclaw_bridge.application import pdf_sandbox

        events = []
        output = io.StringIO()

        def reject_limits():
            events.append("limits")
            raise OSError("setrlimit denied")

        result = pdf_sandbox._worker_main(
            ("validate", "0"),
            input_stream=io.BytesIO(b"%PDF-test"),
            output_stream=output,
            limit_setter=reject_limits,
            pdf_loader=lambda: events.append("pypdf"),
        )

        self.assertEqual(result, 0)
        self.assertEqual(events, ["limits"])
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "error": "rejected"})

    def test_worker_requires_file_size_and_nofile_limits_before_loading_pypdf(self):
        from qq_openclaw_bridge.application import pdf_sandbox

        for missing, failing in (
            ("RLIMIT_FSIZE", None),
            ("RLIMIT_NOFILE", None),
            (None, "RLIMIT_FSIZE"),
            (None, "RLIMIT_NOFILE"),
        ):
            with self.subTest(missing=missing, failing=failing):
                attributes = {
                    "RLIM_INFINITY": -1,
                    "RLIMIT_CPU": 1,
                    "RLIMIT_AS": 2,
                    "RLIMIT_FSIZE": 3,
                    "RLIMIT_NOFILE": 4,
                    "RLIMIT_CORE": 5,
                }
                if missing is not None:
                    del attributes[missing]

                class FakeResource:
                    def getrlimit(self, kind):
                        return -1, -1

                    def setrlimit(self, kind, value):
                        if failing is not None and kind == attributes[failing]:
                            raise OSError("setrlimit denied")

                for name, value in attributes.items():
                    setattr(FakeResource, name, value)
                events = []
                output = io.StringIO()
                result = pdf_sandbox._worker_main(
                    ("validate", "0"),
                    input_stream=io.BytesIO(b"%PDF-test"),
                    output_stream=output,
                    limit_setter=lambda: pdf_sandbox._set_worker_limits(FakeResource()),
                    pdf_loader=lambda: events.append("pypdf"),
                )

                self.assertEqual(result, 0)
                self.assertEqual(events, [])
                self.assertEqual(
                    json.loads(output.getvalue()), {"ok": False, "error": "rejected"}
                )

    def test_small_valid_pdf_uses_real_worker_and_failure_does_not_poison_next_call(self):
        from qq_openclaw_bridge.application.pdf_sandbox import PdfSandbox
        from qq_openclaw_bridge.domain.media import MediaRejected

        sandbox = PdfSandbox()
        body = make_pdf("Real Worker")

        sandbox.validate(body)
        self.assertIn("Real Worker", sandbox.extract(body, 100))
        with self.assertRaisesRegex(MediaRejected, "PDF sandbox rejected"):
            sandbox.validate(b"%PDF-1.7\nnot-a-real-pdf")
        self.assertIn("Real Worker", sandbox.extract(body, 100))

    def test_parent_modules_do_not_import_pypdf(self):
        code = (
            "import sys; "
            "sys.path.insert(0, %r); "
            "import qq_openclaw_bridge.application.pdf_sandbox; "
            "import qq_openclaw_bridge.application.media_ingestion; "
            "import qq_openclaw_bridge.application.documents; "
            "assert not any(n == 'pypdf' or n.startswith('pypdf.') for n in sys.modules)"
            % str(ROOT / "src")
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
