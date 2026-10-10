import base64
from decimal import Decimal
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import traceback
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.closed = False

    def read(self):
        return self._payload

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False


class RecordingRepository:
    def __init__(self):
        self.calls = []

    def record_model_call(self, **kwargs):
        self.calls.append(kwargs)


class RaisingRepository:
    def __init__(self, message):
        self.message = message
        self.attempts = 0

    def record_model_call(self, **kwargs):
        del kwargs
        self.attempts += 1
        raise RuntimeError(self.message)


class MediaApiTests(unittest.TestCase):
    def setUp(self):
        from qq_openclaw_bridge.domain.media import ValidatedMedia

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.image_path = Path(self.tempdir.name) / "image.png"
        self.image_path.write_bytes(b"image-bytes")
        self.audio_path = Path(self.tempdir.name) / "audio.wav"
        self.audio_path.write_bytes(b"audio-bytes")
        self.image = ValidatedMedia("image-id", self.image_path, "image", "image/png", 11, "hash", None)
        self.audio = ValidatedMedia("audio-id", self.audio_path, "audio", "audio/wav", 11, "hash", None)
        self.clock = FakeClock()
        self.repository = RecordingRepository()

    def client(self, opener, **kwargs):
        from qq_openclaw_bridge.runtime.media_api import MediaApiClient

        repository = kwargs.pop("repository", self.repository)
        return MediaApiClient(
            base_url="https://media.example/api/",
            api_key="not-a-real-key",
            image_model="vision-1",
            audio_model="audio-1",
            provider="configured-provider",
            repository=repository,
            opener=opener,
            clock=self.clock,
            **kwargs
        )

    def test_secret_ref_uses_fixed_security_command_and_never_falls_back(self):
        from qq_openclaw_bridge.runtime.secrets import SecretResolutionError, SecretResolver

        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return type("Result", (), {"stdout": "resolved-key\n"})()

        self.assertEqual(
            SecretResolver(runner=runner, timeout_seconds=7).resolve(
                "keychain:service:account", "fallback"
            ),
            "resolved-key",
        )
        self.assertEqual(calls[0][0], ["/usr/bin/security", "find-generic-password", "-w", "-s", "service", "-a", "account"])
        self.assertEqual(calls[0][1]["timeout"], 7)
        self.assertFalse(calls[0][1]["shell"])

        failing = SecretResolver(runner=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not-a-real-key")))
        with self.assertRaises(SecretResolutionError) as error:
            failing.resolve("keychain:service:account", "fallback")
        self.assertNotIn("not-a-real-key", str(error.exception))
        with self.assertRaises(SecretResolutionError):
            SecretResolver(runner=runner).resolve("exec:echo:key", "fallback")

    def test_secret_resolution_failure_removes_sensitive_exception_chain(self):
        from qq_openclaw_bridge.runtime.secrets import (
            SecretResolutionError,
            SecretResolver,
        )

        sentinel = "sentinel-key-material"

        class SensitiveRunnerError(RuntimeError):
            def __init__(self):
                super().__init__("runner failed with %s" % sentinel)
                self.stdout = "stdout=%s" % sentinel
                self.stderr = "stderr=%s" % sentinel
                self.secret = sentinel

        def runner(*args, **kwargs):
            del args, kwargs
            raise SensitiveRunnerError()

        with self.assertRaises(SecretResolutionError) as raised:
            SecretResolver(runner=runner).resolve(
                "keychain:service:account",
                "fallback-must-not-be-used",
            )

        error = raised.exception
        formatted = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.assertEqual(str(error), "media secret resolution failed")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(sentinel, formatted)

    def test_image_request_encodes_json_image_url_and_authorization(self):
        requests = []
        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(b'{"choices":[{"message":{"content":"a cat"}}],"usage":{"prompt_tokens":3,"completion_tokens":4}}')

        result = self.client(opener).describe_image(self.image, "describe this")

        request, timeout = requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://media.example/api/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer not-a-real-key")
        self.assertEqual(timeout, 90)
        self.assertEqual(payload["model"], "vision-1")
        image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
        self.assertEqual(image_url, "data:image/png;base64," + base64.b64encode(b"image-bytes").decode("ascii"))
        self.assertEqual(result.text, "a cat")
        self.assertEqual((result.provider, result.model, result.input_tokens, result.output_tokens), ("configured-provider", "vision-1", 3, 4))

    def test_audio_request_uses_multipart_fields_and_boundary(self):
        requests = []
        def opener(request, timeout):
            requests.append(request)
            return FakeResponse(b'{"text":"hello"}')

        result = self.client(opener).transcribe_audio(self.audio)

        request = requests[0]
        content_type = request.get_header("Content-type")
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="model"', request.data)
        self.assertIn(b"audio-1", request.data)
        self.assertIn(b'name="file"; filename="audio.wav"', request.data)
        self.assertIn(b"audio-bytes", request.data)
        self.assertEqual(request.full_url, "https://media.example/api/v1/audio/transcriptions")
        self.assertEqual(result.text, "hello")

    def test_failure_classes_and_records_do_not_leak_secret(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        failures = [
            (HTTPError("url", 401, "", {}, io.BytesIO()), "authentication"),
            (HTTPError("url", 403, "", {}, io.BytesIO()), "authorization"),
            (HTTPError("url", 429, "", {}, io.BytesIO()), "rate_limited"),
            (HTTPError("url", 503, "", {}, io.BytesIO()), "server_error"),
            (socket.timeout(), "timeout"),
            (URLError("offline"), "transport"),
            (FakeResponse(b"not-json"), "malformed_response"),
        ]
        for response, expected in failures:
            with self.subTest(expected=expected):
                def opener(request, timeout, response=response):
                    if isinstance(response, Exception):
                        raise response
                    return response
                with self.assertRaises(MediaApiError) as error:
                    self.client(opener).describe_image(self.image, "prompt")
                self.assertEqual(error.exception.error_class, expected)
                self.assertNotIn("not-a-real-key", str(error.exception))
                self.assertEqual(self.repository.calls[-1]["error_class"], expected)
                self.assertNotIn("not-a-real-key", repr(self.repository.calls[-1]))

    def test_rate_limit_concurrency_budget_and_circuit_prevent_http_calls(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        calls = []
        def opener(request, timeout):
            calls.append(request)
            raise URLError("offline")

        blocked = self.client(opener, budget_policy=MediaBudgetPolicy(0), estimated_cost=0.01)
        with self.assertRaises(MediaApiError) as error:
            blocked.describe_image(self.image, "prompt")
        self.assertEqual(error.exception.error_class, "budget_exceeded")
        self.assertEqual(calls, [])

        rate_limited = self.client(opener, requests_per_minute=2)
        for unused in range(3):
            with self.assertRaises(MediaApiError):
                rate_limited.describe_image(self.image, "prompt")
        self.assertEqual(len(calls), 2)

        client = self.client(opener, requests_per_minute=10)
        for unused in range(3):
            with self.assertRaises(MediaApiError):
                client.describe_image(self.image, "prompt")
        self.assertEqual(client.circuit_state, "open")
        self.clock.advance(60)
        with self.assertRaises(MediaApiError):
            client.describe_image(self.image, "prompt")
        self.assertEqual(len(calls), 6)

    def test_concurrency_limit_rejects_second_in_flight_call(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        entered = threading.Event()
        release = threading.Event()
        calls = []
        def opener(request, timeout):
            calls.append(request)
            entered.set()
            release.wait(1)
            return FakeResponse(b'{"choices":[{"message":{"content":"ok"}}]}')

        client = self.client(opener, max_concurrent_calls=1)
        thread = threading.Thread(
            target=lambda: client.describe_image(self.image, "prompt")
        )
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(MediaApiError) as error:
            client.describe_image(self.image, "prompt")
        self.assertEqual(error.exception.error_class, "concurrency_limit")
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls), 1)

    def test_success_resets_transient_failures_and_records_duration(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        sequence = [URLError("offline"), FakeResponse(b'{"choices":[{"message":{"content":"ok"}}]}')]
        def opener(request, timeout):
            response = sequence.pop(0)
            self.clock.advance(0.123)
            if isinstance(response, Exception):
                raise response
            return response

        client = self.client(opener)
        with self.assertRaises(MediaApiError):
            client.describe_image(self.image, "prompt")
        result = client.describe_image(self.image, "prompt")

        self.assertEqual(result.duration_ms, 123)
        self.assertEqual(client.circuit_state, "closed")
        self.assertEqual(self.repository.calls[-1]["success"], True)
        self.assertEqual(self.repository.calls[-1]["duration_ms"], 123)

    def test_base_url_validation_rejects_unsafe_origins(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiClient

        invalid_urls = (
            "http://media.example",
            "ftp://media.example",
            "https://",
            "https://user:password@media.example",
            "https://media.example/api?key=value",
            "https://media.example/api#fragment",
            "https://media.example/\r\nX-Evil: yes",
            "http://localhost.evil",
        )
        for base_url in invalid_urls:
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ValueError, "base URL"):
                    MediaApiClient(base_url, "key", "image", "audio", opener=lambda *args: None)

        for base_url in (
            "https://media.example/api",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://[::1]:8080",
        ):
            with self.subTest(base_url=base_url):
                MediaApiClient(base_url, "key", "image", "audio", opener=lambda *args: None)

    def test_default_transport_rejects_redirect_and_closes_http_error(self):
        from qq_openclaw_bridge.runtime import media_api

        class TrackedBody(io.BytesIO):
            def __init__(self):
                super().__init__(b"redirect body with not-a-real-key")
                self.was_closed = False

            def close(self):
                self.was_closed = True
                super().close()

        body = TrackedBody()
        requests = []

        class FakeTransport:
            def open(self, request, timeout):
                requests.append((request.full_url, request.get_header("Authorization"), timeout))
                raise HTTPError(
                    request.full_url,
                    302,
                    "redirect to https://other.example",
                    {"Location": "https://other.example/steal"},
                    body,
                )

        handlers = []

        def fake_build_opener(*items):
            handlers.extend(items)
            return FakeTransport()

        with mock.patch.object(media_api, "build_opener", side_effect=fake_build_opener):
            client = media_api.MediaApiClient(
                base_url="https://media.example",
                api_key="not-a-real-key",
                image_model="vision-1",
                audio_model="audio-1",
                repository=self.repository,
                clock=self.clock,
            )
            with self.assertRaises(media_api.MediaApiError) as raised:
                client.describe_image(self.image, "prompt")

        self.assertEqual(raised.exception.error_class, "redirect_rejected")
        self.assertEqual(
            requests,
            [("https://media.example/v1/chat/completions", "Bearer not-a-real-key", 90)],
        )
        self.assertEqual(len(handlers), 1)
        self.assertEqual(type(handlers[0]).__name__, "_NoRedirectHandler")
        self.assertTrue(body.was_closed)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn("not-a-real-key", repr(raised.exception))

    def test_unknown_transport_stages_are_sanitized_released_and_accounted_once(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        marker = "sensitive-internal-detail"

        class BrokenResponse(FakeResponse):
            def __init__(self, stage):
                super().__init__(
                    b'{"choices":[{"message":{"content":"ok"}}]}'
                )
                self.stage = stage

            def read(self):
                if self.stage == "read":
                    raise RuntimeError(marker)
                return super().read()

            def close(self):
                super().close()
                if self.stage == "close":
                    raise RuntimeError(marker)

        for stage in ("open", "read", "close", "parser"):
            with self.subTest(stage=stage):
                responses = [
                    RuntimeError(marker)
                    if stage == "open"
                    else BrokenResponse(stage),
                    FakeResponse(b'{"choices":[{"message":{"content":"ok"}}]}'),
                ]
                repository = RecordingRepository()
                budget = MediaBudgetPolicy("1.00")

                def opener(request, timeout):
                    del request, timeout
                    response = responses.pop(0)
                    if isinstance(response, Exception):
                        raise response
                    return response

                client = self.client(
                    opener,
                    repository=repository,
                    budget_policy=budget,
                    estimated_cost="0.25",
                    max_concurrent_calls=1,
                )
                if stage == "parser":
                    client._image_result = lambda *args: (_ for _ in ()).throw(
                        RuntimeError(marker)
                    )

                with self.assertRaises(MediaApiError) as raised:
                    client.describe_image(self.image, "prompt")

                self.assertEqual(raised.exception.error_class, "internal_error")
                self.assertNotIn(marker, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(budget.reserved_cost, 0)
                self.assertEqual(budget.consumed_cost, 0)
                self.assertEqual(len(repository.calls), 1)
                self.assertFalse(repository.calls[0]["success"])
                self.assertIsNone(repository.calls[0]["estimated_cost"])

                if stage == "parser":
                    client._image_result = lambda response, duration: type(
                        "Result",
                        (),
                        {
                            "text": response["choices"][0]["message"]["content"],
                            "provider": "configured-provider",
                            "model": "vision-1",
                            "input_tokens": None,
                            "output_tokens": None,
                            "duration_ms": duration,
                        },
                    )()
                client.describe_image(self.image, "prompt")

    def test_accounting_failure_after_success_preserves_consumed_budget(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy
        from qq_openclaw_bridge.runtime.media_api import MediaApiClient, MediaApiError

        repository = RaisingRepository("database has not-a-real-key")
        budget = MediaBudgetPolicy("1.00")
        client = MediaApiClient(
            base_url="https://media.example",
            api_key="not-a-real-key",
            image_model="vision-1",
            audio_model="audio-1",
            repository=repository,
            budget_policy=budget,
            estimated_cost="0.25",
            opener=lambda request, timeout: FakeResponse(
                b'{"choices":[{"message":{"content":"ok"}}]}'
            ),
            clock=self.clock,
        )

        with self.assertRaises(MediaApiError) as raised:
            client.describe_image(self.image, "prompt")

        self.assertEqual(raised.exception.error_class, "accounting")
        self.assertNotIn("not-a-real-key", str(raised.exception))
        self.assertEqual(repository.attempts, 1)
        self.assertEqual(budget.consumed_cost, Decimal("0.25"))
        self.assertEqual(budget.reserved_cost, Decimal("0"))

    def test_repository_failure_while_recording_failure_becomes_accounting(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiClient, MediaApiError

        repository = RaisingRepository("database has not-a-real-key")
        client = MediaApiClient(
            base_url="https://media.example",
            api_key="not-a-real-key",
            image_model="vision-1",
            audio_model="audio-1",
            repository=repository,
            opener=lambda request, timeout: (_ for _ in ()).throw(
                RuntimeError("opener has not-a-real-key")
            ),
            clock=self.clock,
        )

        with self.assertRaises(MediaApiError) as raised:
            client.describe_image(self.image, "prompt")

        self.assertEqual(raised.exception.error_class, "accounting")
        self.assertNotIn("not-a-real-key", str(raised.exception))
        self.assertEqual(repository.attempts, 1)

    def test_reconcile_failure_after_valid_result_does_not_release_reservation(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        class Reservation:
            def __init__(self):
                self.reconciled = 0
                self.released = 0

            def reconcile(self, actual_cost):
                del actual_cost
                self.reconciled += 1
                raise RuntimeError("reconcile secret")

            def release(self):
                self.released += 1

        reservation = Reservation()

        class Budget:
            def reserve(self, kind, estimated_cost):
                del kind, estimated_cost
                return reservation

        client = self.client(
            lambda request, timeout: FakeResponse(
                b'{"choices":[{"message":{"content":"ok"}}]}'
            ),
            budget_policy=Budget(),
            estimated_cost="0.25",
        )

        with self.assertRaises(MediaApiError) as raised:
            client.describe_image(self.image, "prompt")

        self.assertEqual(raised.exception.error_class, "internal_error")
        self.assertEqual(reservation.reconciled, 1)
        self.assertEqual(reservation.released, 0)

    def test_local_budget_rejection_records_no_estimated_cost(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        with self.assertRaises(MediaApiError):
            self.client(
                lambda request, timeout: self.fail("HTTP must not be called"),
                budget_policy=MediaBudgetPolicy(0),
                estimated_cost="0.25",
            ).describe_image(self.image, "prompt")

        self.assertFalse(self.repository.calls[-1]["success"])
        self.assertIsNone(self.repository.calls[-1]["estimated_cost"])

    def test_real_repository_records_tokens_actual_cost_and_no_sensitive_payload(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository
        from qq_openclaw_bridge.runtime.media_api import MediaApiClient, MediaApiError

        database = SqliteDatabase(Path(self.tempdir.name) / "state.sqlite3")
        repository = E4Repository(database)
        budget = MediaBudgetPolicy("1.00")
        calculator_calls = []

        def calculator(**values):
            calculator_calls.append(values)
            return Decimal("0.125")

        client = MediaApiClient(
            base_url="https://media.example",
            api_key="not-a-real-key",
            image_model="vision-1",
            audio_model="audio-1",
            provider="configured-provider",
            repository=repository,
            budget_policy=budget,
            estimated_cost="0.40",
            actual_cost_calculator=calculator,
            opener=lambda request, timeout: FakeResponse(
                b'{"choices":[{"message":{"content":"safe result"}}],'
                b'"usage":{"prompt_tokens":3,"completion_tokens":4}}'
            ),
            clock=self.clock,
        )

        client.describe_image(self.image, "prompt")

        self.assertEqual(
            calculator_calls,
            [
                {
                    "kind": "image_description",
                    "model": "vision-1",
                    "input_tokens": 3,
                    "output_tokens": 4,
                }
            ],
        )
        self.assertEqual(budget.consumed_cost, Decimal("0.125"))
        with database.connect() as connection:
            success = connection.execute(
                "SELECT input_tokens, output_tokens, success, estimated_cost "
                "FROM model_calls ORDER BY id"
            ).fetchone()
        self.assertEqual(success, (3, 4, 1, 0.125))

        failing = MediaApiClient(
            base_url="https://media.example",
            api_key="not-a-real-key",
            image_model="vision-1",
            audio_model="audio-1",
            provider="configured-provider",
            repository=repository,
            budget_policy=budget,
            estimated_cost="0.40",
            opener=lambda request, timeout: (_ for _ in ()).throw(
                URLError("offline")
            ),
            clock=self.clock,
        )
        with self.assertRaises(MediaApiError):
            failing.describe_image(self.image, "prompt")

        with database.connect() as connection:
            rows = connection.execute(
                "SELECT input_tokens, output_tokens, success, error_class, "
                "estimated_cost FROM model_calls ORDER BY id"
            ).fetchall()
        self.assertEqual(rows[1], (None, None, 0, "transport", None))
        stored = repr(rows)
        self.assertNotIn("not-a-real-key", stored)
        self.assertNotIn(base64.b64encode(b"image-bytes").decode("ascii"), stored)

    def test_cost_calculator_failure_uses_estimate_and_records_success_once(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        budget = MediaBudgetPolicy("1.00")
        client = self.client(
            lambda request, timeout: FakeResponse(
                b'{"choices":[{"message":{"content":"ok"}}],'
                b'"usage":{"prompt_tokens":3,"completion_tokens":4}}'
            ),
            budget_policy=budget,
            estimated_cost="0.25",
            actual_cost_calculator=lambda **values: (_ for _ in ()).throw(
                RuntimeError("calculator secret")
            ),
        )

        with self.assertRaises(MediaApiError) as raised:
            client.describe_image(self.image, "prompt")

        self.assertEqual(raised.exception.error_class, "internal_error")
        self.assertNotIn("calculator secret", str(raised.exception))
        self.assertEqual(budget.consumed_cost, Decimal("0.25"))
        self.assertEqual(budget.reserved_cost, Decimal("0"))
        self.assertEqual(len(self.repository.calls), 1)
        self.assertEqual(
            self.repository.calls[0],
            {
                "call_kind": "image_description",
                "provider": "configured-provider",
                "model": "vision-1",
                "success": True,
                "input_tokens": 3,
                "output_tokens": 4,
                "duration_ms": 0,
                "error_class": "internal_error",
                "estimated_cost": Decimal("0.25"),
            },
        )

    def test_cost_calculator_failure_without_repository_keeps_budget_consumed(self):
        from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        budget = MediaBudgetPolicy("1.00")
        client = self.client(
            lambda request, timeout: FakeResponse(
                b'{"choices":[{"message":{"content":"ok"}}]}'
            ),
            repository=None,
            budget_policy=budget,
            estimated_cost="0.25",
            actual_cost_calculator=lambda **values: (_ for _ in ()).throw(
                RuntimeError("calculator secret")
            ),
        )

        with self.assertRaises(MediaApiError) as raised:
            client.describe_image(self.image, "prompt")

        self.assertEqual(raised.exception.error_class, "internal_error")
        self.assertEqual(budget.consumed_cost, Decimal("0.25"))
        self.assertEqual(budget.reserved_cost, Decimal("0"))

    def test_response_shape_requires_nonempty_text_dict_usage_and_valid_tokens(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        payloads = (
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"content": []}}]},
            {"choices": [{"message": {"content": "  "}}]},
            {"choices": [{"message": {"content": "ok"}}], "usage": []},
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": True},
            },
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"completion_tokens": -1},
            },
            {"text": None},
            {"text": {}},
            {"text": ""},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                encoded = json.dumps(payload).encode("utf-8")
                target = self.audio if "text" in payload else self.image
                call = (
                    self.client(lambda request, timeout: FakeResponse(encoded)).transcribe_audio
                    if "text" in payload
                    else self.client(lambda request, timeout: FakeResponse(encoded)).describe_image
                )
                with self.assertRaises(MediaApiError) as raised:
                    call(target, "prompt") if target is self.image else call(target)
                self.assertEqual(raised.exception.error_class, "malformed_response")

    def test_urlerror_timeout_reason_is_classified_as_timeout(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        timeout_client = self.client(
            lambda request, timeout: (_ for _ in ()).throw(
                URLError(socket.timeout("timed out"))
            )
        )
        with self.assertRaises(MediaApiError) as raised:
            timeout_client.describe_image(self.image, "prompt")
        self.assertEqual(raised.exception.error_class, "timeout")

    def test_nontransient_failure_breaks_transient_sequence(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        sequence = [
            URLError("offline"),
            HTTPError("url", 401, "", {}, io.BytesIO()),
            URLError("offline"),
            URLError("offline"),
        ]

        def opener(request, timeout):
            del request, timeout
            raise sequence.pop(0)

        client = self.client(opener, requests_per_minute=10)
        for unused in range(4):
            with self.assertRaises(MediaApiError):
                client.describe_image(self.image, "prompt")
        self.assertEqual(client.circuit_state, "closed")

    def test_concurrency_rejection_does_not_consume_request_rate_slot(self):
        from qq_openclaw_bridge.runtime.media_api import MediaApiError

        entered = threading.Event()
        release = threading.Event()
        calls = []

        def opener(request, timeout):
            del timeout
            calls.append(request)
            if len(calls) == 1:
                entered.set()
                release.wait(1)
            return FakeResponse(b'{"choices":[{"message":{"content":"ok"}}]}')

        client = self.client(
            opener,
            max_concurrent_calls=1,
            requests_per_minute=2,
        )
        errors = []
        thread = threading.Thread(
            target=lambda: (
                client.describe_image(self.image, "prompt")
                if not errors
                else None
            )
        )
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(MediaApiError) as raised:
            client.describe_image(self.image, "prompt")
        self.assertEqual(raised.exception.error_class, "concurrency_limit")
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())

        client.describe_image(self.image, "prompt")
        self.assertEqual(len(calls), 2)

    def test_multipart_fields_reject_header_injection(self):
        from qq_openclaw_bridge.domain.media import ValidatedMedia
        from qq_openclaw_bridge.runtime.media_api import MediaApiClient, MediaApiError

        bad_path = Path(self.tempdir.name) / 'bad"name.wav'
        bad_path.write_bytes(b"audio")
        bad_filename = ValidatedMedia(
            "bad-name", bad_path, "audio", "audio/wav", 5, "hash", None
        )
        bad_mime = ValidatedMedia(
            "bad-mime", self.audio_path, "audio", "audio/wav\r\nX-Evil: yes", 11, "hash", None
        )
        opener = lambda request, timeout: self.fail("HTTP must not be called")

        for media in (bad_filename, bad_mime):
            with self.subTest(media=media.asset_id):
                with self.assertRaises(MediaApiError) as raised:
                    self.client(opener).transcribe_audio(media)
                self.assertEqual(raised.exception.error_class, "invalid_request")

        with self.assertRaisesRegex(ValueError, "audio model"):
            MediaApiClient(
                "https://media.example",
                "key",
                "image",
                "audio\r\ninjected",
                opener=opener,
            )


if __name__ == "__main__":
    unittest.main()
