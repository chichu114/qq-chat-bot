import json
import os
import re
import shlex
import stat
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Protocol, Set, Tuple
from urllib import error, request
from pathlib import Path

from .config import Config
from .domain.routing import TASK_KINDS
from .models import AgentRequest, AgentResponse


SQLITE_INTEGER_MAX = (1 << 63) - 1
MAX_USAGE_TOKENS = SQLITE_INTEGER_MAX
MAX_DURATION_MS = SQLITE_INTEGER_MAX
MAX_MODEL_IDENTIFIER_LENGTH = 128
_MODEL_IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z"
)
_ROUTING_AGENT_IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z"
)
_OPENAI_RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
_ROUTING_RESPONSE_MAX_OUTPUT_TOKENS = 256
_ROUTING_RESPONSE_MAX_BYTES = 1024 * 1024
_OPENAI_API_KEY_MAX_LENGTH = 4096
_CURL_TIMEOUT_EXIT_CODE = 28
_CURL_ENVIRONMENT_WHITELIST = frozenset({
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "LANG",
    "LC_CTYPE",
})


class AgentAdapter(Protocol):
    def reply(self, request_data: AgentRequest) -> AgentResponse:
        raise NotImplementedError


class CancellationGate:
    """Synchronizes shutdown against routing-only process creation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False

    @property
    def lock(self):
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def is_closed(self) -> bool:
        with self._lock:
            return self._closed


class EchoAgentAdapter:
    def reply(self, request_data: AgentRequest) -> AgentResponse:
        return AgentResponse(text="echo: %s" % request_data.text)


class HttpAgentAdapter:
    def __init__(
        self,
        endpoint: str,
        timeout_seconds: float = 30.0,
        opener: Callable = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._opener = request.urlopen if opener is None else opener

    def reply(self, request_data: AgentRequest) -> AgentResponse:
        payload = {
            "user_id": request_data.user_id,
            "message_id": request_data.message_id,
            "text": request_data.text,
            "timestamp": request_data.timestamp,
        }
        for field_name in ("context", "media_context", "task_id"):
            value = getattr(request_data, field_name)
            if value:
                payload[field_name] = value
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with self._opener(req, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except error.URLError as exc:
            raise RuntimeError("OpenClaw HTTP request failed: %s" % exc) from exc

        try:
            data = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("OpenClaw HTTP response was not valid JSON") from exc

        if not isinstance(data, dict):
            raise RuntimeError("OpenClaw HTTP response must be a JSON object")

        for field_name in ("reply", "text", "message"):
            value = data.get(field_name)
            if isinstance(value, str) and value.strip():
                return AgentResponse(text=value.strip())

        raise RuntimeError("OpenClaw HTTP response must include reply, text, or message")


class OpenAIResponsesAgentAdapter:
    """Use the Responses API as the isolated routing classifier transport."""

    def __init__(
        self,
        model: str,
        routing_agent_id: str,
        timeout_seconds: float = 30.0,
        auth_path: Optional[Path] = None,
        cancellable: bool = False,
        popen_factory: Callable = None,
        cancellation_gate: Optional[CancellationGate] = None,
    ) -> None:
        if not isinstance(model, str) or not model.startswith("openai/"):
            raise ValueError("responses classifier model must be openai/<id>")
        model_id = model[len("openai/"):]
        if _MODEL_IDENTIFIER_PATTERN.fullmatch(model_id) is None or "/" in model_id:
            raise ValueError("responses classifier model must be openai/<id>")
        if _ROUTING_AGENT_IDENTIFIER_PATTERN.fullmatch(routing_agent_id or "") is None:
            raise ValueError("routing agent ID must be bounded")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("responses classifier timeout must be greater than zero")
        self.model = model_id
        self.routing_agent_id = routing_agent_id
        self.timeout_seconds = timeout_seconds
        self._cancellable = cancellable
        self._popen_factory = subprocess.Popen if popen_factory is None else popen_factory
        self._active_processes: Set[object] = set()
        self._active_processes_lock = threading.Lock()
        self._cancellation_gate = cancellation_gate or CancellationGate()
        self._auth_path = (
            Path(auth_path)
            if auth_path is not None
            else Path.home() / ".openclaw" / "agents" / routing_agent_id
            / "agent" / "codex-home" / "auth.json"
        )

    def reply(self, request_data: AgentRequest) -> AgentResponse:
        api_key = self._read_api_key()
        payload = {
            "model": self.model,
            "input": request_data.text,
            "store": False,
            "reasoning": {"effort": "none"},
            "max_output_tokens": _ROUTING_RESPONSE_MAX_OUTPUT_TOKENS,
            "text": {"format": self._response_format()},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        started = time.perf_counter_ns()
        response_body = self._run_curl(body, api_key)

        try:
            data = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid OpenAI Responses JSON") from None
        output_text, model, input_tokens, output_tokens = self._parse_response(data)
        duration_ms = (time.perf_counter_ns() - started) // 1_000_000
        return AgentResponse(
            text=output_text,
            provider="openai",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int(duration_ms),
        )

    def cancel_active(self) -> None:
        self._cancel_active_processes("terminate")

    def force_cancel_active(self) -> None:
        self._cancel_active_processes("kill")

    def bind_cancellation_gate(self, cancellation_gate: CancellationGate) -> None:
        self._cancellation_gate = cancellation_gate

    def _run_curl(self, body: bytes, api_key: str) -> bytes:
        try:
            header_read_fd, header_write_fd = os.pipe()
        except OSError:
            raise RuntimeError("OpenAI Responses transport failed") from None

        headers = (
            "Authorization: Bearer %s\nContent-Type: application/json\n"
            % api_key
        ).encode("utf-8")
        try:
            try:
                pipe_capacity = os.fpathconf(header_write_fd, "PC_PIPE_BUF")
                if len(headers) > pipe_capacity:
                    raise RuntimeError("OpenAI Responses auth header is too large")
                if os.write(header_write_fd, headers) != len(headers):
                    raise RuntimeError("OpenAI Responses auth header write failed")
            except OSError:
                raise RuntimeError("OpenAI Responses transport failed") from None
        except Exception:
            os.close(header_read_fd)
            raise
        finally:
            os.close(header_write_fd)

        command = self._curl_command(header_read_fd)
        process = None
        try:
            with self._cancellation_gate.lock:
                if self._cancellation_gate.is_closed():
                    raise RuntimeError("routing classifier is shutting down")
                process = self._popen_factory(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=(header_read_fd,),
                    env=self._sanitized_environment(),
                )
                if self._cancellable:
                    with self._active_processes_lock:
                        self._active_processes.add(process)
        except OSError:
            raise RuntimeError("OpenAI Responses transport failed") from None
        finally:
            os.close(header_read_fd)

        try:
            try:
                stdout, _stderr = process.communicate(
                    input=body, timeout=self.timeout_seconds
                )
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                self._kill_process(process)
                try:
                    process.communicate()
                except Exception:
                    pass
                raise TimeoutError("OpenAI Responses request timed out") from None
            except OSError:
                self._terminate_process(process)
                self._kill_process(process)
                try:
                    process.communicate()
                except Exception:
                    pass
                raise RuntimeError("OpenAI Responses transport failed") from None
        finally:
            if self._cancellable:
                with self._active_processes_lock:
                    self._active_processes.discard(process)

        if process.returncode == _CURL_TIMEOUT_EXIT_CODE:
            raise TimeoutError("OpenAI Responses request timed out")
        if process.returncode != 0:
            raise RuntimeError("OpenAI Responses request failed")
        if not isinstance(stdout, bytes) or len(stdout) > _ROUTING_RESPONSE_MAX_BYTES:
            raise RuntimeError("OpenAI Responses response exceeded the size limit")
        return stdout

    def _curl_command(self, header_fd: int) -> List[str]:
        timeout = "%g" % self.timeout_seconds
        return [
            "/usr/bin/curl",
            "--disable",
            "--silent",
            "--show-error",
            "--fail",
            "--proto",
            "=https",
            "--request",
            "POST",
            "--max-time",
            timeout,
            "--connect-timeout",
            timeout,
            "--max-filesize",
            str(_ROUTING_RESPONSE_MAX_BYTES),
            "--header",
            "@/dev/fd/%d" % header_fd,
            "--data-binary",
            "@-",
            _OPENAI_RESPONSES_ENDPOINT,
        ]

    @staticmethod
    def _sanitized_environment() -> dict:
        return {
            name: value
            for name, value in os.environ.items()
            if name in _CURL_ENVIRONMENT_WHITELIST
        }

    def _active_process_snapshot(self) -> Tuple[object, ...]:
        with self._active_processes_lock:
            return tuple(self._active_processes)

    def _cancel_active_processes(self, method_name: str) -> None:
        with self._cancellation_gate.lock:
            for process in self._active_process_snapshot():
                try:
                    if process.poll() is None:
                        getattr(process, method_name)()
                except Exception:
                    continue

    @staticmethod
    def _terminate_process(process) -> None:
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            return

    @staticmethod
    def _kill_process(process) -> None:
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            return

    def _read_api_key(self) -> str:
        try:
            path_stat = os.lstat(self._auth_path)
            if not stat.S_ISREG(path_stat.st_mode):
                raise RuntimeError("unsafe auth file")
            descriptor = os.open(
                os.fspath(self._auth_path),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                file_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_uid != os.getuid()
                    or stat.S_IMODE(file_stat.st_mode) != 0o600
                ):
                    raise RuntimeError("unsafe auth file")
                with os.fdopen(descriptor, "r", encoding="utf-8") as auth_file:
                    descriptor = None
                    auth_data = json.load(auth_file)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raise RuntimeError("unable to read routing auth") from None

        if (
            not isinstance(auth_data, dict)
            or auth_data.get("auth_mode") not in {"apikey", "api_key"}
        ):
            raise RuntimeError("invalid routing auth")
        api_key = auth_data.get("OPENAI_API_KEY")
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or len(api_key) > _OPENAI_API_KEY_MAX_LENGTH
            or any(character in api_key for character in ("\r", "\n", "\x00"))
        ):
            raise RuntimeError("invalid routing auth")
        return api_key.strip()

    @staticmethod
    def _response_format() -> dict:
        return {
            "type": "json_schema",
            "name": "routing_classifier",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "level",
                    "confidence",
                    "task_kind",
                    "reasons",
                    "brainstorm_required",
                ],
                "properties": {
                    "level": {"type": "string", "enum": ["L1", "L2", "L3"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "task_kind": {
                        "type": "string",
                        "enum": list(TASK_KINDS),
                    },
                    "reasons": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string", "maxLength": 64},
                    },
                    "brainstorm_required": {"type": "boolean"},
                },
            },
        }

    @staticmethod
    def _parse_response(data: object) -> Tuple[str, str, int, int]:
        if (
            not isinstance(data, dict)
            or data.get("object") != "response"
            or data.get("status") != "completed"
        ):
            raise ValueError("invalid OpenAI Responses envelope")
        model = _optional_model_identifier(data.get("model"))
        output = data.get("output")
        usage = data.get("usage")
        if model is None or not isinstance(output, list) or not isinstance(usage, dict):
            raise ValueError("invalid OpenAI Responses envelope")

        output_texts = []
        for item in output:
            if not isinstance(item, dict):
                raise ValueError("invalid OpenAI Responses envelope")
            if item.get("type") != "message":
                continue
            if item.get("status") != "completed":
                raise ValueError("invalid OpenAI Responses envelope")
            content = item.get("content")
            if not isinstance(content, list):
                raise ValueError("invalid OpenAI Responses envelope")
            for part in content:
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "output_text"
                    or not isinstance(part.get("text"), str)
                    or not part["text"].strip()
                ):
                    raise ValueError("invalid OpenAI Responses envelope")
                output_texts.append(part["text"])

        if len(output_texts) != 1:
            raise ValueError("invalid OpenAI Responses envelope")
        input_tokens = _optional_nonnegative_int(usage.get("input_tokens"), MAX_USAGE_TOKENS)
        output_tokens = _optional_nonnegative_int(usage.get("output_tokens"), MAX_USAGE_TOKENS)
        if input_tokens is None or output_tokens is None:
            raise ValueError("invalid OpenAI Responses envelope")
        return output_texts[0], model, input_tokens, output_tokens


def _select_reply_owner(data: object) -> Optional[Tuple[str, Dict]]:
    if isinstance(data, dict):
        for field_name in ("reply", "text", "message", "content", "output", "response"):
            value = data.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip(), data

        for field_name in ("result", "data"):
            nested = data.get(field_name)
            candidate = _select_reply_owner(nested)
            if candidate:
                return candidate

        payloads = data.get("payloads")
        candidate = _select_reply_owner(payloads)
        if candidate:
            if isinstance(data.get("meta"), dict):
                return candidate[0], data
            return candidate

    if isinstance(data, list):
        for item in reversed(data):
            candidate = _select_reply_owner(item)
            if candidate:
                return candidate

    return None


def _extract_reply_from_json(data: object) -> Optional[str]:
    candidate = _select_reply_owner(data)
    if candidate:
        return candidate[0]
    return None


def _optional_model_identifier(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if (
        value
        and len(value) <= MAX_MODEL_IDENTIFIER_LENGTH
        and _MODEL_IDENTIFIER_PATTERN.fullmatch(value)
    ):
        return value
    return None


def _optional_nonnegative_int(value: object, maximum: int) -> Optional[int]:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= maximum
    ):
        return value
    return None


def _extract_response_from_json(data: object) -> Optional[AgentResponse]:
    candidate = _select_reply_owner(data)
    if candidate is None:
        return None
    reply, owner = candidate

    provider = None
    model = None
    input_tokens = None
    output_tokens = None
    duration_ms = None
    if isinstance(owner, dict):
        meta = owner.get("meta")
        if isinstance(meta, dict):
            duration_ms = _optional_nonnegative_int(
                meta.get("durationMs"), MAX_DURATION_MS
            )
            agent_meta = meta.get("agentMeta")
            if isinstance(agent_meta, dict):
                provider = _optional_model_identifier(agent_meta.get("provider"))
                model = _optional_model_identifier(agent_meta.get("model"))
                usage = agent_meta.get("usage")
                if isinstance(usage, dict):
                    input_tokens = _optional_nonnegative_int(
                        usage.get("input"), MAX_USAGE_TOKENS
                    )
                    output_tokens = _optional_nonnegative_int(
                        usage.get("output"), MAX_USAGE_TOKENS
                    )

    return AgentResponse(
        text=reply,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )


def extract_openclaw_response_from_stdout(
    stdout: str, require_json: bool = False
) -> AgentResponse:
    """Extract the selected OpenClaw JSON envelope without invoking a process."""
    decoder = json.JSONDecoder()
    json_values = []
    index = 0
    while index < len(stdout):
        char = stdout[index]
        if char not in "{[":
            index += 1
            continue
        try:
            data, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        json_values.append(data)
        index += end

    for data in reversed(json_values):
        response = _extract_response_from_json(data)
        if response is not None:
            return response

    text = stdout.strip()
    if text and not require_json:
        return AgentResponse(text=text)

    raise RuntimeError("OpenClaw CLI did not return a JSON reply")


def _extract_response_from_stdout(stdout: str) -> AgentResponse:
    return extract_openclaw_response_from_stdout(stdout)


def _extract_reply_from_stdout(stdout: str) -> str:
    return _extract_response_from_stdout(stdout).text


class OpenClawCliAgentAdapter:
    def __init__(
        self,
        command: str,
        timeout_seconds: int = 120,
        model: Optional[str] = None,
        agent_id: Optional[str] = "main",
        session_prefix: str = "agent:main:qq",
        use_gateway: bool = True,
        runner: Callable = None,
        cancellable: bool = False,
        popen_factory: Callable = None,
        cancellation_gate: Optional[CancellationGate] = None,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.agent_id = agent_id
        self.session_prefix = session_prefix
        self.use_gateway = use_gateway
        self._runner = subprocess.run if runner is None else runner
        self._cancellable = cancellable
        self._popen_factory = subprocess.Popen if popen_factory is None else popen_factory
        self._active_processes: Set[object] = set()
        self._active_processes_lock = threading.Lock()
        self._cancellation_gate = cancellation_gate or CancellationGate()

    def reply(self, request_data: AgentRequest) -> AgentResponse:
        command = self._build_command(request_data)
        if self._cancellable:
            result = self._run_owned_process(command)
        else:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "OpenClaw CLI failed").strip()
            raise RuntimeError(detail)

        stderr = (result.stderr or "").strip()
        if self.use_gateway and "EMBEDDED FALLBACK" in stderr:
            raise RuntimeError(
                "OpenClaw Gateway call fell back to embedded mode: %s" % stderr
            )

        return _extract_response_from_stdout(result.stdout)

    def cancel_active(self) -> None:
        self._cancel_active_processes("terminate")

    def force_cancel_active(self) -> None:
        self._cancel_active_processes("kill")

    def bind_cancellation_gate(self, cancellation_gate: CancellationGate) -> None:
        self._cancellation_gate = cancellation_gate

    def _run_owned_process(self, command: List[str]):
        with self._cancellation_gate.lock:
            if self._cancellation_gate.is_closed():
                raise RuntimeError("routing classifier is shutting down")
            process = self._popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self._active_processes_lock:
                self._active_processes.add(process)
        try:
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                process.communicate()
                raise
            return subprocess.CompletedProcess(
                command, process.returncode, stdout=stdout, stderr=stderr
            )
        finally:
            with self._active_processes_lock:
                self._active_processes.discard(process)

    def _active_process_snapshot(self) -> Tuple[object, ...]:
        with self._active_processes_lock:
            return tuple(self._active_processes)

    def _cancel_active_processes(self, method_name: str) -> None:
        with self._cancellation_gate.lock:
            for process in self._active_process_snapshot():
                try:
                    if process.poll() is None:
                        getattr(process, method_name)()
                except Exception:
                    continue

    @staticmethod
    def _terminate_process(process) -> None:
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            return

    def _build_command(self, request_data: AgentRequest) -> List[str]:
        command = shlex.split(self.command)
        command.append("agent")
        if not self.use_gateway:
            command.append("--local")
        if self.agent_id:
            command.extend(["--agent", self.agent_id])
        message = request_data.text
        contexts = [request_data.context.strip(), request_data.media_context.strip()]
        contexts = [context for context in contexts if context]
        if contexts:
            message = "%s\n\n【用户原始消息】\n%s" % (
                "\n\n".join(contexts), request_data.text
            )
        command.extend(
            [
                "--json",
                "--message",
                message,
                "--session-key",
                request_data.session_key
                or "%s-%s" % (self.session_prefix, request_data.user_id),
                "--timeout",
                str(self.timeout_seconds),
            ]
        )
        if self.model:
            command.extend(["--model", self.model])
        return command


def create_agent_adapter(config: Config) -> AgentAdapter:
    if config.agent_adapter == "echo":
        return EchoAgentAdapter()

    if config.agent_adapter == "http":
        if not config.openclaw_endpoint:
            raise ValueError("OPENCLAW_ENDPOINT is required when AGENT_ADAPTER=http")
        return HttpAgentAdapter(config.openclaw_endpoint)

    if config.agent_adapter == "openclaw-cli":
        return OpenClawCliAgentAdapter(
            config.openclaw_command,
            timeout_seconds=config.openclaw_timeout_seconds,
            model=config.openclaw_model,
            agent_id=config.openclaw_agent_id,
            session_prefix=config.openclaw_session_prefix,
            use_gateway=config.openclaw_use_gateway,
        )

    raise ValueError("AGENT_ADAPTER must be echo, http, or openclaw-cli")
