from dataclasses import dataclass
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
from typing import Callable, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit


if __package__:
    from ..domain.media import MediaRejected
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from qq_openclaw_bridge.domain.media import MediaRejected


CHUNK_SIZE = 64 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_HEADER_BYTES = 64 * 1024 + 1
WORKER_NOFILE = 32
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class RemoteDownloadResult:
    body: bytes
    content_type: Optional[str]


@dataclass(frozen=True)
class _WorkerResult:
    returncode: int
    stdout: bytes


def _reject() -> MediaRejected:
    return MediaRejected("remote download sandbox rejected media")


def _json_loads_strict(value: bytes):
    def reject_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    return json.loads(value.decode("utf-8", "strict"), object_pairs_hook=reject_duplicates)


def _normalize_remote_host(value: str) -> str:
    if type(value) is not str:
        raise ValueError("remote host must be a string")
    try:
        host = value.encode("idna").decode("ascii").rstrip(".").casefold()
    except UnicodeError as error:
        raise ValueError("remote host is invalid") from error
    if not host or "*" in host or any(not label for label in host.split(".")):
        raise ValueError("remote host must be an exact hostname")
    return host


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_peer: str, port=443, timeout=None, context=None):
        self._pinned_peer = pinned_peer
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=context or ssl.create_default_context(),
        )

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_peer, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPSResponse:
    def __init__(self, response, connection) -> None:
        self._response = response
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._response, name)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _default_opener(url: str, pinned_peer: str):
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise ValueError("HTTPS URL has no host")
    connection = _PinnedHTTPSConnection(
        host,
        pinned_peer,
        port=443,
        timeout=30,
        context=ssl.create_default_context(),
    )
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    host_header = "[%s]" % host if ":" in host else host
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Host": host_header,
            },
        )
        return _PinnedHTTPSResponse(connection.getresponse(), connection)
    except Exception:
        connection.close()
        raise


def _default_resolver(host: str) -> Tuple[str, ...]:
    addresses = []
    for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        address = item[4][0]
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _run_worker(
    command: Sequence[str], payload: bytes, timeout: float, output_limit: int
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
            process.communicate(input=payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            finally:
                process.communicate()
            raise
        size = os.fstat(output.fileno()).st_size
        output.seek(0)
        return _WorkerResult(
            returncode=process.returncode,
            stdout=output.read(min(size, output_limit + 1)),
        )


class RemoteDownloadSandbox:
    def __init__(
        self,
        runner: Optional[Callable[[Sequence[str], bytes, float, int], _WorkerResult]] = None,
    ) -> None:
        self._runner = runner or _run_worker

    def fetch(
        self,
        url: str,
        allowed_hosts: Sequence[str],
        max_redirects: int,
        byte_limit: int,
        timeout_seconds: float = 60,
    ) -> RemoteDownloadResult:
        try:
            if type(url) is not str or type(byte_limit) is not int or byte_limit < 0:
                raise ValueError("invalid request")
            if type(max_redirects) is not int or max_redirects < 0:
                raise ValueError("invalid request")
            request = {
                "url": url,
                "allowed_hosts": list(allowed_hosts),
                "max_redirects": max_redirects,
                "byte_limit": byte_limit,
            }
            if any(type(host) is not str for host in request["allowed_hosts"]):
                raise ValueError("invalid host")
            payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            if len(payload) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            timeout = float(timeout_seconds)
            if timeout <= 0:
                raise ValueError("invalid timeout")
        except Exception as error:
            raise _reject() from error

        command = (sys.executable, str(Path(__file__).resolve()), "--worker")
        try:
            result = self._runner(command, payload, timeout, byte_limit + MAX_HEADER_BYTES)
        except Exception as error:
            raise _reject() from error
        return self._decode_result(result, byte_limit)

    @staticmethod
    def _is_exact_rejection(header, body: bytes) -> bool:
        return (
            type(header) is dict
            and set(header) == {"ok", "error"}
            and header.get("ok") is False
            and header.get("error") == "rejected"
            and body == b""
        )

    @staticmethod
    def _decode_result(result, byte_limit: int) -> RemoteDownloadResult:
        if type(getattr(result, "returncode", None)) is not int or result.returncode != 0:
            raise _reject()
        output = getattr(result, "stdout", None)
        if type(output) is not bytes or len(output) > byte_limit + MAX_HEADER_BYTES:
            raise _reject()
        if len(output) < 4:
            raise _reject()
        header_size = int.from_bytes(output[:4], "big")
        if header_size > MAX_HEADER_BYTES - 1 or len(output) < 4 + header_size:
            raise _reject()
        try:
            header = _json_loads_strict(output[4 : 4 + header_size])
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise _reject() from error
        body = output[4 + header_size :]
        if type(header) is not dict or type(header.get("ok")) is not bool:
            raise _reject()
        if header["ok"] is False:
            if not RemoteDownloadSandbox._is_exact_rejection(header, body):
                raise _reject()
            raise _reject()
        if set(header) != {"ok", "content_type", "byte_size"}:
            raise _reject()
        content_type = header["content_type"]
        byte_size = header["byte_size"]
        if content_type is not None and type(content_type) is not str:
            raise _reject()
        if type(byte_size) is not int or byte_size < 0 or byte_size > byte_limit:
            raise _reject()
        if len(body) != byte_size:
            raise _reject()
        return RemoteDownloadResult(body=body, content_type=content_type)


def _set_worker_limits(maximum: int, resource_module=None) -> None:
    if resource_module is None:
        import resource as resource_module

    for name, value in (
        ("RLIMIT_FSIZE", maximum),
        ("RLIMIT_NOFILE", WORKER_NOFILE),
        ("RLIMIT_CORE", 0),
    ):
        limit = getattr(resource_module, name, None)
        if limit is None:
            raise RuntimeError("resource limit unavailable")
        _, hard = resource_module.getrlimit(limit)
        infinity = getattr(resource_module, "RLIM_INFINITY", -1)
        effective = value if hard == infinity else min(value, hard)
        resource_module.setrlimit(limit, (effective, hard))


def _request_from_stream(source) -> Tuple[str, Tuple[str, ...], int, int]:
    raw = source.read(MAX_REQUEST_BYTES + 1)
    if type(raw) is not bytes or len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("invalid input")
    request = _json_loads_strict(raw)
    if type(request) is not dict or set(request) != {
        "url", "allowed_hosts", "max_redirects", "byte_limit"
    }:
        raise ValueError("invalid request")
    url = request["url"]
    hosts = request["allowed_hosts"]
    redirects = request["max_redirects"]
    byte_limit = request["byte_limit"]
    if (
        type(url) is not str
        or type(hosts) is not list
        or type(redirects) is not int
        or redirects < 0
        or type(byte_limit) is not int
        or byte_limit < 0
    ):
        raise ValueError("invalid request")
    return url, tuple(_normalize_remote_host(host) for host in hosts), redirects, byte_limit


def _header(headers, name: str) -> Optional[str]:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(name)
        if value is not None:
            return str(value)
    wanted = name.casefold()
    for key, value in getattr(headers, "items", lambda: ())():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def _validate_url(url: str, allowed_hosts: Sequence[str], resolver) -> Tuple[str, ...]:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https":
        raise ValueError("only HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid URL")
    host = _normalize_remote_host(parsed.hostname)
    if host not in allowed_hosts:
        raise ValueError("host not allowed")
    if parsed.port not in (None, 443):
        raise ValueError("invalid port")
    addresses = tuple(resolver(host))
    if not addresses:
        raise ValueError("no addresses")
    valid = []
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ValueError("private address")
        normalized = str(address)
        if normalized not in valid:
            valid.append(normalized)
    return tuple(valid)


def _download(url: str, hosts: Sequence[str], max_redirects: int, byte_limit: int, resolver, opener):
    current = url
    redirects = 0
    while True:
        addresses = _validate_url(current, hosts, resolver)
        response = opener(current, addresses[0])
        try:
            status = getattr(response, "status", None)
            if status is None:
                getcode = getattr(response, "getcode", None)
                status = getcode() if getcode is not None else 200
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError("invalid status")
            if status in REDIRECT_STATUSES:
                location = _header(getattr(response, "headers", None), "Location")
                if not location or redirects >= max_redirects:
                    raise ValueError("invalid redirect")
                current = urljoin(current, location)
                redirects += 1
                continue
            if status < 200 or status >= 300:
                raise ValueError("HTTP error")
            content_type = _header(getattr(response, "headers", None), "Content-Type")
            chunks = []
            size = 0
            while True:
                chunk = response.read(min(CHUNK_SIZE, byte_limit - size + 1))
                if not chunk:
                    break
                if type(chunk) is not bytes:
                    raise ValueError("invalid bytes")
                size += len(chunk)
                if size > byte_limit:
                    raise ValueError("overflow")
                chunks.append(chunk)
            return b"".join(chunks), content_type
        finally:
            response.close()


def _write_protocol(destination, payload, body=b"") -> None:
    header = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    destination.write(len(header).to_bytes(4, "big"))
    destination.write(header)
    if body:
        destination.write(body)
    destination.flush()


def _worker_main(
    input_stream=None,
    output_stream=None,
    limit_setter=None,
    resolver=None,
    opener=None,
) -> int:
    source = input_stream or sys.stdin.buffer
    destination = output_stream or sys.stdout.buffer
    try:
        url, hosts, redirects, byte_limit = _request_from_stream(source)
        (limit_setter or _set_worker_limits)(byte_limit + MAX_HEADER_BYTES)
        body, content_type = _download(
            url, hosts, redirects, byte_limit, resolver or _default_resolver, opener or _default_opener
        )
        _write_protocol(destination, {"ok": True, "content_type": content_type, "byte_size": len(body)}, body)
    except (Exception, MemoryError):
        try:
            _write_protocol(destination, {"ok": False, "error": "rejected"})
        except (Exception, MemoryError):
            return 1
    return 0


def _main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "--worker":
        return 2
    return _worker_main()


if __name__ == "__main__":
    raise SystemExit(_main())
