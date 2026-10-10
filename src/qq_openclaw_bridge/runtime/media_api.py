import base64
import errno
import json
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from qq_openclaw_bridge.application.media_budget import (
    BudgetExceeded,
    MediaBudgetPolicy,
    _cost,
)


@dataclass(frozen=True)
class MediaApiResult:
    text: str
    provider: str
    model: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    duration_ms: int


class MediaApiError(RuntimeError):
    def __init__(self, error_class: str, transient: bool = False) -> None:
        self.error_class = error_class
        self.transient = transient
        super().__init__("media API %s" % error_class)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        del request, fp, code, message, headers, new_url
        return None


def validate_media_api_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError("invalid media API base URL")
    if "?" in value or "#" in value:
        raise ValueError("invalid media API base URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise ValueError("invalid media API base URL") from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid media API base URL")
    if parsed.scheme == "http" and hostname.lower() not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise ValueError("invalid media API base URL")
    return value.rstrip("/")


def _validate_header_value(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in ('\r', '\n', '"'))
    ):
        raise ValueError("invalid media API %s" % label)


class MediaApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        image_model: str,
        audio_model: str,
        repository=None,
        provider: str = "media_api",
        timeout_seconds: int = 90,
        requests_per_minute: int = 10,
        max_concurrent_calls: int = 2,
        budget_policy: Optional[MediaBudgetPolicy] = None,
        estimated_cost=0,
        actual_cost_calculator: Optional[Callable] = None,
        opener: Optional[Callable] = None,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._base_url = validate_media_api_base_url(base_url)
        if not isinstance(api_key, str) or not api_key or "\r" in api_key or "\n" in api_key:
            raise ValueError("invalid media API key")
        _validate_header_value(audio_model, "audio model")
        self._api_key = api_key
        self._image_model = image_model
        self._audio_model = audio_model
        self._repository = repository
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._requests_per_minute = requests_per_minute
        self._budget_policy = budget_policy
        self._estimated_cost = _cost(estimated_cost)
        self._actual_cost_calculator = actual_cost_calculator
        if opener is None:
            self._opener = build_opener(_NoRedirectHandler()).open
        else:
            self._opener = opener
        self._clock = time.monotonic if clock is None else clock
        self._sleeper = time.sleep if sleeper is None else sleeper
        self._semaphore = threading.BoundedSemaphore(max_concurrent_calls)
        self._lock = threading.Lock()
        self._request_times = []
        self._consecutive_transient_failures = 0
        self._circuit_open_until = 0.0

    @property
    def circuit_state(self) -> str:
        with self._lock:
            return "open" if self._clock() < self._circuit_open_until else "closed"

    def describe_image(self, media, prompt: str) -> MediaApiResult:
        failure = None
        try:
            encoded = base64.b64encode(media.path.read_bytes()).decode("ascii")
            payload = {
                "model": self._image_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:%s;base64,%s" % (media.mime, encoded)
                                },
                            },
                        ],
                    }
                ],
            }
            body = json.dumps(payload).encode("utf-8")
        except MediaApiError as exc:
            failure = MediaApiError(exc.error_class, exc.transient)
        except Exception:
            failure = MediaApiError("internal_error")
        if failure is not None:
            self._raise_local_failure("image_description", self._image_model, failure)
        return self._call(
            "image_description",
            self._image_model,
            "/v1/chat/completions",
            body,
            {"Content-Type": "application/json"},
            self._image_result,
        )

    def transcribe_audio(self, media) -> MediaApiResult:
        failure = None
        try:
            boundary = "----qq-openclaw-%s" % uuid.uuid4().hex
            body = self._multipart_body(boundary, media)
        except MediaApiError as exc:
            failure = MediaApiError(exc.error_class, exc.transient)
        except Exception:
            failure = MediaApiError("internal_error")
        if failure is not None:
            self._raise_local_failure("audio_transcription", self._audio_model, failure)
        return self._call(
            "audio_transcription",
            self._audio_model,
            "/v1/audio/transcriptions",
            body,
            {"Content-Type": "multipart/form-data; boundary=%s" % boundary},
            self._audio_result,
        )

    def _image_result(self, response, duration_ms):
        try:
            choices = response["choices"]
            if not isinstance(choices, list) or not choices:
                raise MediaApiError("malformed_response")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise MediaApiError("malformed_response")
            message = choice["message"]
            if not isinstance(message, dict):
                raise MediaApiError("malformed_response")
            text = self._response_text(message["content"])
            input_tokens, output_tokens = self._usage_tokens(response)
        except MediaApiError:
            raise
        except (KeyError, IndexError, TypeError):
            raise MediaApiError("malformed_response") from None
        return MediaApiResult(
            text=text,
            provider=self._provider,
            model=self._image_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )

    def _audio_result(self, response, duration_ms):
        try:
            text = self._response_text(response["text"])
            input_tokens, output_tokens = self._usage_tokens(response)
        except MediaApiError:
            raise
        except (KeyError, TypeError):
            raise MediaApiError("malformed_response") from None
        return MediaApiResult(
            text=text,
            provider=self._provider,
            model=self._audio_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _response_text(value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MediaApiError("malformed_response")
        return value

    @staticmethod
    def _usage_tokens(response):
        if "usage" not in response:
            return None, None
        usage = response["usage"]
        if not isinstance(usage, dict):
            raise MediaApiError("malformed_response")

        def token(name):
            if name not in usage:
                return None
            value = usage[name]
            if type(value) is not int or value < 0:
                raise MediaApiError("malformed_response")
            return value

        return token("prompt_tokens"), token("completion_tokens")

    def _multipart_body(self, boundary: str, media) -> bytes:
        filename = media.path.name
        try:
            _validate_header_value(filename, "multipart filename")
            _validate_header_value(media.mime, "multipart MIME")
        except ValueError:
            raise MediaApiError("invalid_request") from None
        prefix = "--%s\r\n" % boundary
        parts = [
            (
                prefix
                + 'Content-Disposition: form-data; name="model"\r\n\r\n'
                + self._audio_model
                + "\r\n"
            ).encode("utf-8"),
            (
                prefix
                + 'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
                % filename
            ).encode("utf-8"),
            ("Content-Type: %s\r\n\r\n" % media.mime).encode("utf-8"),
            media.path.read_bytes(),
            b"\r\n",
            ("--%s--\r\n" % boundary).encode("ascii"),
        ]
        return b"".join(parts)

    def _call(self, kind, model, path, body, headers, parser):
        started = self._clock()
        reservation = None
        result = None
        actual_cost = None
        request_started = False
        model_succeeded = False
        cost_known = False
        failure = None
        acquired = self._semaphore.acquire(blocking=False)
        if not acquired:
            failure = MediaApiError("concurrency_limit")
        else:
            try:
                if self._budget_policy is not None:
                    try:
                        reservation = self._budget_policy.reserve(
                            kind, self._estimated_cost
                        )
                    except BudgetExceeded:
                        raise MediaApiError("budget_exceeded") from None
                self._begin_call()
                request_headers = dict(headers)
                request_headers["Authorization"] = "Bearer %s" % self._api_key
                request = Request(
                    self._base_url + path,
                    data=body,
                    headers=request_headers,
                    method="POST",
                )
                request_started = True
                raw = self._read_response(request)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise MediaApiError("malformed_response") from None
                if not isinstance(parsed, dict):
                    raise MediaApiError("malformed_response")
                duration_ms = self._duration_ms(started)
                result = parser(parsed, duration_ms)
                model_succeeded = True
                self._finish_success()
                cost_calculation_failed = False
                try:
                    actual_cost = self._calculate_actual_cost(kind, model, result)
                except Exception:
                    actual_cost = self._estimated_cost
                    cost_calculation_failed = True
                cost_known = True
                if reservation is not None:
                    reservation.reconcile(actual_cost)
                if cost_calculation_failed:
                    raise MediaApiError("internal_error") from None
            except Exception as exc:
                failure = self._classify_exception(exc)
                if reservation is not None and not cost_known:
                    try:
                        reservation.release()
                    except Exception:
                        failure = MediaApiError("internal_error")
                if request_started and not model_succeeded:
                    try:
                        if failure.transient:
                            self._finish_transient_failure()
                        else:
                            self._finish_nontransient_failure()
                    except Exception:
                        failure = MediaApiError("internal_error")
            finally:
                self._semaphore.release()

        if failure is not None:
            duration_ms = self._duration_ms(started)
            if model_succeeded and cost_known:
                self._record(
                    kind,
                    model,
                    True,
                    duration_ms,
                    failure.error_class,
                    result.input_tokens,
                    result.output_tokens,
                    actual_cost,
                )
            else:
                self._record(
                    kind,
                    model,
                    False,
                    duration_ms,
                    failure.error_class,
                    None,
                    None,
                    None,
                )
            self._raise_clean(failure)

        assert result is not None
        assert actual_cost is not None
        self._record(
            kind,
            model,
            True,
            result.duration_ms,
            None,
            result.input_tokens,
            result.output_tokens,
            actual_cost,
        )
        return result

    def _read_response(self, request) -> bytes:
        response = None
        failure = None
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
        except HTTPError as exc:
            status = exc.code
            close_failed = self._close_response(exc)
            failure = (
                MediaApiError("internal_error")
                if close_failed
                else self._http_error(status)
            )
        except Exception as exc:
            failure = self._classify_exception(exc)
        if failure is not None:
            self._raise_clean(failure)

        read_failure = None
        raw = None
        try:
            raw = response.read()
        except HTTPError as exc:
            status = exc.code
            read_failure = (
                MediaApiError("internal_error")
                if self._close_response(exc)
                else self._http_error(status)
            )
        except Exception as exc:
            read_failure = self._classify_exception(exc)
        close_failed = self._close_response(response)
        if read_failure is not None:
            self._raise_clean(read_failure)
        if close_failed:
            self._raise_clean(MediaApiError("internal_error"))
        return raw

    @staticmethod
    def _close_response(response) -> bool:
        close = getattr(response, "close", None)
        if close is None:
            return False
        try:
            close()
        except Exception:
            return True
        return False

    def _calculate_actual_cost(
        self, kind: str, model: str, result: MediaApiResult
    ) -> Decimal:
        if self._actual_cost_calculator is None:
            return self._estimated_cost
        value = self._actual_cost_calculator(
            kind=kind,
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        return _cost(value)

    def _duration_ms(self, started: float) -> int:
        try:
            return max(0, int((self._clock() - started) * 1000))
        except Exception:
            return 0

    @staticmethod
    def _classify_exception(exc: Exception) -> MediaApiError:
        if isinstance(exc, MediaApiError):
            return MediaApiError(exc.error_class, exc.transient)
        if isinstance(exc, HTTPError):
            return MediaApiClient._http_error(exc.code)
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return MediaApiError("timeout", transient=True)
        if isinstance(exc, URLError):
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)) or getattr(
                reason, "errno", None
            ) == errno.ETIMEDOUT:
                return MediaApiError("timeout", transient=True)
            return MediaApiError("transport", transient=True)
        if isinstance(exc, OSError):
            if getattr(exc, "errno", None) == errno.ETIMEDOUT:
                return MediaApiError("timeout", transient=True)
            return MediaApiError("transport", transient=True)
        return MediaApiError("internal_error")

    def _begin_call(self) -> None:
        now = self._clock()
        with self._lock:
            if now < self._circuit_open_until:
                raise MediaApiError("circuit_open")
            self._request_times = [
                timestamp for timestamp in self._request_times if now - timestamp < 60
            ]
            if len(self._request_times) >= self._requests_per_minute:
                raise MediaApiError("rate_limited")
            self._request_times.append(now)

    def _finish_success(self) -> None:
        with self._lock:
            self._consecutive_transient_failures = 0
            self._circuit_open_until = 0.0

    def _finish_nontransient_failure(self) -> None:
        with self._lock:
            self._consecutive_transient_failures = 0
            self._circuit_open_until = 0.0

    def _finish_transient_failure(self) -> None:
        with self._lock:
            self._consecutive_transient_failures += 1
            if self._consecutive_transient_failures >= 3:
                self._circuit_open_until = self._clock() + 60

    @staticmethod
    def _http_error(status: int) -> MediaApiError:
        if 300 <= status <= 399:
            return MediaApiError("redirect_rejected")
        if status == 401:
            return MediaApiError("authentication")
        if status == 403:
            return MediaApiError("authorization")
        if status == 429:
            return MediaApiError("rate_limited", transient=True)
        if 500 <= status <= 599:
            return MediaApiError("server_error", transient=True)
        return MediaApiError("invalid_request")

    def _raise_local_failure(self, kind, model, failure) -> None:
        self._record(
            kind,
            model,
            False,
            0,
            failure.error_class,
            None,
            None,
            None,
        )
        self._raise_clean(failure)

    @staticmethod
    def _raise_clean(error: MediaApiError) -> None:
        clean = MediaApiError(error.error_class, error.transient)
        clean.__cause__ = None
        clean.__context__ = None
        raise clean

    def _record(
        self,
        kind,
        model,
        success,
        duration_ms,
        error_class,
        input_tokens,
        output_tokens,
        actual_cost,
    ) -> None:
        if self._repository is None:
            return
        failed = False
        try:
            self._repository.record_model_call(
                call_kind=kind,
                provider=self._provider,
                model=model,
                success=success,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                error_class=error_class,
                estimated_cost=actual_cost,
            )
        except Exception:
            failed = True
        if failed:
            raise MediaApiError("accounting")


__all__ = [
    "MediaApiClient",
    "MediaApiError",
    "MediaApiResult",
    "validate_media_api_base_url",
]
