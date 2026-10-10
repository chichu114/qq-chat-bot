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


PNG = b"\x89PNG\r\n\x1a\n" + b"png-data"


def protocol(payload, body=b""):
    header = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return len(header).to_bytes(4, "big") + header + body


def request_payload(
    url="https://media.example/image.png",
    allowed_hosts=("media.example",),
    max_redirects=3,
    byte_limit=1024,
):
    return json.dumps(
        {
            "url": url,
            "allowed_hosts": list(allowed_hosts),
            "max_redirects": max_redirects,
            "byte_limit": byte_limit,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def decoded_protocol(value):
    header_size = int.from_bytes(value[:4], "big")
    header = json.loads(value[4 : 4 + header_size].decode("utf-8"))
    return header, value[4 + header_size :]


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, payload, timeout, output_limit):
        self.calls.append((command, payload, timeout, output_limit))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = headers or {}
        self.read_sizes = []
        self.closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self._offset >= len(self._body):
            return b""
        end = len(self._body) if size < 0 else self._offset + size
        chunk = self._body[self._offset : end]
        self._offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, pinned_peer):
        self.calls.append((url, pinned_peer))
        if not self.responses:
            raise AssertionError("unexpected network open")
        return self.responses.pop(0)


class RemoteDownloadSandboxTests(unittest.TestCase):
    @staticmethod
    def _module():
        from qq_openclaw_bridge.application import remote_download_sandbox

        return remote_download_sandbox

    def _result(self, returncode=0, stdout=None):
        module = self._module()
        return module._WorkerResult(returncode=returncode, stdout=stdout or b"")

    def _run_worker(
        self,
        payload,
        resolver,
        opener,
        limit_setter=lambda maximum: None,
    ):
        module = self._module()
        output = io.BytesIO()
        result = module._worker_main(
            input_stream=io.BytesIO(payload),
            output_stream=output,
            limit_setter=limit_setter,
            resolver=resolver,
            opener=opener,
        )
        return result, output.getvalue()

    def test_parent_uses_fixed_command_strict_bounded_json_and_exact_success_protocol(self):
        module = self._module()
        stdout = protocol(
            {"ok": True, "content_type": "image/png", "byte_size": len(PNG)},
            PNG,
        )
        runner = FakeRunner([self._result(stdout=stdout)])
        sandbox = module.RemoteDownloadSandbox(runner=runner)

        result = sandbox.fetch(
            "https://media.example/image.png",
            ("media.example",),
            2,
            2048,
            timeout_seconds=7.5,
        )

        self.assertEqual(
            result,
            module.RemoteDownloadResult(body=PNG, content_type="image/png"),
        )
        command, payload, timeout, output_limit = runner.calls[0]
        self.assertEqual(
            command,
            (
                sys.executable,
                str(Path(module.__file__).resolve()),
                "--worker",
            ),
        )
        self.assertTrue(all(type(item) is str for item in command))
        self.assertEqual(timeout, 7.5)
        self.assertEqual(output_limit, 2048 + module.MAX_HEADER_BYTES)
        self.assertLessEqual(len(payload), module.MAX_REQUEST_BYTES)
        self.assertEqual(
            json.loads(payload.decode("utf-8")),
            {
                "url": "https://media.example/image.png",
                "allowed_hosts": ["media.example"],
                "max_redirects": 2,
                "byte_limit": 2048,
            },
        )

        calls_before = len(runner.calls)
        with self.assertRaisesRegex(
            module.MediaRejected, "remote download sandbox rejected media"
        ):
            sandbox.fetch(
                "https://media.example/image.png",
                tuple("host-%05d.example" % index for index in range(5000)),
                2,
                2048,
            )
        self.assertEqual(len(runner.calls), calls_before)

    def test_default_runner_uses_anonymous_output_and_kills_then_reaps_timeout(self):
        module = self._module()

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
        command = (sys.executable, "/fixed/remote_download_sandbox.py", "--worker")
        with mock.patch.object(module.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaises(subprocess.TimeoutExpired):
                module._run_worker(command, b"{}", 0.01, 1024)

        self.assertTrue(process.killed)
        self.assertEqual(process.communicate_calls, [(b"{}", 0.01), (None, None)])
        kwargs = popen.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertIs(kwargs["stdin"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(hasattr(kwargs["stdout"], "fileno"))

    def test_parent_rejects_exit_signal_oversize_and_malformed_protocols(self):
        module = self._module()
        limit = 32
        malformed = (
            self._result(returncode=1, stdout=protocol({"ok": False, "error": "rejected"})),
            self._result(returncode=-9, stdout=protocol({"ok": False, "error": "rejected"})),
            self._result(stdout=b"x" * (limit + module.MAX_HEADER_BYTES + 1)),
            self._result(stdout=b"\x00\x00\x00\x10{}"),
            self._result(
                stdout=protocol(
                    {
                        "ok": True,
                        "content_type": "image/png",
                        "byte_size": 1,
                        "extra": True,
                    },
                    b"x",
                )
            ),
            self._result(
                stdout=protocol(
                    {"ok": True, "content_type": "image/png", "byte_size": 2},
                    b"x",
                )
            ),
            self._result(
                stdout=protocol(
                    {"ok": True, "content_type": "image/png", "byte_size": 1},
                    b"xy",
                )
            ),
            self._result(
                stdout=protocol(
                    {"ok": 1, "content_type": "image/png", "byte_size": 1},
                    b"x",
                )
            ),
            self._result(stdout=protocol({"ok": False, "error": "details"})),
            self._result(
                stdout=(
                    b"\x00\x00\x00\x1f"
                    b'{"ok":true,"ok":false,"x":1}'
                )
            ),
        )
        for index, worker_result in enumerate(malformed):
            with self.subTest(index=index):
                sandbox = module.RemoteDownloadSandbox(
                    runner=FakeRunner([worker_result])
                )
                with self.assertRaisesRegex(
                    module.MediaRejected,
                    "^remote download sandbox rejected media$",
                ):
                    sandbox.fetch(
                        "https://media.example/image.png",
                        ("media.example",),
                        3,
                        limit,
                    )

    def test_parent_requires_the_exact_empty_rejection_protocol(self):
        module = self._module()
        exact = {"ok": False, "error": "rejected"}

        self.assertTrue(
            module.RemoteDownloadSandbox._is_exact_rejection(exact, b"")
        )
        for header, body in (
            ({"ok": False, "error": "details"}, b""),
            ({"ok": False}, b""),
            ({"ok": False, "error": "rejected", "extra": True}, b""),
            (exact, b"unexpected body"),
        ):
            with self.subTest(header=header, body=body):
                self.assertFalse(
                    module.RemoteDownloadSandbox._is_exact_rejection(header, body)
                )

    def test_worker_rejects_initial_and_redirect_hosts_before_dns_or_open(self):
        resolver_calls = []
        opener = FakeOpener([])

        def resolver(host):
            resolver_calls.append(host)
            return ("93.184.216.34",)

        result, stdout = self._run_worker(
            request_payload(url="https://blocked.example/image.png"),
            resolver,
            opener,
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            decoded_protocol(stdout),
            ({"ok": False, "error": "rejected"}, b""),
        )
        self.assertEqual(resolver_calls, [])
        self.assertEqual(opener.calls, [])

        redirect = FakeResponse(
            status=302,
            headers={"Location": "https://blocked.example/final.png"},
        )
        opener = FakeOpener([redirect])
        result, stdout = self._run_worker(
            request_payload(),
            resolver,
            opener,
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            decoded_protocol(stdout),
            ({"ok": False, "error": "rejected"}, b""),
        )
        self.assertEqual(resolver_calls, ["media.example"])
        self.assertEqual(
            opener.calls,
            [("https://media.example/image.png", "93.184.216.34")],
        )
        self.assertTrue(redirect.closed)

    def test_worker_rejects_private_dns_and_pins_peer_for_every_allowed_hop(self):
        private_opener = FakeOpener([])
        result, stdout = self._run_worker(
            request_payload(),
            lambda host: ("127.0.0.1",),
            private_opener,
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            decoded_protocol(stdout),
            ({"ok": False, "error": "rejected"}, b""),
        )
        self.assertEqual(private_opener.calls, [])

        redirect = FakeResponse(
            status=302,
            headers={"Location": "https://cdn.example/final.png"},
        )
        success = FakeResponse(
            PNG,
            headers={"Content-Type": "image/png"},
        )
        opener = FakeOpener([redirect, success])
        resolutions = {
            "media.example": ("93.184.216.34",),
            "cdn.example": ("142.250.72.14",),
        }
        result, stdout = self._run_worker(
            request_payload(allowed_hosts=("media.example", "cdn.example")),
            lambda host: resolutions[host],
            opener,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            decoded_protocol(stdout),
            (
                {
                    "ok": True,
                    "content_type": "image/png",
                    "byte_size": len(PNG),
                },
                PNG,
            ),
        )
        self.assertEqual(
            opener.calls,
            [
                ("https://media.example/image.png", "93.184.216.34"),
                ("https://cdn.example/final.png", "142.250.72.14"),
            ],
        )
        self.assertTrue(redirect.closed)
        self.assertTrue(success.closed)

    def test_default_transport_uses_pinned_tcp_peer_with_original_sni_and_host(self):
        module = self._module()
        raw_socket = object()

        class FakeContext:
            def __init__(self):
                self.calls = []
                self.verify_mode = module.ssl.CERT_REQUIRED
                self.check_hostname = True
                self.post_handshake_auth = None

            def wrap_socket(self, sock, server_hostname=None):
                self.calls.append((sock, server_hostname))
                return object()

        context = FakeContext()
        connection = module._PinnedHTTPSConnection(
            "files.example",
            "93.184.216.34",
            port=443,
            timeout=17,
            context=context,
        )
        with mock.patch.object(
            module.socket,
            "create_connection",
            return_value=raw_socket,
        ) as create_connection:
            connection.connect()
        create_connection.assert_called_once_with(
            ("93.184.216.34", 443), 17, None
        )
        self.assertEqual(context.calls, [(raw_socket, "files.example")])

        class FakeConnection:
            instances = []

            def __init__(self, host, peer, port, timeout, context):
                self.values = (host, peer, port, timeout, context)
                self.request_call = None
                self.response = FakeResponse(PNG)
                self.__class__.instances.append(self)

            def request(self, method, target, headers):
                self.request_call = (method, target, headers)

            def getresponse(self):
                return self.response

            def close(self):
                pass

        with mock.patch.object(module, "_PinnedHTTPSConnection", FakeConnection):
            response = module._default_opener(
                "https://files.example/path?q=1",
                "93.184.216.34",
            )
        instance = FakeConnection.instances[0]
        self.assertEqual(
            instance.values[:4],
            ("files.example", "93.184.216.34", 443, 30),
        )
        self.assertEqual(instance.request_call[0:2], ("GET", "/path?q=1"))
        self.assertEqual(instance.request_call[2]["Host"], "files.example")
        self.assertEqual(instance.request_call[2]["Accept-Encoding"], "identity")
        response.close()

    def test_worker_streams_in_64_kib_chunks_and_rejects_overflow(self):
        module = self._module()
        body = b"x" * (module.CHUNK_SIZE + 1)
        response = FakeResponse(body, headers={"Content-Type": "image/png"})
        result, stdout = self._run_worker(
            request_payload(byte_limit=module.CHUNK_SIZE),
            lambda host: ("93.184.216.34",),
            FakeOpener([response]),
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            decoded_protocol(stdout),
            ({"ok": False, "error": "rejected"}, b""),
        )
        self.assertEqual(response.read_sizes, [module.CHUNK_SIZE, 1])
        self.assertTrue(response.closed)
        self.assertNotIn(body, stdout)

    def test_worker_sets_dynamic_fsize_nofile_and_core_before_network(self):
        module = self._module()

        class FakeResource:
            RLIM_INFINITY = -1
            RLIMIT_FSIZE = 1
            RLIMIT_NOFILE = 2
            RLIMIT_CORE = 3

            def __init__(self):
                self.calls = []

            def getrlimit(self, kind):
                return self.RLIM_INFINITY, self.RLIM_INFINITY

            def setrlimit(self, kind, value):
                self.calls.append((kind, value))

        resource = FakeResource()
        maximum = 12345
        module._set_worker_limits(maximum, resource)
        self.assertEqual(
            resource.calls,
            [
                (resource.RLIMIT_FSIZE, (maximum, resource.RLIM_INFINITY)),
                (resource.RLIMIT_NOFILE, (module.WORKER_NOFILE, resource.RLIM_INFINITY)),
                (resource.RLIMIT_CORE, (0, resource.RLIM_INFINITY)),
            ],
        )

        for missing, failing in (
            ("RLIMIT_FSIZE", None),
            ("RLIMIT_NOFILE", None),
            ("RLIMIT_CORE", None),
            (None, "RLIMIT_FSIZE"),
            (None, "RLIMIT_NOFILE"),
            (None, "RLIMIT_CORE"),
        ):
            with self.subTest(missing=missing, failing=failing):
                attributes = {
                    "RLIM_INFINITY": -1,
                    "RLIMIT_FSIZE": 1,
                    "RLIMIT_NOFILE": 2,
                    "RLIMIT_CORE": 3,
                }
                if missing:
                    del attributes[missing]

                class FailingResource:
                    def getrlimit(self, kind):
                        return -1, -1

                    def setrlimit(self, kind, value):
                        if failing and kind == attributes[failing]:
                            raise OSError("setrlimit denied")

                for name, value in attributes.items():
                    setattr(FailingResource, name, value)
                events = []
                result, stdout = self._run_worker(
                    request_payload(),
                    lambda host: events.append(("resolver", host)),
                    lambda url, peer: events.append(("opener", url, peer)),
                    limit_setter=lambda maximum: module._set_worker_limits(
                        maximum,
                        FailingResource(),
                    ),
                )
                self.assertEqual(result, 0)
                self.assertEqual(events, [])
                self.assertEqual(
                    decoded_protocol(stdout),
                    ({"ok": False, "error": "rejected"}, b""),
                )

    def test_idna_trailing_separators_normalize_equally_and_invalid_labels_fail(self):
        module = self._module()
        variants = ("example.", "example\u3002", "example\uff0e", "example\uff61")
        self.assertEqual(
            {module._normalize_remote_host(value) for value in variants},
            {"example"},
        )
        for value in ("", ".", "bad..example", "example..", "*.example"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    module._normalize_remote_host(value)


if __name__ == "__main__":
    unittest.main()
