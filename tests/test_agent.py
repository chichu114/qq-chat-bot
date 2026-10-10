import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "raw_body": request.data,
                "body": json.loads(request.data.decode("utf-8")),
            }
        )
        return _FakeHTTPResponse(self.payload)


_CLASSIFIER_TEXT = (
    '{"level":"L2","confidence":0.8,"task_kind":"analysis",'
    '"reasons":["bounded"],"brainstorm_required":false}'
)


def _responses_envelope(text=_CLASSIFIER_TEXT, model="gpt-5.4-nano-2026-08-01"):
    return {
        "object": "response",
        "status": "completed",
        "model": model,
        "output": [{
            "id": "msg_123",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": text,
            }],
        }],
        "usage": {
            "input_tokens": 123,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 45,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 168,
        },
    }


class _FakeCurlProcess:
    def __init__(self, payload=None, *, raw_stdout=None, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stdout = (
            json.dumps(payload).encode("utf-8")
            if raw_stdout is None
            else raw_stdout
        )
        self.stderr = stderr
        self.communicate_calls = []
        self.terminated = False
        self.killed = False

    def communicate(self, input=None, timeout=None):
        self.communicate_calls.append({"input": input, "timeout": timeout})
        return self.stdout, self.stderr

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class _FakeCurlFactory:
    def __init__(self, process):
        self.process = process
        self.calls = []

    def __call__(self, command, **kwargs):
        header_fd = kwargs["pass_fds"][0]
        self.calls.append({
            "command": list(command),
            "kwargs": dict(kwargs),
            "headers": os.read(header_fd, 8192).decode("utf-8"),
        })
        return self.process


def _capture_error(errors, operation):
    try:
        operation()
    except Exception as error:
        errors.append(error)


class AgentAdapterTests(unittest.TestCase):
    def test_responses_adapter_accepts_openclaw_apikey_auth_mode(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "key"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano",
                routing_agent_id="routing",
                auth_path=auth_path,
            )

            self.assertEqual(adapter._read_api_key(), "key")

    def test_responses_adapter_sends_strict_bounded_request_and_returns_metadata(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.domain.routing import TASK_KINDS
        from qq_openclaw_bridge.models import AgentRequest

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "test-key"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            process = _FakeCurlProcess(_responses_envelope())
            transport = _FakeCurlFactory(process)
            with mock.patch.dict(os.environ, {
                "HTTPS_PROXY": "http://proxy.test:8443",
                "LEAK_TEST": "prefix-test-key-suffix",
                "OPENAI_API_KEY": "environment-key",
            }, clear=True):
                response = OpenAIResponsesAgentAdapter(
                    model="openai/gpt-5.4-nano",
                    routing_agent_id="routing",
                    timeout_seconds=12,
                    popen_factory=transport,
                    auth_path=auth_path,
                    cancellable=True,
                ).reply(AgentRequest(1, "message-1", "classify this", 1.0))

        self.assertEqual(response.text, _CLASSIFIER_TEXT)
        self.assertEqual(response.provider, "openai")
        self.assertEqual(response.model, "gpt-5.4-nano-2026-08-01")
        self.assertEqual((response.input_tokens, response.output_tokens), (123, 45))
        self.assertIsInstance(response.duration_ms, int)
        self.assertGreaterEqual(response.duration_ms, 0)
        command = transport.calls[0]["command"]
        self.assertEqual(command[0], "/usr/bin/curl")
        self.assertEqual(command[1], "--disable")
        self.assertEqual(command[command.index("--proto") + 1], "=https")
        self.assertEqual(command[-1], "https://api.openai.com/v1/responses")
        self.assertEqual(command[command.index("--max-time") + 1], "12")
        self.assertEqual(command[command.index("--connect-timeout") + 1], "12")
        self.assertIn("--max-filesize", command)
        self.assertEqual(command[command.index("--data-binary") + 1], "@-")
        header_argument = command[command.index("--header") + 1]
        self.assertRegex(header_argument, r"^@/dev/fd/\d+$")
        self.assertNotIn("test-key", " ".join(command))
        process_env = transport.calls[0]["kwargs"]["env"]
        self.assertEqual(process_env["HTTPS_PROXY"], "http://proxy.test:8443")
        self.assertNotIn("test-key", json.dumps(process_env))
        self.assertNotIn("OPENAI_API_KEY", process_env)
        self.assertEqual(
            transport.calls[0]["headers"],
            "Authorization: Bearer test-key\nContent-Type: application/json\n",
        )
        self.assertEqual(process.communicate_calls[0]["timeout"], 12)
        body = json.loads(process.communicate_calls[0]["input"].decode("utf-8"))
        self.assertEqual(body["model"], "gpt-5.4-nano")
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertLessEqual(body["max_output_tokens"], 512)
        response_format = body["text"]["format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["strict"])
        schema = response_format["schema"]
        self.assertEqual(set(schema["properties"]), {
            "level", "confidence", "task_kind", "reasons", "brainstorm_required",
        })
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(tuple(schema["properties"]["task_kind"]["enum"]), TASK_KINDS)

    def test_responses_adapter_passes_only_whitelisted_environment_to_curl(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "test-key"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            process = _FakeCurlProcess(_responses_envelope())
            transport = _FakeCurlFactory(process)
            environment = {
                "HTTPS_PROXY": "http://upper-https-proxy.test:8443",
                "https_proxy": "http://lower-https-proxy.test:8443",
                "HTTP_PROXY": "http://upper-http-proxy.test:8080",
                "http_proxy": "http://lower-http-proxy.test:8080",
                "ALL_PROXY": "socks5://upper-all-proxy.test:1080",
                "all_proxy": "socks5://lower-all-proxy.test:1080",
                "NO_PROXY": "localhost,.internal",
                "no_proxy": "127.0.0.1",
                "LANG": "zh_CN.UTF-8",
                "LC_CTYPE": "zh_CN.UTF-8",
                "SSL_CERT_FILE": "/tmp/untrusted-ca.pem",
                "CURL_CA_BUNDLE": "/tmp/untrusted-bundle.pem",
                "SSLKEYLOGFILE": "/tmp/tls-keys.log",
                "HOME": "/tmp/untrusted-home",
                "OPENAI_API_KEY": "environment-key",
                "UNRELATED_SETTING": "must-not-reach-curl",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                OpenAIResponsesAgentAdapter(
                    model="openai/gpt-5.4-nano",
                    routing_agent_id="routing",
                    popen_factory=transport,
                    auth_path=auth_path,
                ).reply(AgentRequest(1, "message-1", "classify this", 1.0))

        self.assertEqual(
            transport.calls[0]["kwargs"]["env"],
            {
                "HTTPS_PROXY": "http://upper-https-proxy.test:8443",
                "https_proxy": "http://lower-https-proxy.test:8443",
                "HTTP_PROXY": "http://upper-http-proxy.test:8080",
                "http_proxy": "http://lower-http-proxy.test:8080",
                "ALL_PROXY": "socks5://upper-all-proxy.test:1080",
                "all_proxy": "socks5://lower-all-proxy.test:1080",
                "NO_PROXY": "localhost,.internal",
                "no_proxy": "127.0.0.1",
                "LANG": "zh_CN.UTF-8",
                "LC_CTYPE": "zh_CN.UTF-8",
            },
        )

    def test_responses_adapter_rejects_non_openai_model_identifiers(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter

        for model in ("gpt-5.4-nano", "openai/", "openai/a/b", "other/gpt"):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "model"):
                OpenAIResponsesAgentAdapter(model=model, routing_agent_id="routing")

    def test_responses_adapter_rejects_missing_malformed_or_unsafe_auth_files(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano", routing_agent_id="routing",
                auth_path=auth_path,
            )
            with self.assertRaisesRegex(RuntimeError, "auth"):
                adapter.reply(AgentRequest(1, "m", "text", 1.0))

            cases = (
                ("not json", 0o600),
                (json.dumps({"auth_mode": "oauth", "OPENAI_API_KEY": "key"}), 0o600),
                (json.dumps({"auth_mode": "api_key"}), 0o600),
                (json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": ""}), 0o600),
                (json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "key"}), 0o640),
            )
            for content, mode in cases:
                auth_path.write_text(content, encoding="utf-8")
                auth_path.chmod(mode)
                with self.subTest(content=content, mode=mode), self.assertRaisesRegex(
                    RuntimeError, "auth"
                ):
                    adapter.reply(AgentRequest(1, "m", "text", 1.0))

            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "key"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            with mock.patch("qq_openclaw_bridge.agent.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(RuntimeError, "auth"):
                    adapter.reply(AgentRequest(1, "m", "text", 1.0))

    def test_responses_adapter_requires_auth_mode_exactly_0600(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "key"}),
                encoding="utf-8",
            )
            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano", routing_agent_id="routing",
                auth_path=auth_path,
            )
            for mode in (0o400, 0o640, 0o700):
                auth_path.chmod(mode)
                with self.subTest(mode=oct(mode)), self.assertRaisesRegex(
                    RuntimeError, "auth"
                ):
                    adapter._read_api_key()
            auth_path.chmod(0o600)
            self.assertEqual(adapter._read_api_key(), "key")

    def test_responses_adapter_rejects_unsafe_or_oversized_api_keys_before_spawn(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            popen_calls = []

            def fail_if_spawned(*args, **kwargs):
                popen_calls.append((args, kwargs))
                raise AssertionError("Popen must not be called")

            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano",
                routing_agent_id="routing",
                popen_factory=fail_if_spawned,
                auth_path=auth_path,
            )
            for api_key in (
                "prefix\rvalue",
                "prefix\nvalue",
                "prefix\x00value",
                "k" * 4097,
            ):
                auth_path.write_text(
                    json.dumps({
                        "auth_mode": "api_key",
                        "OPENAI_API_KEY": api_key,
                    }),
                    encoding="utf-8",
                )
                auth_path.chmod(0o600)
                with self.subTest(api_key_length=len(api_key)), self.assertRaises(
                    RuntimeError
                ):
                    adapter.reply(AgentRequest(1, "m", "text", 1.0))

        self.assertEqual(popen_calls, [])

    def test_responses_adapter_checks_pipe_capacity_and_complete_header_write(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "test-key"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            popen_calls = []

            def fail_if_spawned(*args, **kwargs):
                popen_calls.append((args, kwargs))
                raise AssertionError("Popen must not be called")

            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano",
                routing_agent_id="routing",
                popen_factory=fail_if_spawned,
                auth_path=auth_path,
            )
            request = AgentRequest(1, "m", "text", 1.0)
            with mock.patch(
                "qq_openclaw_bridge.agent.os.fpathconf", return_value=32
            ), self.assertRaises(RuntimeError):
                adapter.reply(request)
            with mock.patch(
                "qq_openclaw_bridge.agent.os.fpathconf", return_value=4096
            ), mock.patch(
                "qq_openclaw_bridge.agent.os.write", return_value=1
            ), self.assertRaises(RuntimeError):
                adapter.reply(request)

        self.assertEqual(popen_calls, [])

    def test_responses_adapter_raises_runtime_error_for_http_and_network_failures(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "never-leak"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            for returncode in (6, 22):
                process = _FakeCurlProcess(
                    raw_stdout=b'{"error":"body-secret"}',
                    returncode=returncode,
                    stderr=b"never-leak body-secret",
                )
                adapter = OpenAIResponsesAgentAdapter(
                    model="openai/gpt-5.4-nano", routing_agent_id="routing",
                    popen_factory=_FakeCurlFactory(process), auth_path=auth_path,
                    cancellable=True,
                )
                with self.subTest(returncode=returncode), self.assertRaises(
                    RuntimeError
                ) as context:
                    adapter.reply(AgentRequest(1, "m", "text", 1.0))
                self.assertNotIn("never-leak", str(context.exception))
                self.assertNotIn("body-secret", str(context.exception))

            def fail_to_spawn(*args, **kwargs):
                raise OSError("never-leak body-secret")

            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano", routing_agent_id="routing",
                popen_factory=fail_to_spawn, auth_path=auth_path,
                cancellable=True,
            )
            with self.assertRaises(RuntimeError) as context:
                adapter.reply(AgentRequest(1, "m", "text", 1.0))
            self.assertNotIn("never-leak", str(context.exception))
            self.assertNotIn("body-secret", str(context.exception))

            class FailedIOProcess(_FakeCurlProcess):
                def communicate(self, input=None, timeout=None):
                    raise OSError("never-leak body-secret")

            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano", routing_agent_id="routing",
                popen_factory=_FakeCurlFactory(
                    FailedIOProcess(_responses_envelope())
                ),
                auth_path=auth_path, cancellable=True,
            )
            with self.assertRaises(RuntimeError) as context:
                adapter.reply(AgentRequest(1, "m", "text", 1.0))
            self.assertNotIn("never-leak", str(context.exception))
            self.assertNotIn("body-secret", str(context.exception))

    def test_responses_adapter_reaps_curl_after_communicate_oserror(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        class FailedIOProcess(_FakeCurlProcess):
            def __init__(self):
                super().__init__(_responses_envelope())
                self.returncode = None

            def communicate(self, input=None, timeout=None):
                self.communicate_calls.append({"input": input, "timeout": timeout})
                if input is not None:
                    raise OSError("never-leak body-secret")
                self.returncode = -9
                return b"", b""

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "never-leak"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            process = FailedIOProcess()
            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano",
                routing_agent_id="routing",
                popen_factory=_FakeCurlFactory(process),
                auth_path=auth_path,
                cancellable=True,
            )

            with self.assertRaisesRegex(
                RuntimeError, "^OpenAI Responses transport failed$"
            ) as context:
                adapter.reply(AgentRequest(1, "m", "text", 1.0))

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.communicate_calls[0]["timeout"], 30.0)
        self.assertEqual(process.communicate_calls[1], {"input": None, "timeout": None})
        self.assertNotIn("never-leak", str(context.exception))
        self.assertNotIn("body-secret", str(context.exception))
        self.assertIsNone(context.exception.__cause__)

    def test_responses_adapter_rejects_invalid_json_and_protocol_envelopes(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        valid = _responses_envelope()
        invalid_envelopes = [
            b'{"body-secret":',
            {key: value for key, value in valid.items() if key != "object"},
            {**valid, "object": "list"},
            {**valid, "status": "in_progress"},
            {key: value for key, value in valid.items() if key != "output"},
            {**valid, "output": []},
            {**valid, "output": [{
                "type": "message", "status": "incomplete", "content": [{
                    "type": "output_text", "text": _CLASSIFIER_TEXT,
                }],
            }]},
            {**valid, "output": [{
                "type": "message", "status": "completed", "content": [{
                    "type": "refusal", "refusal": "no",
                }],
            }]},
            {**valid, "output": [{
                "type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": _CLASSIFIER_TEXT},
                    {"type": "output_text", "text": _CLASSIFIER_TEXT},
                ],
            }]},
            {**valid, "model": "m" * 129},
            {**valid, "usage": {"input_tokens": 1}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "never-leak"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            for envelope in invalid_envelopes:
                process = (
                    _FakeCurlProcess(raw_stdout=envelope)
                    if isinstance(envelope, bytes)
                    else _FakeCurlProcess(envelope)
                )
                adapter = OpenAIResponsesAgentAdapter(
                    model="openai/gpt-5.4-nano", routing_agent_id="routing",
                    popen_factory=_FakeCurlFactory(process), auth_path=auth_path,
                    cancellable=True,
                )
                with self.subTest(envelope=envelope), self.assertRaises(
                    ValueError
                ) as context:
                    adapter.reply(AgentRequest(1, "m", "text", 1.0))
                self.assertNotIn("never-leak", str(context.exception))
                self.assertNotIn("body-secret", str(context.exception))

    def test_responses_adapter_enforces_total_timeout_and_terminates_process(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        class TimeoutProcess(_FakeCurlProcess):
            def __init__(self):
                super().__init__(_responses_envelope())
                self.returncode = None

            def communicate(self, input=None, timeout=None):
                self.communicate_calls.append({"input": input, "timeout": timeout})
                if not self.killed:
                    raise subprocess.TimeoutExpired("curl", timeout)
                self.returncode = -9
                return b"", b"never-leak"

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True
                self.returncode = -9

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "never-leak"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            process = TimeoutProcess()
            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano", routing_agent_id="routing",
                timeout_seconds=3, popen_factory=_FakeCurlFactory(process),
                auth_path=auth_path, cancellable=True,
            )

            with self.assertRaises(TimeoutError) as context:
                adapter.reply(AgentRequest(1, "m", "text", 1.0))

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.communicate_calls[0]["timeout"], 3)
        self.assertNotIn("never-leak", str(context.exception))

    def test_responses_adapter_cancellation_only_stops_owned_processes(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        class BlockingProcess(_FakeCurlProcess):
            def __init__(self):
                super().__init__(_responses_envelope())
                self.returncode = None
                self.started = threading.Event()
                self.released = threading.Event()

            def communicate(self, input=None, timeout=None):
                self.communicate_calls.append({"input": input, "timeout": timeout})
                self.started.set()
                self.released.wait(1)
                return b"", b"cancelled"

            def terminate(self):
                self.terminated = True
                self.returncode = -15
                self.released.set()

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.released.set()

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "key"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            first_process = BlockingProcess()
            second_process = BlockingProcess()
            first = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano", routing_agent_id="routing",
                popen_factory=_FakeCurlFactory(first_process), auth_path=auth_path,
                cancellable=True,
            )
            second = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano", routing_agent_id="routing",
                popen_factory=_FakeCurlFactory(second_process), auth_path=auth_path,
                cancellable=True,
            )
            errors = []
            first_worker = threading.Thread(target=lambda: _capture_error(
                errors, lambda: first.reply(AgentRequest(1, "m1", "text", 1.0))
            ))
            second_worker = threading.Thread(target=lambda: _capture_error(
                errors, lambda: second.reply(AgentRequest(1, "m2", "text", 1.0))
            ))
            first_worker.start()
            second_worker.start()
            self.assertTrue(first_process.started.wait(0.2))
            self.assertTrue(second_process.started.wait(0.2))

            first.cancel_active()
            first_worker.join(0.5)

            self.assertTrue(first_process.terminated)
            self.assertFalse(second_process.terminated)
            self.assertTrue(second_worker.is_alive())
            second.force_cancel_active()
            second_worker.join(0.5)

        self.assertTrue(second_process.killed)
        self.assertFalse(first_worker.is_alive())
        self.assertFalse(second_worker.is_alive())

    def test_responses_adapter_does_not_chain_malformed_auth_contents(self):
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                '{"auth_mode":"api_key","OPENAI_API_KEY":"never-leak"',
                encoding="utf-8",
            )
            auth_path.chmod(0o600)
            adapter = OpenAIResponsesAgentAdapter(
                model="openai/gpt-5.4-nano", routing_agent_id="routing",
                auth_path=auth_path,
            )

            with self.assertRaises(RuntimeError) as context:
                adapter.reply(AgentRequest(1, "m", "text", 1.0))

        self.assertNotIn("never-leak", str(context.exception))
        self.assertIsNone(context.exception.__cause__)

    def test_cancellable_cli_adapter_terminates_only_its_owned_processes(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        started = threading.Event()
        released = threading.Event()
        created = []

        class Process:
            returncode = None

            def communicate(self, timeout):
                started.set()
                released.wait(1)
                self.returncode = -15
                return "", "terminated"

            def poll(self):
                return self.returncode

            def terminate(self):
                released.set()

        adapter = OpenClawCliAgentAdapter(
            "openclaw", cancellable=True,
            popen_factory=lambda *args, **kwargs: created.append(Process()) or created[-1],
        )
        errors = []
        worker = threading.Thread(
            target=lambda: _capture_error(
                errors,
                lambda: adapter.reply(AgentRequest(1, "m1", "classify", 1.0)),
            )
        )
        worker.start()
        self.assertTrue(started.wait(0.2))

        adapter.cancel_active()
        worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertTrue(released.is_set())
        self.assertEqual(len(created), 1)
        self.assertEqual(len(errors), 1)
    def test_agent_response_metadata_defaults_preserve_text_only_construction(self):
        from qq_openclaw_bridge.models import AgentResponse

        response = AgentResponse("legacy reply")

        self.assertEqual(response.text, "legacy reply")
        self.assertIsNone(response.provider)
        self.assertIsNone(response.model)
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.duration_ms)

    def test_agent_request_new_fields_keep_existing_positional_arguments_and_cli_order(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        request = AgentRequest(1, "m1", "原始文字", 1.0, "记忆上下文", "session")
        self.assertEqual(request.session_key, "session")
        command = OpenClawCliAgentAdapter("openclaw")._build_command(
            AgentRequest(
                user_id=1,
                message_id="m1",
                text="原始文字",
                timestamp=1.0,
                context="记忆上下文",
                media_context="媒体上下文",
                task_id="task-1",
            )
        )
        message = command[command.index("--message") + 1]
        self.assertEqual(
            message,
            "记忆上下文\n\n媒体上下文\n\n【用户原始消息】\n原始文字",
        )

    def test_http_adapter_adds_optional_context_fields_without_changing_plain_payload(self):
        from qq_openclaw_bridge.agent import HttpAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        transport = _FakeTransport({"reply": "ok"})
        HttpAgentAdapter("http://openclaw.local/reply", opener=transport).reply(
            AgentRequest(
                user_id=1,
                message_id="m1",
                text="文字",
                timestamp=1.0,
                context="记忆",
                media_context="媒体",
                task_id="task-1",
            )
        )
        self.assertEqual(
            transport.calls[0]["body"],
            {
                "user_id": 1,
                "message_id": "m1",
                "text": "文字",
                "timestamp": 1.0,
                "context": "记忆",
                "media_context": "媒体",
                "task_id": "task-1",
            },
        )

    def test_openclaw_cli_adapter_places_memory_context_before_original_message(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        adapter = OpenClawCliAgentAdapter("openclaw", runner=lambda *args, **kwargs: None)
        command = adapter._build_command(
            AgentRequest(
                user_id=1,
                message_id="m-context",
                text="今晚吃什么",
                timestamp=1.0,
                context="【已确认用户档案】\n- 用户不吃香菜",
            )
        )
        message = command[command.index("--message") + 1]

        self.assertIn("用户不吃香菜", message)
        self.assertTrue(message.endswith("【用户原始消息】\n今晚吃什么"))

    def test_echo_adapter_includes_incoming_text(self):
        from qq_openclaw_bridge.agent import EchoAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        response = EchoAgentAdapter().reply(
            AgentRequest(user_id=10001, message_id="m1", text="你好", timestamp=10.0)
        )

        self.assertEqual(response.text, "echo: 你好")

    def test_http_adapter_sends_json_and_reads_reply(self):
        from qq_openclaw_bridge.agent import HttpAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        transport = _FakeTransport({"reply": "来自 OpenClaw"})
        response = HttpAgentAdapter(
            "http://openclaw.local/reply", opener=transport
        ).reply(
            AgentRequest(user_id=10001, message_id="m1", text="你好", timestamp=10.0)
        )

        self.assertEqual(response.text, "来自 OpenClaw")
        self.assertEqual(transport.calls[0]["url"], "http://openclaw.local/reply")
        self.assertEqual(transport.calls[0]["timeout"], 30.0)
        self.assertEqual(
            transport.calls[0]["body"],
            {
                "user_id": 10001,
                "message_id": "m1",
                "text": "你好",
                "timestamp": 10.0,
            },
        )
        base_payload = {
            "user_id": 10001,
            "message_id": "m1",
            "text": "你好",
            "timestamp": 10.0,
        }
        self.assertEqual(
            transport.calls[0]["raw_body"],
            json.dumps(base_payload).encode("utf-8"),
        )

    def test_openclaw_cli_plain_text_command_matches_pre_media_baseline(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        command = OpenClawCliAgentAdapter(
            "scripts/openclaw.sh",
            timeout_seconds=45,
            agent_id="qq",
            session_prefix="agent:main:qq",
            use_gateway=True,
        )._build_command(
            AgentRequest(
                user_id=10001,
                message_id="m1",
                text="你好",
                timestamp=10.0,
            )
        )

        self.assertEqual(
            command,
            [
                "scripts/openclaw.sh",
                "agent",
                "--agent",
                "qq",
                "--json",
                "--message",
                "你好",
                "--session-key",
                "agent:main:qq-10001",
                "--timeout",
                "45",
            ],
        )

    def test_http_adapter_accepts_text_or_message_response_fields(self):
        from qq_openclaw_bridge.agent import HttpAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        request = AgentRequest(
            user_id=10001, message_id="m1", text="你好", timestamp=10.0
        )

        for field_name in ("text", "message"):
            with self.subTest(field_name=field_name):
                transport = _FakeTransport({field_name: "字段 %s" % field_name})
                response = HttpAgentAdapter(
                    "http://openclaw.local/reply", opener=transport
                ).reply(request)
                self.assertEqual(response.text, "字段 %s" % field_name)

    def test_factory_selects_echo_adapter(self):
        from qq_openclaw_bridge.agent import EchoAgentAdapter, create_agent_adapter
        from qq_openclaw_bridge.config import Config

        adapter = create_agent_adapter(
            Config(onebot_ws_url="ws://127.0.0.1:3001", allowed_qq_users=set())
        )

        self.assertIsInstance(adapter, EchoAgentAdapter)

    def test_factory_rejects_unknown_adapter(self):
        from qq_openclaw_bridge.agent import create_agent_adapter
        from qq_openclaw_bridge.config import Config

        with self.assertRaisesRegex(ValueError, "AGENT_ADAPTER"):
            create_agent_adapter(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users=set(),
                    agent_adapter="missing",
                )
            )

    def test_http_adapter_requires_endpoint(self):
        from qq_openclaw_bridge.agent import create_agent_adapter
        from qq_openclaw_bridge.config import Config

        with self.assertRaisesRegex(ValueError, "OPENCLAW_ENDPOINT"):
            create_agent_adapter(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users=set(),
                    agent_adapter="http",
                )
            )

    def test_openclaw_cli_adapter_runs_agent_command_and_reads_json_reply(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        calls = []

        def runner(command, **kwargs):
            calls.append({"command": command, "kwargs": kwargs})

            class Result:
                returncode = 0
                stdout = '{"reply":"来自 OpenClaw CLI"}\n'
                stderr = ""

            return Result()

        adapter = OpenClawCliAgentAdapter(
            command="scripts/openclaw.sh",
            timeout_seconds=45,
            model="openai/gpt-5.5",
            agent_id="qq",
            use_gateway=True,
            runner=runner,
        )

        response = adapter.reply(
            AgentRequest(user_id=10001, message_id="m1", text="你好", timestamp=10.0)
        )

        self.assertEqual(response.text, "来自 OpenClaw CLI")
        self.assertEqual(calls[0]["command"][0], "scripts/openclaw.sh")
        self.assertIn("agent", calls[0]["command"])
        self.assertNotIn("--local", calls[0]["command"])
        self.assertIn("--json", calls[0]["command"])
        self.assertIn("--message", calls[0]["command"])
        self.assertIn("你好", calls[0]["command"])
        self.assertIn("--session-key", calls[0]["command"])
        self.assertIn("--agent", calls[0]["command"])
        self.assertIn("qq", calls[0]["command"])
        self.assertIn("agent:main:qq-10001", calls[0]["command"])
        self.assertIn("--model", calls[0]["command"])
        self.assertIn("openai/gpt-5.5", calls[0]["command"])
        self.assertEqual(calls[0]["kwargs"]["timeout"], 45)

    def test_openclaw_cli_adapter_can_use_embedded_local_mode(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": '{"reply":"好"}\n', "stderr": ""},
            )()

        OpenClawCliAgentAdapter(
            command="scripts/openclaw.sh", use_gateway=False, runner=runner
        ).reply(
            AgentRequest(user_id=10001, message_id="m1", text="你好", timestamp=10.0)
        )

        self.assertIn("--local", calls[0])

    def test_openclaw_cli_adapter_can_omit_explicit_agent(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = '{"reply":"好"}\n'
                stderr = ""

            return Result()

        OpenClawCliAgentAdapter(
            command="scripts/openclaw.sh", agent_id=None, runner=runner
        ).reply(
            AgentRequest(user_id=10001, message_id="m1", text="你好", timestamp=10.0)
        )

        self.assertNotIn("--agent", calls[0])

    def test_openclaw_cli_adapter_uses_custom_session_prefix(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        calls = []

        def runner(command, **kwargs):
            calls.append(command)

            class Result:
                returncode = 0
                stdout = '{"reply":"好"}\n'
                stderr = ""

            return Result()

        OpenClawCliAgentAdapter(
            command="scripts/openclaw.sh",
            session_prefix="agent:main:qq-chichu-v2",
            runner=runner,
        ).reply(
            AgentRequest(user_id=10001, message_id="m1", text="你是谁", timestamp=10.0)
        )

        self.assertIn("agent:main:qq-chichu-v2-10001", calls[0])

    def test_openclaw_cli_adapter_uses_plain_stdout_when_json_is_absent(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = "纯文本回复\n"
                stderr = ""

            return Result()

        response = OpenClawCliAgentAdapter(
            command="scripts/openclaw.sh", runner=runner
        ).reply(
            AgentRequest(user_id=10001, message_id="m1", text="你好", timestamp=10.0)
        )

        self.assertEqual(response.text, "纯文本回复")

    def test_openclaw_cli_adapter_reads_payload_text_response(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = (
                    '{"payloads":[{"text":"你好","mediaUrl":null}],'
                    '"meta":{"durationMs":8484}}\n'
                )
                stderr = ""

            return Result()

        response = OpenClawCliAgentAdapter(
            command="scripts/openclaw.sh", runner=runner
        ).reply(
            AgentRequest(user_id=10001, message_id="m1", text="你好", timestamp=10.0)
        )

        self.assertEqual(response.text, "你好")

    def test_public_openclaw_response_extractor_accepts_payloads_envelope(self):
        from qq_openclaw_bridge.agent import extract_openclaw_response_from_stdout

        response = extract_openclaw_response_from_stdout(
            json.dumps(
                {
                    "payloads": [{"text": "classifier JSON"}],
                    "meta": {"durationMs": 1},
                }
            ),
            require_json=True,
        )

        self.assertEqual(response.text, "classifier JSON")

    def test_openclaw_cli_json_usage_is_preserved_from_selected_reply(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        def runner(command, **kwargs):
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": (
                        '{"payloads":[{"text":"metadata reply"}],'
                        '"meta":{"durationMs":8484,"agentMeta":{'
                        '"provider":"openai","model":"gpt-test",'
                        '"usage":{"input":120,"output":30,"total":150}}}}\n'
                    ),
                    "stderr": "",
                },
            )()

        response = OpenClawCliAgentAdapter("openclaw", runner=runner).reply(
            AgentRequest(10001, "m-usage", "hello", 10.0)
        )

        self.assertEqual(response.text, "metadata reply")
        self.assertEqual(response.provider, "openai")
        self.assertEqual(response.model, "gpt-test")
        self.assertEqual(response.input_tokens, 120)
        self.assertEqual(response.output_tokens, 30)
        self.assertEqual(response.duration_ms, 8484)

    def test_openclaw_cli_nested_reply_uses_only_nested_owner_metadata(self):
        from qq_openclaw_bridge.agent import _extract_response_from_stdout

        response = _extract_response_from_stdout(
            json.dumps(
                {
                    "result": {
                        "reply": "nested reply",
                        "meta": {
                            "durationMs": 222,
                            "agentMeta": {
                                "provider": "nested-provider",
                                "model": "nested/model-2",
                                "usage": {"input": 22, "output": 2},
                            },
                        },
                    },
                    "meta": {
                        "durationMs": 111,
                        "agentMeta": {
                            "provider": "outer-provider",
                            "model": "outer/model-1",
                            "usage": {"input": 11, "output": 1},
                        },
                    },
                }
            )
        )

        self.assertEqual(response.text, "nested reply")
        self.assertEqual(response.provider, "nested-provider")
        self.assertEqual(response.model, "nested/model-2")
        self.assertEqual(response.input_tokens, 22)
        self.assertEqual(response.output_tokens, 2)
        self.assertEqual(response.duration_ms, 222)

    def test_openclaw_cli_nested_candidates_do_not_donate_metadata(self):
        from qq_openclaw_bridge.agent import _extract_response_from_stdout

        response = _extract_response_from_stdout(
            json.dumps(
                {
                    "data": [
                        {
                            "reply": "old",
                            "meta": {
                                "durationMs": 10,
                                "agentMeta": {
                                    "provider": "old-provider",
                                    "model": "old-model",
                                    "usage": {"input": 10, "output": 10},
                                },
                            },
                        },
                        {"reply": "selected"},
                    ],
                    "meta": {
                        "durationMs": 997,
                        "agentMeta": {
                            "provider": "outer-provider",
                            "model": "outer-model",
                            "usage": {"input": 999, "output": 998},
                        },
                    },
                }
            )
        )

        self.assertEqual(response.text, "selected")
        self.assertIsNone(response.provider)
        self.assertIsNone(response.model)
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.duration_ms)

    def test_openclaw_cli_payloads_envelope_keeps_its_metadata(self):
        from qq_openclaw_bridge.agent import _extract_response_from_stdout

        response = _extract_response_from_stdout(
            json.dumps(
                {
                    "payloads": [{"text": "envelope reply"}],
                    "meta": {
                        "durationMs": 321,
                        "agentMeta": {
                            "provider": "openai",
                            "model": "openai/gpt-5.5",
                            "usage": {"input": 123, "output": 45},
                        },
                    },
                }
            )
        )

        self.assertEqual(response.text, "envelope reply")
        self.assertEqual(response.provider, "openai")
        self.assertEqual(response.model, "openai/gpt-5.5")
        self.assertEqual(response.input_tokens, 123)
        self.assertEqual(response.output_tokens, 45)
        self.assertEqual(response.duration_ms, 321)

    def test_openclaw_cli_metadata_cannot_come_from_unselected_log_json(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        def runner(command, **kwargs):
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": (
                        '{"reply":"old","meta":{"durationMs":1,'
                        '"agentMeta":{"provider":"secret-provider",'
                        '"model":"secret-model","usage":{"input":999,'
                        '"output":999}}}}\n'
                        '{"reply":"selected"}\n'
                    ),
                    "stderr": "",
                },
            )()

        response = OpenClawCliAgentAdapter("openclaw", runner=runner).reply(
            AgentRequest(10001, "m-source", "hello", 10.0)
        )

        self.assertEqual(response.text, "selected")
        self.assertIsNone(response.provider)
        self.assertIsNone(response.model)
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.duration_ms)

    def test_openclaw_cli_rejects_malformed_optional_usage(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        def runner(command, **kwargs):
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": (
                        '{"reply":"ok","meta":{"durationMs":-1,'
                        '"agentMeta":{"provider":[],"model":"",'
                        '"usage":{"input":true,"output":3.5}}}}\n'
                    ),
                    "stderr": "",
                },
            )()

        response = OpenClawCliAgentAdapter("openclaw", runner=runner).reply(
            AgentRequest(10001, "m-malformed", "hello", 10.0)
        )

        self.assertEqual(response.text, "ok")
        self.assertIsNone(response.provider)
        self.assertIsNone(response.model)
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.duration_ms)

    def test_openclaw_cli_accepts_sqlite_integer_metadata_boundary(self):
        from qq_openclaw_bridge.agent import _extract_response_from_stdout

        sqlite_integer_max = (1 << 63) - 1
        response = _extract_response_from_stdout(
            json.dumps(
                {
                    "reply": "ok",
                    "meta": {
                        "durationMs": sqlite_integer_max,
                        "agentMeta": {
                            "usage": {
                                "input": sqlite_integer_max,
                                "output": sqlite_integer_max,
                            }
                        },
                    },
                }
            )
        )

        self.assertEqual(response.input_tokens, sqlite_integer_max)
        self.assertEqual(response.output_tokens, sqlite_integer_max)
        self.assertEqual(response.duration_ms, sqlite_integer_max)

    def test_openclaw_cli_rejects_metadata_above_sqlite_integer_boundary(self):
        from qq_openclaw_bridge.agent import _extract_response_from_stdout

        above_sqlite_integer_max = 1 << 63
        response = _extract_response_from_stdout(
            json.dumps(
                {
                    "reply": "ok",
                    "meta": {
                        "durationMs": above_sqlite_integer_max,
                        "agentMeta": {
                            "usage": {
                                "input": above_sqlite_integer_max,
                                "output": 10**100,
                            }
                        },
                    },
                }
            )
        )

        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.duration_ms)

    def test_openclaw_cli_rejects_unsafe_provider_and_model_identifiers(self):
        from qq_openclaw_bridge.agent import _extract_response_from_stdout

        invalid_identifiers = (
            "https://user:pass@example.invalid/model",
            "user:credential",
            "--token secret",
            "/private/model",
            "model\ncommand",
            "\topenai",
            "openai\r",
            "x" * 129,
        )
        for identifier in invalid_identifiers:
            with self.subTest(identifier=identifier):
                response = _extract_response_from_stdout(
                    json.dumps(
                        {
                            "reply": "ok",
                            "meta": {
                                "agentMeta": {
                                    "provider": identifier,
                                    "model": identifier,
                                }
                            },
                        }
                    )
                )
                self.assertIsNone(response.provider)
                self.assertIsNone(response.model)

    def test_openclaw_cli_accepts_conservative_provider_and_model_identifiers(self):
        from qq_openclaw_bridge.agent import _extract_response_from_stdout

        response = _extract_response_from_stdout(
            json.dumps(
                {
                    "reply": "ok",
                    "meta": {
                        "agentMeta": {
                            "provider": "openai-codex",
                            "model": "openai/gpt-5.5_test",
                        }
                    },
                }
            )
        )

        self.assertEqual(response.provider, "openai-codex")
        self.assertEqual(response.model, "openai/gpt-5.5_test")

    def test_openclaw_cli_adapter_reads_pretty_payload_json_with_logs(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        def runner(command, **kwargs):
            class Result:
                returncode = 0
                stdout = """
[plugins] plugins.allow is empty
{
  "payloads": [
    {"text": "你好", "mediaUrl": null}
  ],
  "meta": {
    "durationMs": 10069,
    "agentMeta": {
      "provider": "openai",
      "model": "gpt-pretty",
      "usage": {
        "input": 42,
        "output": 7
      }
    }
  }
}
[agent] run ended with stopReason=stop
"""
                stderr = ""

            return Result()

        response = OpenClawCliAgentAdapter(
            command="scripts/openclaw.sh", runner=runner
        ).reply(
            AgentRequest(user_id=10001, message_id="m1", text="你好", timestamp=10.0)
        )

        self.assertEqual(response.text, "你好")
        self.assertEqual(response.provider, "openai")
        self.assertEqual(response.model, "gpt-pretty")
        self.assertEqual(response.input_tokens, 42)
        self.assertEqual(response.output_tokens, 7)
        self.assertEqual(response.duration_ms, 10069)

    def test_openclaw_cli_adapter_raises_on_failed_command(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        def runner(command, **kwargs):
            class Result:
                returncode = 1
                stdout = ""
                stderr = "No API key found"

            return Result()

        with self.assertRaisesRegex(RuntimeError, "No API key found"):
            OpenClawCliAgentAdapter(command="scripts/openclaw.sh", runner=runner).reply(
                AgentRequest(
                    user_id=10001, message_id="m1", text="你好", timestamp=10.0
                )
            )

    def test_gateway_mode_rejects_embedded_fallback_result(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.models import AgentRequest

        def runner(command, **kwargs):
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": '{"reply":"embedded reply"}\n',
                    "stderr": "EMBEDDED FALLBACK: Gateway agent timed out",
                },
            )()

        with self.assertRaisesRegex(RuntimeError, "fell back to embedded"):
            OpenClawCliAgentAdapter(
                command="scripts/openclaw.sh",
                use_gateway=True,
                runner=runner,
            ).reply(
                AgentRequest(
                    user_id=10001,
                    message_id="m1",
                    text="你好",
                    timestamp=10.0,
                )
            )

    def test_factory_selects_openclaw_cli_adapter(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter, create_agent_adapter
        from qq_openclaw_bridge.config import Config

        adapter = create_agent_adapter(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users=set(),
                agent_adapter="openclaw-cli",
                openclaw_command="scripts/openclaw.sh",
            )
        )

        self.assertIsInstance(adapter, OpenClawCliAgentAdapter)
        self.assertTrue(adapter.use_gateway)


if __name__ == "__main__":
    unittest.main()
