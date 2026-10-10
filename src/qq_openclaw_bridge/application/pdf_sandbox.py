from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Callable, Optional, Sequence


if __package__:
    from ..domain.media import MediaRejected
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from qq_openclaw_bridge.domain.media import MediaRejected


MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_EXTRACT_CHARS = 200000
MAX_STDOUT_BYTES = 1024 * 1024
MAX_PDF_PAGES = 100
WALL_TIMEOUT_SECONDS = 8.0
WORKER_CPU_SECONDS = 5
WORKER_MEMORY_BYTES = 512 * 1024 * 1024
WORKER_NOFILE = 32


@dataclass(frozen=True)
class _WorkerResult:
    returncode: int
    stdout: bytes


def _run_worker(
    command: Sequence[str],
    snapshot: bytes,
    timeout: float,
    stdout_limit: int,
) -> _WorkerResult:
    with tempfile.TemporaryFile(mode="w+b") as output:
        process = subprocess.Popen(
            tuple(command),
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
        )
        try:
            process.communicate(input=snapshot, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            finally:
                process.communicate()
            raise

        size = os.fstat(output.fileno()).st_size
        output.seek(0)
        stdout = output.read(min(size, stdout_limit + 1))
        return _WorkerResult(returncode=process.returncode, stdout=stdout)


class PdfSandbox:
    def __init__(
        self,
        runner: Optional[
            Callable[[Sequence[str], bytes, float, int], _WorkerResult]
        ] = None,
    ) -> None:
        self._runner = runner or _run_worker

    def validate(self, snapshot: bytes) -> None:
        self._request(snapshot, "validate", 0)

    def extract(self, snapshot: bytes, max_chars: int) -> str:
        effective_max_chars = min(max(0, int(max_chars)), MAX_EXTRACT_CHARS)
        payload = self._request(snapshot, "extract", effective_max_chars)
        text = payload["text"]
        if len(text) > effective_max_chars + 1:
            raise MediaRejected("PDF sandbox rejected document")
        return text

    def _request(self, snapshot: bytes, operation: str, max_chars: int):
        if type(snapshot) is not bytes:
            raise MediaRejected("PDF snapshot must be immutable bytes")
        if len(snapshot) > MAX_INPUT_BYTES:
            raise MediaRejected("PDF exceeds the 20 MiB input limit")

        command = (
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            operation,
            str(max_chars),
        )
        try:
            result = self._runner(
                command,
                snapshot,
                WALL_TIMEOUT_SECONDS,
                MAX_STDOUT_BYTES,
            )
            returncode = result.returncode
            stdout = result.stdout
        except Exception as error:
            raise MediaRejected("PDF sandbox rejected document") from error

        if type(returncode) is not int or returncode != 0:
            raise MediaRejected("PDF sandbox rejected document")
        if type(stdout) is not bytes or len(stdout) > MAX_STDOUT_BYTES:
            raise MediaRejected("PDF sandbox rejected document")
        try:
            decoded = stdout.decode("utf-8", "strict")
            payload = json.loads(decoded)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise MediaRejected("PDF sandbox rejected document") from error
        if type(payload) is not dict or type(payload.get("ok")) is not bool:
            raise MediaRejected("PDF sandbox rejected document")
        if payload["ok"] is False:
            if set(payload) != {"ok", "error"} or type(payload["error"]) is not str:
                raise MediaRejected("PDF sandbox rejected document")
            raise MediaRejected("PDF sandbox rejected document")
        if operation == "validate":
            if set(payload) != {"ok"}:
                raise MediaRejected("PDF sandbox rejected document")
        elif set(payload) != {"ok", "text"} or type(payload["text"]) is not str:
            raise MediaRejected("PDF sandbox rejected document")
        return payload


def _set_one_limit(resource_module, limit_name: str, value: int) -> bool:
    limit = getattr(resource_module, limit_name, None)
    if limit is None:
        return False
    _, current_hard = resource_module.getrlimit(limit)
    infinity = getattr(resource_module, "RLIM_INFINITY", -1)
    effective = value
    if current_hard != infinity:
        effective = min(effective, current_hard)
    resource_module.setrlimit(limit, (effective, current_hard))
    return True


def _mac_virtual_size() -> int:
    import ctypes

    class TimeValue(ctypes.Structure):
        _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]

    class TaskBasicInfo64(ctypes.Structure):
        _fields_ = [
            ("virtual_size", ctypes.c_uint64),
            ("resident_size", ctypes.c_uint64),
            ("resident_size_max", ctypes.c_uint64),
            ("user_time", TimeValue),
            ("system_time", TimeValue),
            ("policy", ctypes.c_int),
            ("suspend_count", ctypes.c_int),
        ]

    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.task_info.argtypes = (
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
    )
    info = TaskBasicInfo64()
    count = ctypes.c_uint(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_int))
    task_self = ctypes.c_uint.in_dll(library, "mach_task_self_").value
    result = library.task_info(
        task_self,
        20,
        ctypes.cast(ctypes.byref(info), ctypes.POINTER(ctypes.c_int)),
        ctypes.byref(count),
    )
    if result != 0 or info.virtual_size <= 0:
        raise OSError("cannot read macOS task memory size")
    return int(info.virtual_size)


def _set_worker_limits(resource_module=None) -> None:
    if resource_module is None:
        import resource as resource_module

    if not _set_one_limit(resource_module, "RLIMIT_CPU", WORKER_CPU_SECONDS):
        raise RuntimeError("CPU resource limit is unavailable")

    memory_limited = False
    for name in ("RLIMIT_AS", "RLIMIT_DATA", "RLIMIT_RSS"):
        try:
            if _set_one_limit(resource_module, name, WORKER_MEMORY_BYTES):
                memory_limited = True
                break
        except (OSError, ValueError):
            if name != "RLIMIT_AS" or sys.platform != "darwin":
                continue
            mac_limit = _mac_virtual_size() + WORKER_MEMORY_BYTES
            if _set_one_limit(resource_module, name, mac_limit):
                memory_limited = True
                break
    if not memory_limited:
        raise RuntimeError("memory resource limit is unavailable")

    if not _set_one_limit(resource_module, "RLIMIT_FSIZE", MAX_STDOUT_BYTES):
        raise RuntimeError("file-size resource limit is unavailable")
    if not _set_one_limit(resource_module, "RLIMIT_NOFILE", WORKER_NOFILE):
        raise RuntimeError("file-descriptor resource limit is unavailable")
    _set_one_limit(resource_module, "RLIMIT_CORE", 0)


def _load_pypdf():
    import pypdf

    try:
        major = int(str(pypdf.__version__).split(".", 1)[0])
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeError("unsupported pypdf version") from error
    if major < 4 or major >= 7:
        raise RuntimeError("unsupported pypdf version")
    return pypdf.PdfReader


def _read_bounded_pages(reader):
    pages = reader.pages
    count = len(pages)
    if type(count) is not int or count < 0 or count > MAX_PDF_PAGES:
        raise ValueError("PDF page limit exceeded")
    bounded = []
    for index in range(count):
        page = pages[index]
        page.get("/Type")
        page.get("/Resources")
        page.mediabox
        contents = page.get_contents()
        if contents is not None:
            contents.get_data()
        bounded.append(page)
    return bounded


def _parse_pdf(reader_type, snapshot: bytes):
    reader = reader_type(io.BytesIO(snapshot), strict=True)
    return _read_bounded_pages(reader)


def _extract_pdf_text(reader_type, snapshot: bytes, max_chars: int) -> str:
    pages = _parse_pdf(reader_type, snapshot)
    cap = max_chars + 1
    parts = []
    length = 0
    for index, page in enumerate(pages):
        if index:
            if length >= cap:
                break
            parts.append("\n")
            length += 1
        if length >= cap:
            break
        text = page.extract_text() or ""
        if type(text) is not str:
            raise ValueError("invalid PDF text")
        piece = text[: cap - length]
        parts.append(piece)
        length += len(piece)
    return "".join(parts)


def _write_worker_payload(output_stream, payload) -> None:
    output_stream.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    output_stream.flush()


def _worker_main(
    arguments,
    input_stream=None,
    output_stream=None,
    limit_setter=None,
    pdf_loader=None,
) -> int:
    source = input_stream or sys.stdin.buffer
    destination = output_stream or sys.stdout
    set_limits = limit_setter or _set_worker_limits
    load_pdf = pdf_loader or _load_pypdf
    try:
        set_limits()
        if len(arguments) != 2 or arguments[0] not in ("validate", "extract"):
            raise ValueError("invalid worker command")
        operation = arguments[0]
        max_chars = int(arguments[1])
        if max_chars < 0 or max_chars > MAX_EXTRACT_CHARS:
            raise ValueError("invalid character limit")
        snapshot = source.read(MAX_INPUT_BYTES + 1)
        if type(snapshot) is not bytes or len(snapshot) > MAX_INPUT_BYTES:
            raise ValueError("invalid PDF input")
        reader_type = load_pdf()
        if operation == "validate":
            _parse_pdf(reader_type, snapshot)
            payload = {"ok": True}
        else:
            payload = {
                "ok": True,
                "text": _extract_pdf_text(reader_type, snapshot, max_chars),
            }
    except (Exception, MemoryError):
        payload = {"ok": False, "error": "rejected"}
    try:
        _write_worker_payload(destination, payload)
    except (Exception, MemoryError):
        return 1
    return 0


def _main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "--worker":
        return 2
    return _worker_main(tuple(sys.argv[2:]))


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["PdfSandbox"]
