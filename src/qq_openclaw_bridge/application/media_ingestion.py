from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import codecs
import errno
import fcntl
import hashlib
import http.client
import io
import ipaddress
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
import threading
import time
from typing import Callable, Iterable, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit
import uuid
import xml.etree.ElementTree as ElementTree
import xml.parsers.expat as expat
import zipfile

from ..domain.media import MediaLimits, MediaRejected, ValidatedMedia
from .pdf_sandbox import PdfSandbox
from .remote_download_sandbox import RemoteDownloadSandbox


CHUNK_SIZE = 64 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_DOCX_ENTRIES = 256
MAX_DOCUMENT_XML_BYTES = 4 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100
CACHE_LOCK_NAME = ".media-cache.lock"
DOCUMENT_MIMES = {
    "application/json",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/csv",
    "text/markdown",
    "text/plain",
}
MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "text/x-markdown": "text/markdown",
}
_CACHE_ROOT_LOCKS = {}
_CACHE_ROOT_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class _WrittenCacheEntry:
    device: int
    inode: int
    size: int
    sha256: str


def _shared_cache_root_lock(identity):
    with _CACHE_ROOT_LOCKS_GUARD:
        lock = _CACHE_ROOT_LOCKS.get(identity)
        if lock is None:
            lock = threading.RLock()
            _CACHE_ROOT_LOCKS[identity] = lock
        return lock


def _open_trusted_directory(value: Path, label: str, create: bool = False):
    path = Path(value).expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(path.anchor, flags)
    try:
        parts = path.parts[1:]
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise MediaRejected("%s must be a directory" % label)
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            except OSError as error:
                try:
                    entry_stat = os.stat(
                        part,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    entry_stat = None
                if entry_stat is not None and stat.S_ISLNK(entry_stat.st_mode):
                    raise MediaRejected(
                        "%s ancestor cannot be a symbolic link" % label
                    ) from error
                raise MediaRejected("%s must be a directory" % label) from error
            os.close(directory_fd)
            directory_fd = next_fd
        return path, directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_cache_lock(directory_fd: int) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = -1
    try:
        lock_fd = os.open(CACHE_LOCK_NAME, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(lock_fd, 0o600)
        opened = os.fstat(lock_fd)
        current = os.stat(
            CACHE_LOCK_NAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise MediaRejected("cache lock file is unsafe")
        return lock_fd
    except MediaRejected:
        if lock_fd >= 0:
            _close_fd(lock_fd)
        raise
    except OSError as error:
        if lock_fd >= 0:
            _close_fd(lock_fd)
        raise MediaRejected("cache lock file is unsafe") from error


def _close_quietly(value) -> None:
    try:
        close = getattr(value, "close", None)
        if close is not None:
            close()
    except Exception:
        pass


def _close_fd(file_descriptor: int) -> None:
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _read_safe_docx_xml(archive: zipfile.ZipFile) -> bytes:
    entries = archive.infolist()
    if len(entries) > MAX_DOCX_ENTRIES:
        raise MediaRejected("DOCX ZIP entry limit exceeded")
    matches = [item for item in entries if item.filename == "word/document.xml"]
    if not matches:
        raise MediaRejected("DOCX is missing word/document.xml")
    if len(matches) != 1:
        raise MediaRejected("DOCX has duplicate word/document.xml entries")

    info = matches[0]
    if info.flag_bits & 0x1:
        raise MediaRejected("DOCX word/document.xml must be readable")
    if info.file_size > MAX_DOCUMENT_XML_BYTES:
        raise MediaRejected("DOCX document XML is too large")
    if (
        info.file_size > 1024
        and info.file_size
        > max(1, info.compress_size) * MAX_DOCX_COMPRESSION_RATIO
    ):
        raise MediaRejected("DOCX document XML compression ratio is too high")

    try:
        with archive.open(info, "r") as source:
            chunks = []
            total = 0
            while True:
                chunk = source.read(min(CHUNK_SIZE, MAX_DOCUMENT_XML_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOCUMENT_XML_BYTES:
                    raise MediaRejected("DOCX document XML is too large")
                chunks.append(chunk)
    except MediaRejected:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise MediaRejected("DOCX word/document.xml is unreadable") from error

    document_xml = b"".join(chunks)
    upper_xml = document_xml.upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise MediaRejected("DOCX XML cannot contain DTD or ENTITY declarations")
    encoding = None
    if document_xml.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif document_xml.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif document_xml.startswith(b"\x00\x00\x00<"):
        encoding = "utf-32-be"
    elif document_xml.startswith(b"<\x00\x00\x00"):
        encoding = "utf-32-le"
    elif document_xml.startswith(b"\x00<\x00?"):
        encoding = "utf-16-be"
    elif document_xml.startswith(b"<\x00?\x00"):
        encoding = "utf-16-le"
    if encoding is not None:
        try:
            decoded_xml = document_xml.decode(encoding).upper()
        except UnicodeError:
            decoded_xml = ""
        if "<!DOCTYPE" in decoded_xml or "<!ENTITY" in decoded_xml:
            raise MediaRejected(
                "DOCX XML cannot contain DTD or ENTITY declarations"
            )
    parser = expat.ParserCreate()

    def reject_declaration(*args):
        raise MediaRejected("DOCX XML cannot contain DTD or ENTITY declarations")

    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    try:
        parser.Parse(document_xml, True)
    except MediaRejected:
        raise
    except expat.ExpatError as error:
        raise MediaRejected("invalid DOCX document") from error
    return document_xml


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        pinned_peer: str,
        port: int = 443,
        timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
        context=None,
    ) -> None:
        self._pinned_peer = pinned_peer
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=context or ssl.create_default_context(),
        )

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_peer, self.port),
            self.timeout,
            self.source_address,
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


def _normal_mime(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    mime = value.split(";", 1)[0].strip().casefold()
    return MIME_ALIASES.get(mime, mime)


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


def _header(headers, name: str) -> Optional[str]:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(name)
        if value is not None:
            return str(value)
    target = name.casefold()
    for key, value in getattr(headers, "items", lambda: ())():
        if str(key).casefold() == target:
            return str(value)
    return None


def _detected_mime(prefix: bytes) -> Optional[str]:
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"PK\x03\x04"):
        return "application/zip"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio/wav"
    if prefix.startswith(b"OggS"):
        return "audio/ogg"
    if prefix.startswith(b"fLaC"):
        return "audio/flac"
    if prefix.startswith(b"#!AMR"):
        return "audio/amr"
    if prefix.startswith(b"ID3") or (
        len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
    ):
        return "audio/mpeg"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "audio/mp4"
    return None


def _mimes_compatible(expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    return {expected, actual} == {
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }


class NapCatLocalMediaSource:
    def __init__(self, roots: Sequence[Path]) -> None:
        if not roots:
            raise ValueError("at least one NapCat root is required")
        checked = []
        try:
            for value in roots:
                checked.append(
                    _open_trusted_directory(Path(value), "NapCat root")
                )
        except Exception:
            for _, directory_fd in checked:
                os.close(directory_fd)
            raise
        self._roots = tuple(checked)

    def close(self) -> None:
        roots = self._roots
        self._roots = ()
        for _, directory_fd in roots:
            _close_fd(directory_fd)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def open(self, value: str):
        path = Path(value)
        errors = []
        for root, root_fd in self._roots:
            try:
                relative = path.relative_to(root) if path.is_absolute() else path
            except ValueError:
                errors.append("outside")
                continue
            if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
                errors.append("outside")
                continue
            try:
                return self._open_relative(root_fd, relative.parts)
            except FileNotFoundError:
                errors.append("missing")
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise MediaRejected("NapCat path contains a symbolic link") from error
                errors.append("invalid")
        if "outside" in errors:
            raise MediaRejected("NapCat path is outside allowed roots")
        raise MediaRejected("NapCat local file is unavailable")

    @staticmethod
    def _open_relative(root_fd: int, parts: Iterable[str]):
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.dup(root_fd)
        try:
            parts_tuple = tuple(parts)
            for part in parts_tuple[:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(
                parts_tuple[-1], os.O_RDONLY | nofollow, dir_fd=directory_fd
            )
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(file_fd)
                raise MediaRejected("NapCat path is not a regular file")
            if file_stat.st_nlink != 1:
                os.close(file_fd)
                raise MediaRejected("NapCat path cannot be a hard link")
            return os.fdopen(file_fd, "rb")
        finally:
            os.close(directory_fd)


class MediaIngestionService:
    def __init__(
        self,
        cache_root: Path,
        repository,
        resolver: Optional[Callable[[str], Sequence[str]]] = None,
        opener: Optional[Callable[[str], object]] = None,
        limits: Optional[MediaLimits] = None,
        local_source: Optional[NapCatLocalMediaSource] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        max_redirects: int = 3,
        ttl_seconds: int = 3600,
        pdf_sandbox: Optional[PdfSandbox] = None,
        allowed_remote_hosts: Sequence[str] = (),
        download_timeout_seconds: float = 60,
        monotonic_clock: Optional[Callable[[], float]] = None,
        remote_download_sandbox: Optional[RemoteDownloadSandbox] = None,
    ) -> None:
        has_resolver = resolver is not None
        has_opener = opener is not None
        if has_resolver != has_opener:
            raise ValueError("resolver and opener must be provided together")
        if remote_download_sandbox is not None and (has_resolver or has_opener):
            raise ValueError(
                "remote_download_sandbox cannot be combined with resolver or opener"
            )
        self._use_remote_download_sandbox = not (has_resolver and has_opener)
        self._resolver = resolver if not self._use_remote_download_sandbox else None
        self._opener = opener if not self._use_remote_download_sandbox else None
        self._remote_download_sandbox = (
            remote_download_sandbox
            if remote_download_sandbox is not None
            else (RemoteDownloadSandbox() if self._use_remote_download_sandbox else None)
        )
        self._cache_root, self._cache_dir_fd = _open_trusted_directory(
            Path(cache_root), "cache root", create=True
        )
        self._cache_lock_fd = -1
        try:
            os.fchmod(self._cache_dir_fd, 0o700)
            cache_stat = os.fstat(self._cache_dir_fd)
            self._cache_root_identity = (cache_stat.st_dev, cache_stat.st_ino)
            self._cache_lock_fd = _open_cache_lock(self._cache_dir_fd)
        except Exception:
            _close_fd(self._cache_dir_fd)
            self._cache_dir_fd = -1
            raise
        self._operation_lock = _shared_cache_root_lock(self._cache_root_identity)
        self._repository = repository
        self._limits = limits or MediaLimits()
        self._local_source = local_source
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._max_redirects = max(0, int(max_redirects))
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._pdf_sandbox = pdf_sandbox or PdfSandbox()
        self._allowed_remote_hosts = frozenset(
            _normalize_remote_host(host) for host in allowed_remote_hosts
        )
        self._download_timeout_seconds = max(0.0, float(download_timeout_seconds))
        self._monotonic_clock = monotonic_clock or time.monotonic

    def close(self) -> None:
        with self._operation_lock:
            lock_fd = self._cache_lock_fd
            self._cache_lock_fd = -1
            directory_fd = self._cache_dir_fd
            self._cache_dir_fd = -1
            if lock_fd >= 0:
                _close_fd(lock_fd)
            if directory_fd >= 0:
                _close_fd(directory_fd)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def _cache_operation(self):
        with self._operation_lock:
            lock_fd = self._cache_lock_fd
            if lock_fd < 0:
                raise MediaRejected("cache service is closed")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as error:
                raise MediaRejected("cache lock is unavailable") from error
            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    def fetch(self, ref, user_id: int, message_id: str) -> ValidatedMedia:
        limit = self._limits.for_kind(ref.kind)
        if ref.declared_size is not None and int(ref.declared_size) > limit:
            raise MediaRejected("media exceeds size limit")
        response = None
        content_type = None
        deadline = None
        use_remote = False
        if self._local_source is not None and ref.file_id:
            try:
                response = self._local_source.open(ref.file_id)
            except MediaRejected:
                if not self._remote_fallback_is_allowed(ref.url):
                    raise
                use_remote = True
        elif ref.url:
            use_remote = True
        else:
            raise MediaRejected("media source is unavailable")

        if use_remote:
            if self._use_remote_download_sandbox:
                remote = self._remote_download_sandbox.fetch(
                    ref.url,
                    tuple(self._allowed_remote_hosts),
                    self._max_redirects,
                    limit,
                    timeout_seconds=self._download_timeout_seconds,
                )
                if type(remote.body) is not bytes or (
                    remote.content_type is not None
                    and type(remote.content_type) is not str
                ):
                    raise MediaRejected(
                        "remote download sandbox rejected media"
                    )
                response = io.BytesIO(remote.body)
                content_type = remote.content_type
            else:
                deadline = (
                    self._monotonic_clock() + self._download_timeout_seconds
                )
                response, content_type = self._open_remote(ref.url, deadline)

        try:
            with self._cache_operation():
                self._require_cache_root_identity()
                temporary_name = ".tmp-" + self._new_cache_name()
                final_name = None
                written = None
                try:
                    written, prefix = self._copy_limited(
                        response, temporary_name, limit,
                        deadline if use_remote else None,
                    )
                    mime = self._validate_mime(
                        ref.kind, ref.declared_mime, content_type, prefix
                    )
                    final_name = self._new_cache_name()
                    os.rename(
                        temporary_name,
                        final_name,
                        src_dir_fd=self._cache_dir_fd,
                        dst_dir_fd=self._cache_dir_fd,
                    )
                    temporary_name = None

                    self._open_verified_cache_snapshot(final_name, written)
                    self._require_cache_root_identity()
                    snapshot = self._open_verified_cache_snapshot(final_name, written)
                    self._validate_cached_document(mime, snapshot)

                    now = self._aware_now()
                    expires_at = now + timedelta(seconds=self._ttl_seconds)
                    asset_id = uuid.uuid4().hex
                    cache_path = self._cache_root / final_name
                    row = self._repository.create_media_asset(
                        user_id=int(user_id),
                        message_id=str(message_id),
                        kind=ref.kind,
                        display_name=ref.name or "",
                        mime=mime,
                        byte_size=written.size,
                        sha256=written.sha256,
                        status="ready",
                        expires_at=expires_at,
                        now=now,
                        asset_id=asset_id,
                        cache_path=str(cache_path),
                    )
                    try:
                        self._require_cache_root_identity()
                        self._open_verified_cache_snapshot(final_name, written)
                    except Exception:
                        self._repository.mark_media_expired(
                            asset_id, "cache publication invalidated"
                        )
                        raise
                except Exception:
                    if temporary_name is not None:
                        self._unlink_expected_cache_entry_quietly(
                            temporary_name, written
                        )
                    if final_name is not None:
                        self._unlink_expected_cache_entry_quietly(final_name, written)
                    raise
        finally:
            _close_quietly(response)

        return ValidatedMedia(
            asset_id=str(row["id"]),
            path=cache_path,
            kind=ref.kind,
            mime=mime,
            size=written.size,
            sha256=written.sha256,
            expires_at=expires_at,
        )

    def _remote_fallback_is_allowed(self, url: Optional[str]) -> bool:
        if not url:
            return False
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme.casefold() != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
            ):
                return False
            return (
                _normalize_remote_host(parsed.hostname)
                in self._allowed_remote_hosts
            )
        except (ValueError, UnicodeError):
            return False

    def cleanup_expired(self, now: datetime) -> int:
        with self._cache_operation():
            self._require_cache_root_identity()
            removed = 0
            for item in self._repository.list_expired_media(now):
                asset_id = str(item["id"])
                cache_name = self._registered_cache_name(item.get("cache_path"))
                if cache_name is None:
                    self._repository.mark_media_expired(asset_id, "invalid cache path")
                    continue
                if self._cleanup_registered_entry(asset_id, cache_name, now):
                    removed += 1
            return removed

    def _registered_cache_name(self, raw_path) -> Optional[str]:
        if not raw_path:
            return None
        path = Path(str(raw_path))
        if not path.is_absolute():
            return None
        try:
            relative = path.relative_to(self._cache_root)
        except ValueError:
            return None
        if len(relative.parts) != 1 or relative.name in ("", ".", ".."):
            return None
        return relative.name

    def _require_cache_root_identity(self) -> None:
        try:
            held = os.fstat(self._cache_dir_fd)
            if (held.st_dev, held.st_ino) != self._cache_root_identity:
                raise MediaRejected("cache root identity changed")
            _, reopened_fd = _open_trusted_directory(
                self._cache_root, "cache root"
            )
        except Exception as error:
            if isinstance(error, MediaRejected) and str(error) == "cache root identity changed":
                raise
            raise MediaRejected("cache root identity changed") from error
        try:
            reopened = os.fstat(reopened_fd)
            if (reopened.st_dev, reopened.st_ino) != self._cache_root_identity:
                raise MediaRejected("cache root identity changed")
        finally:
            _close_fd(reopened_fd)

    @staticmethod
    def _same_entry(left, right) -> bool:
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino

    def _unlink_expected_cache_entry_quietly(
        self,
        cache_name: str,
        written: Optional[_WrittenCacheEntry],
    ) -> None:
        if written is None:
            return
        file_descriptor = -1
        try:
            file_descriptor = os.open(
                cache_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._cache_dir_fd,
            )
            opened = os.fstat(file_descriptor)
            current = os.stat(
                cache_name,
                dir_fd=self._cache_dir_fd,
                follow_symlinks=False,
            )
            if (
                stat.S_ISREG(opened.st_mode)
                and opened.st_nlink == 1
                and opened.st_dev == written.device
                and opened.st_ino == written.inode
                and self._same_entry(opened, current)
            ):
                os.unlink(cache_name, dir_fd=self._cache_dir_fd)
        except OSError:
            pass
        finally:
            if file_descriptor >= 0:
                _close_fd(file_descriptor)

    def _open_verified_cache_snapshot(
        self, cache_name: str, written: _WrittenCacheEntry
    ) -> bytes:
        file_descriptor = -1
        try:
            file_descriptor = os.open(
                cache_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._cache_dir_fd,
            )
            opened = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != written.device
                or opened.st_ino != written.inode
                or opened.st_size != written.size
            ):
                raise MediaRejected("cache entry identity changed")
            chunks = []
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(file_descriptor, CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > written.size:
                    raise MediaRejected("cache entry size changed")
                digest.update(chunk)
                chunks.append(chunk)
            after = os.fstat(file_descriptor)
            current = os.stat(
                cache_name,
                dir_fd=self._cache_dir_fd,
                follow_symlinks=False,
            )
            if (
                total != written.size
                or digest.hexdigest() != written.sha256
                or not self._same_entry(opened, after)
                or not self._same_entry(opened, current)
                or after.st_size != written.size
                or after.st_nlink != 1
            ):
                raise MediaRejected("cache entry identity changed")
            return b"".join(chunks)
        except MediaRejected:
            raise
        except OSError as error:
            raise MediaRejected("cache entry identity changed") from error
        finally:
            if file_descriptor >= 0:
                _close_fd(file_descriptor)

    def _cleanup_registered_entry(
        self, asset_id: str, cache_name: str, now: datetime
    ) -> bool:
        file_descriptor = -1
        try:
            file_descriptor = os.open(
                cache_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._cache_dir_fd,
            )
        except FileNotFoundError:
            self._repository.mark_media_expired(
                asset_id, "cache file already missing"
            )
            return False
        except OSError as error:
            if error.errno == errno.ELOOP:
                self._repository.mark_media_expired(asset_id, "unsafe cache entry")
            else:
                self._record_cleanup_failure(
                    asset_id,
                    "cache open failed",
                    error,
                    now,
                    retained_name=cache_name,
                    registered_name=cache_name,
                )
            return False

        quarantine_name = ".quarantine-" + self._new_cache_name()
        try:
            opened = os.fstat(file_descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                if stat.S_ISDIR(opened.st_mode):
                    self._record_cleanup_failure(
                        asset_id,
                        "cache open failed",
                        OSError(errno.EISDIR, "cache entry is a directory"),
                        now,
                        retained_name=cache_name,
                        registered_name=cache_name,
                    )
                else:
                    self._repository.mark_media_expired(
                        asset_id, "unsafe cache entry"
                    )
                return False
            try:
                os.rename(
                    cache_name,
                    quarantine_name,
                    src_dir_fd=self._cache_dir_fd,
                    dst_dir_fd=self._cache_dir_fd,
                )
            except FileNotFoundError:
                self._repository.mark_media_expired(
                    asset_id, "cache file already missing"
                )
                return False
            except OSError as error:
                self._record_cleanup_failure(
                    asset_id, "cache quarantine failed", error, now
                    , retained_name=cache_name, registered_name=cache_name
                )
                return False

            quarantine_fd = -1
            try:
                quarantine_fd = os.open(
                    quarantine_name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._cache_dir_fd,
                )
                quarantined = os.fstat(quarantine_fd)
                current = os.stat(
                    quarantine_name,
                    dir_fd=self._cache_dir_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(quarantined.st_mode)
                    or quarantined.st_nlink != 1
                    or not self._same_entry(opened, quarantined)
                    or not self._same_entry(quarantined, current)
                ):
                    self._repository.mark_media_expired(
                        asset_id,
                        "cleanup abandoned because cache quarantine identity changed; "
                        "unknown entry retained at %s"
                        % (self._cache_root / quarantine_name),
                    )
                    return False
                deletion_name = ".delete-" + self._new_cache_name()
                try:
                    os.rename(
                        quarantine_name,
                        deletion_name,
                        src_dir_fd=self._cache_dir_fd,
                        dst_dir_fd=self._cache_dir_fd,
                    )
                except OSError as error:
                    self._record_cleanup_failure(
                        asset_id,
                        "cache delete quarantine failed",
                        error,
                        now,
                        retained_name=quarantine_name,
                        registered_name=cache_name,
                    )
                    return False
                deletion_fd = -1
                try:
                    deletion_fd = os.open(
                        deletion_name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=self._cache_dir_fd,
                    )
                    deletion_stat = os.fstat(deletion_fd)
                    deletion_current = os.stat(
                        deletion_name,
                        dir_fd=self._cache_dir_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(deletion_stat.st_mode)
                        or deletion_stat.st_nlink != 1
                        or not self._same_entry(opened, deletion_stat)
                        or not self._same_entry(deletion_stat, deletion_current)
                    ):
                        self._repository.mark_media_expired(
                            asset_id,
                            "cleanup abandoned because cache deletion identity changed; "
                            "unknown entry retained at %s"
                            % (self._cache_root / deletion_name),
                        )
                        return False
                    try:
                        os.unlink(deletion_name, dir_fd=self._cache_dir_fd)
                    except OSError as error:
                        self._record_cleanup_failure(
                            asset_id,
                            "cache unlink failed",
                            error,
                            now,
                            retained_name=deletion_name,
                            registered_name=cache_name,
                        )
                        return False
                except FileNotFoundError:
                    self._repository.mark_media_expired(
                        asset_id, "cache file already missing"
                    )
                    return False
                except OSError as error:
                    self._record_cleanup_failure(
                        asset_id,
                        "cache deletion verification failed",
                        error,
                        now,
                        retained_name=deletion_name,
                        registered_name=cache_name,
                    )
                    return False
                finally:
                    if deletion_fd >= 0:
                        _close_fd(deletion_fd)
            except FileNotFoundError:
                self._repository.mark_media_expired(
                    asset_id, "cache file already missing"
                )
                return False
            except OSError as error:
                self._record_cleanup_failure(
                    asset_id,
                    "cache quarantine verification failed",
                    error,
                now,
                retained_name=quarantine_name,
                registered_name=cache_name,
                )
                return False
            finally:
                if quarantine_fd >= 0:
                    _close_fd(quarantine_fd)
        finally:
            _close_fd(file_descriptor)

        self._repository.mark_media_expired(asset_id, "cache file removed")
        return True

    def _record_cleanup_failure(
        self,
        asset_id: str,
        action: str,
        error: OSError,
        now: datetime,
        retained_name: Optional[str] = None,
        registered_name: Optional[str] = None,
    ) -> None:
        error_name = errno.errorcode.get(error.errno, "UNKNOWN")
        retained_detail = (
            " at %s" % (self._cache_root / retained_name)
            if retained_name is not None
            else ""
        )
        try:
            recorded = self._repository.record_media_cleanup_error(
                asset_id,
                "%s (%s)%s" % (action, error_name, retained_detail),
                now=now,
                cache_path=(
                    None
                    if retained_name is None
                    else str(self._cache_root / retained_name)
                ),
            )
        except Exception:
            self._restore_retained_cache_name(retained_name, registered_name)
            raise
        if recorded is None:
            self._restore_retained_cache_name(retained_name, registered_name)
            raise MediaRejected("cleanup state was not persisted")

    def _restore_retained_cache_name(
        self,
        retained_name: Optional[str],
        registered_name: Optional[str],
    ) -> None:
        if (
            retained_name is None
            or registered_name is None
            or retained_name == registered_name
        ):
            return
        try:
            os.rename(
                retained_name,
                registered_name,
                src_dir_fd=self._cache_dir_fd,
                dst_dir_fd=self._cache_dir_fd,
            )
        except FileNotFoundError as error:
            try:
                os.stat(
                    registered_name,
                    dir_fd=self._cache_dir_fd,
                    follow_symlinks=False,
                )
            except OSError:
                raise MediaRejected(
                    "cleanup persistence compensation failed; retained cache entry at %s"
                    % (self._cache_root / retained_name)
                ) from error
        except OSError as error:
            raise MediaRejected(
                "cleanup persistence compensation failed; retained cache entry at %s"
                % (self._cache_root / retained_name)
            ) from error

    def _check_download_deadline(self, deadline: float) -> None:
        if self._monotonic_clock() >= deadline:
            raise MediaRejected("media download timed out")

    def _open_remote(self, initial_url: str, deadline: float):
        current = initial_url
        redirects = 0
        while True:
            self._check_download_deadline(deadline)
            addresses = self._validate_remote_url(current, deadline)
            self._check_download_deadline(deadline)
            try:
                response = self._opener(current, addresses[0])
            except Exception as error:
                self._check_download_deadline(deadline)
                raise MediaRejected("media download failed") from error
            try:
                self._check_download_deadline(deadline)
                status_code = getattr(response, "status", None)
                if status_code is None:
                    getcode = getattr(response, "getcode", None)
                    status_code = getcode() if getcode is not None else 200
                self._check_download_deadline(deadline)
                if type(status_code) is not int or not 100 <= status_code <= 599:
                    raise MediaRejected("invalid media response status")
                response_status = status_code
                headers = getattr(response, "headers", None)
                self._check_download_deadline(deadline)
                if response_status in REDIRECT_STATUSES:
                    location = _header(headers, "Location")
                else:
                    location = None
                    content_type = _header(headers, "Content-Type")
                self._check_download_deadline(deadline)
            except MediaRejected:
                _close_quietly(response)
                raise
            except Exception as error:
                _close_quietly(response)
                raise MediaRejected("invalid media response metadata") from error
            if response_status in REDIRECT_STATUSES:
                _close_quietly(response)
                if not location:
                    raise MediaRejected("redirect is missing a location")
                if redirects >= self._max_redirects:
                    raise MediaRejected("redirect limit exceeded")
                current = urljoin(current, location)
                redirects += 1
                continue
            if response_status < 200 or response_status >= 300:
                _close_quietly(response)
                raise MediaRejected("media server returned an error")
            return response, content_type

    def _validate_remote_url(self, url: str, deadline: float) -> Tuple[str, ...]:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() != "https":
            raise MediaRejected("only HTTPS media URLs are allowed")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise MediaRejected("invalid HTTPS media URL")
        try:
            host = _normalize_remote_host(parsed.hostname)
        except ValueError as error:
            raise MediaRejected("invalid HTTPS media URL") from error
        if host not in self._allowed_remote_hosts:
            raise MediaRejected("media host is not in the remote allowlist")
        try:
            port = parsed.port
        except ValueError as error:
            raise MediaRejected("invalid HTTPS media URL") from error
        if port not in (None, 443):
            raise MediaRejected("HTTPS media URLs must use port 443")
        try:
            self._check_download_deadline(deadline)
            addresses = tuple(self._resolver(host))
            self._check_download_deadline(deadline)
        except MediaRejected:
            raise
        except Exception as error:
            raise MediaRejected("media host resolution failed") from error
        if not addresses:
            raise MediaRejected("media host resolution failed")
        validated = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as error:
                raise MediaRejected("media host returned an invalid address") from error
            if (
                not address.is_global
                or address.is_loopback
                or address.is_private
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            ):
                raise MediaRejected(
                    "media URL must resolve only to a public address; private address rejected"
                )
            normalized = str(address)
            if normalized not in validated:
                validated.append(normalized)
        return tuple(validated)

    def _copy_limited(
        self, source, destination_name: str, limit: int, deadline: Optional[float]
    ):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(
            destination_name,
            flags,
            0o600,
            dir_fd=self._cache_dir_fd,
        )
        opened_stat = os.fstat(file_descriptor)
        size = 0
        digest = hashlib.sha256()
        prefix = bytearray()
        written = None
        try:
            with os.fdopen(file_descriptor, "wb") as output:
                while True:
                    if deadline is not None:
                        self._check_download_deadline(deadline)
                    chunk = source.read(CHUNK_SIZE)
                    if deadline is not None:
                        self._check_download_deadline(deadline)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise MediaRejected("media source returned invalid bytes")
                    size += len(chunk)
                    if size > limit:
                        raise MediaRejected("media exceeds size limit")
                    if len(prefix) < 32:
                        prefix.extend(chunk[: 32 - len(prefix)])
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                file_stat = os.fstat(output.fileno())
                written = _WrittenCacheEntry(
                    device=file_stat.st_dev,
                    inode=file_stat.st_ino,
                    size=size,
                    sha256=digest.hexdigest(),
                )
        except Exception:
            if written is None:
                written = _WrittenCacheEntry(
                    device=opened_stat.st_dev,
                    inode=opened_stat.st_ino,
                    size=0,
                    sha256="",
                )
            self._unlink_expected_cache_entry_quietly(destination_name, written)
            raise
        return written, bytes(prefix)

    @staticmethod
    def _new_cache_name() -> str:
        return secrets.token_hex(16)

    @staticmethod
    def _validate_mime(
        kind: str,
        declared_mime: Optional[str],
        response_mime: Optional[str],
        prefix: bytes,
    ) -> str:
        declared = _normal_mime(declared_mime)
        response = _normal_mime(response_mime)
        detected = _detected_mime(prefix)
        candidates = [value for value in (declared, response) if value]

        if detected is not None:
            for candidate in candidates:
                if not _mimes_compatible(candidate, detected):
                    raise MediaRejected("MIME mismatch")
            mime = declared or response or detected
        else:
            if declared and response and declared != response:
                raise MediaRejected("MIME mismatch")
            mime = declared or response

        if not mime:
            raise MediaRejected("media MIME is missing")
        if kind == "image" and (detected is None or not mime.startswith("image/")):
            raise MediaRejected("image signature is invalid")
        if kind == "audio" and (detected is None or not mime.startswith("audio/")):
            raise MediaRejected("audio signature is invalid")
        if kind == "file" and mime not in DOCUMENT_MIMES:
            raise MediaRejected("unsupported file MIME")
        if kind == "file" and mime == "application/pdf" and detected != mime:
            raise MediaRejected("PDF signature is invalid")
        if (
            kind == "file"
            and mime
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            and detected != "application/zip"
        ):
            raise MediaRejected("DOCX signature is invalid")
        return mime

    def _validate_cached_document(self, mime: str, snapshot: bytes) -> None:
        docx_mime = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        if mime == "application/pdf":
            self._pdf_sandbox.validate(snapshot)
            return
        if mime != docx_mime:
            return
        try:
            with zipfile.ZipFile(io.BytesIO(snapshot), "r") as archive:
                document_xml = _read_safe_docx_xml(archive)
                ElementTree.fromstring(document_xml)
        except MediaRejected:
            raise
        except (OSError, ElementTree.ParseError, zipfile.BadZipFile) as error:
            raise MediaRejected("invalid DOCX document") from error

    def _aware_now(self) -> datetime:
        current = self._now_provider()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)


__all__ = ["MediaIngestionService", "NapCatLocalMediaSource"]
