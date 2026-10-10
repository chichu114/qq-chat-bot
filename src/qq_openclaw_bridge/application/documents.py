import csv
import codecs
from datetime import datetime, timezone
import errno
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import stat
import xml.etree.ElementTree as ElementTree
import zipfile

from ..domain.media import DocumentArtifact, MediaRejected, ValidatedMedia
from .media_ingestion import (
    _close_fd,
    _open_trusted_directory,
    _read_safe_docx_xml,
)
from .pdf_sandbox import MAX_EXTRACT_CHARS, PdfSandbox


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
READ_CHUNK_SIZE = 64 * 1024
MAX_DOCUMENT_INPUT_BYTES = 20 * 1024 * 1024
MAX_JSON_INPUT_BYTES = 2 * 1024 * 1024
MAX_JSON_NESTING_DEPTH = 64
MAX_JSON_TOKENS = 100000
MAX_JSON_STRING_CHARS = 1024 * 1024
MAX_JSON_NUMBER_CHARS = 1000
MAX_CSV_ROWS = 10000
SUPPORTED_DOCUMENT_MIMES = {
    "application/json",
    "application/pdf",
    DOCX_MIME,
    "text/csv",
    "text/markdown",
    "text/plain",
}


class DocumentTextExtractor:
    def __init__(self, trusted_cache_root: Path, pdf_sandbox=None) -> None:
        self._cache_root, self._cache_dir_fd = _open_trusted_directory(
            Path(trusted_cache_root), "trusted cache root"
        )
        self._pdf_sandbox = pdf_sandbox or PdfSandbox()

    def close(self) -> None:
        directory_fd = self._cache_dir_fd
        self._cache_dir_fd = -1
        if directory_fd >= 0:
            _close_fd(directory_fd)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def extract(self, media: ValidatedMedia, max_chars: int) -> DocumentArtifact:
        mime = media.mime.split(";", 1)[0].strip().casefold()
        if mime not in SUPPORTED_DOCUMENT_MIMES:
            raise MediaRejected("unsupported document MIME")
        try:
            validated_size = int(media.size)
        except (TypeError, ValueError) as error:
            raise MediaRejected("validated media size is invalid") from error
        if validated_size > MAX_DOCUMENT_INPUT_BYTES:
            raise MediaRejected("document exceeds the 20 MiB input limit")
        if mime == "application/json" and validated_size > MAX_JSON_INPUT_BYTES:
            raise MediaRejected("document exceeds the JSON input limit")

        limit = max(0, int(max_chars))

        artifact_limit = limit
        with self._open_validated_media(media) as source:
            if mime in ("text/plain", "text/markdown"):
                text = self._text_value(source, limit)
            elif mime == "application/json":
                text = self._json_text(source, limit)
            elif mime == "text/csv":
                text = self._csv_text(source, limit)
            elif mime == "application/pdf":
                artifact_limit = min(limit, MAX_EXTRACT_CHARS)
                text = self._pdf_text(source, artifact_limit)
            else:
                text = self._docx_text(source, limit)

        truncated = len(text) > artifact_limit
        return DocumentArtifact(
            text=text[:artifact_limit], mime=mime, truncated=truncated
        )

    def _open_validated_media(self, media: ValidatedMedia):
        expires_at = media.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                raise MediaRejected("validated media has expired")

        path = Path(media.path)
        if not path.is_absolute():
            raise MediaRejected("document is outside trusted cache root")
        try:
            relative = path.relative_to(self._cache_root)
        except ValueError as error:
            raise MediaRejected("document is outside trusted cache root") from error
        if len(relative.parts) != 1 or relative.name in ("", ".", ".."):
            raise MediaRejected("document is outside trusted cache root")

        try:
            file_descriptor = os.open(
                relative.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._cache_dir_fd,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise MediaRejected("document cannot be a symbolic link") from error
            raise MediaRejected("trusted cache document is unavailable") from error

        source = None
        try:
            file_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise MediaRejected("document is not a regular file")
            if file_stat.st_nlink != 1:
                raise MediaRejected("document cannot be a hard link")
            if file_stat.st_size > MAX_DOCUMENT_INPUT_BYTES:
                raise MediaRejected("document exceeds the 20 MiB input limit")
            if file_stat.st_size != int(media.size):
                raise MediaRejected("document size does not match validated media")

            source = os.fdopen(file_descriptor, "rb")
            file_descriptor = -1
            digest = hashlib.sha256()
            chunks = []
            total = 0
            while True:
                chunk = source.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOCUMENT_INPUT_BYTES:
                    raise MediaRejected("document exceeds the 20 MiB input limit")
                digest.update(chunk)
                chunks.append(chunk)
            after = os.fstat(source.fileno())
            if (
                total != file_stat.st_size
                or after.st_dev != file_stat.st_dev
                or after.st_ino != file_stat.st_ino
                or after.st_size != file_stat.st_size
                or after.st_nlink != 1
            ):
                raise MediaRejected("document changed during validation")
            if not hmac.compare_digest(digest.hexdigest(), str(media.sha256).casefold()):
                raise MediaRejected("document SHA-256 does not match validated media")
            snapshot = b"".join(chunks)
            source.close()
            source = None
            immutable_source = io.BytesIO(snapshot)
            self._validate_signature(immutable_source, media.mime)
            immutable_source.seek(0)
            return immutable_source
        except Exception:
            if source is not None:
                source.close()
            if file_descriptor >= 0:
                _close_fd(file_descriptor)
            raise

    @staticmethod
    def _validate_signature(source, mime: str) -> None:
        normalized = mime.split(";", 1)[0].strip().casefold()
        prefix = source.read(5)
        if normalized == "application/pdf" and not prefix.startswith(b"%PDF-"):
            raise MediaRejected("PDF signature is invalid")
        if normalized == DOCX_MIME and not prefix.startswith(b"PK\x03\x04"):
            raise MediaRejected("DOCX signature is invalid")

    @staticmethod
    def _append_bounded(parts, length: int, value: str, cap: int):
        if length >= cap or not value:
            return length, length >= cap
        piece = value[: cap - length]
        parts.append(piece)
        length += len(piece)
        return length, length >= cap

    @classmethod
    def _text_value(cls, source, limit: int) -> str:
        cap = limit + 1
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts = []
        length = 0
        while length < cap:
            chunk = source.read(READ_CHUNK_SIZE)
            decoded = decoder.decode(chunk, final=not chunk)
            length, full = cls._append_bounded(parts, length, decoded, cap)
            if full or not chunk:
                break
        return "".join(parts)

    @classmethod
    def _json_text(cls, source, limit: int) -> str:
        try:
            raw = source.read(MAX_JSON_INPUT_BYTES + 1)
            if len(raw) > MAX_JSON_INPUT_BYTES:
                raise MediaRejected("document exceeds the JSON input limit")
            decoded = raw.decode("utf-8")
            cls._prescan_json(decoded)
            value = json.loads(decoded)
        except MediaRejected:
            raise
        except (UnicodeError, json.JSONDecodeError) as error:
            raise MediaRejected("invalid JSON document") from error

        cap = limit + 1
        parts = []
        length = 0
        encoder = json.JSONEncoder(ensure_ascii=False)
        for chunk in encoder.iterencode(value):
            length, full = cls._append_bounded(parts, length, chunk, cap)
            if full:
                break
        return "".join(parts)

    @staticmethod
    def _prescan_json(value: str) -> None:
        depth = 0
        tokens = 0
        index = 0
        length = len(value)
        number_characters = set("-+0123456789.eE")
        while index < length:
            character = value[index]
            if character in " \t\r\n":
                index += 1
                continue
            if character == '"':
                tokens += 1
                index += 1
                string_length = 0
                escaped = False
                while index < length:
                    current = value[index]
                    if escaped:
                        escaped = False
                        string_length += 1
                    elif current == "\\":
                        escaped = True
                        string_length += 1
                    elif current == '"':
                        index += 1
                        break
                    else:
                        string_length += 1
                    if string_length > MAX_JSON_STRING_CHARS:
                        raise MediaRejected("JSON string length limit exceeded")
                    index += 1
            elif character in "[{":
                depth += 1
                tokens += 1
                if depth > MAX_JSON_NESTING_DEPTH:
                    raise MediaRejected("JSON nesting depth limit exceeded")
                index += 1
            elif character in "]}":
                depth -= 1
                index += 1
            elif character == "-" or character.isdigit():
                start = index
                while index < length and value[index] in number_characters:
                    index += 1
                if index - start > MAX_JSON_NUMBER_CHARS:
                    raise MediaRejected("JSON numeric literal length limit exceeded")
                tokens += 1
            elif character in ",:":
                index += 1
            else:
                start = index
                while index < length and value[index].isalpha():
                    index += 1
                if index == start:
                    index += 1
                tokens += 1
            if tokens > MAX_JSON_TOKENS:
                raise MediaRejected("JSON token limit exceeded")

    @classmethod
    def _csv_text(cls, source, limit: int) -> str:
        cap = limit + 1
        parts = []
        length = 0
        decoded = io.TextIOWrapper(
            source,
            encoding="utf-8",
            errors="replace",
            newline="",
        )
        try:
            for row_number, row in enumerate(csv.reader(decoded), 1):
                if row_number > MAX_CSV_ROWS:
                    raise MediaRejected("CSV row limit exceeded")
                row_output = io.StringIO(newline="")
                csv.writer(row_output, lineterminator="\n").writerow(row)
                length, full = cls._append_bounded(
                    parts, length, row_output.getvalue(), cap
                )
                if full:
                    break
        except csv.Error as error:
            raise MediaRejected("invalid CSV document") from error
        finally:
            decoded.detach()
        return "".join(parts)

    def _pdf_text(self, source, limit: int) -> str:
        snapshot = source.read(MAX_DOCUMENT_INPUT_BYTES + 1)
        if len(snapshot) > MAX_DOCUMENT_INPUT_BYTES:
            raise MediaRejected("document exceeds the 20 MiB input limit")
        return self._pdf_sandbox.extract(snapshot, limit)

    @classmethod
    def _docx_text(cls, source, limit: int) -> str:
        try:
            with zipfile.ZipFile(source, "r") as archive:
                document_xml = _read_safe_docx_xml(archive)

            cap = limit + 1
            parts = []
            length = 0
            paragraph_count = 0
            paragraph_tag = "{%s}p" % WORD_NAMESPACE
            text_tag = "{%s}t" % WORD_NAMESPACE
            for _, paragraph in ElementTree.iterparse(
                io.BytesIO(document_xml), events=("end",)
            ):
                if paragraph.tag != paragraph_tag:
                    continue
                if paragraph_count:
                    length, full = cls._append_bounded(parts, length, "\n", cap)
                    if full:
                        break
                paragraph_count += 1
                for node in paragraph.iter(text_tag):
                    length, full = cls._append_bounded(
                        parts, length, node.text or "", cap
                    )
                    if full:
                        break
                paragraph.clear()
                if length >= cap:
                    break
            return "".join(parts)
        except MediaRejected:
            raise
        except (OSError, ElementTree.ParseError, zipfile.BadZipFile) as error:
            raise MediaRejected("invalid DOCX document") from error


__all__ = ["DocumentTextExtractor"]
