import hashlib
import json
import logging
import os
import re
import stat
import unicodedata
from pathlib import Path


_MAX_CONFIG_BYTES = 65536
_MAX_REPLACEMENTS = 512
_MAX_TERM_CHARS = 64
_MAX_OUTPUT_CHARS = 4096
_LOGGER = logging.getLogger("qq_openclaw_bridge.group_homophone_filter")


def _read_bounded(path: Path) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_CONFIG_BYTES:
            raise ValueError("invalid group homophone configuration")
        chunks = []
        total = 0
        while total <= _MAX_CONFIG_BYTES:
            chunk = os.read(
                descriptor,
                min(8192, _MAX_CONFIG_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_CONFIG_BYTES:
            raise ValueError("invalid group homophone configuration")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid group homophone configuration")
        result[key] = value
    return result


def _term(value: object) -> str:
    if type(value) is not str:
        raise ValueError("invalid group homophone configuration")
    try:
        normalized = unicodedata.normalize("NFC", value)
        normalized.encode("utf-8", errors="strict")
    except (TypeError, UnicodeError, ValueError):
        raise ValueError("invalid group homophone configuration") from None
    if (
        not normalized
        or normalized != normalized.strip()
        or len(normalized) > _MAX_TERM_CHARS
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        raise ValueError("invalid group homophone configuration")
    return normalized


class GroupHomophoneFilter:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        try:
            raw = _read_bounded(self._path)
        except (OSError, ValueError):
            raise ValueError("invalid group homophone configuration") from None
        self._observed_digest = hashlib.sha256(raw).digest()
        self._replacements, self._pattern = self._parse(raw)
        self._reload_error_active = False

    @staticmethod
    def _parse(raw: bytes):
        try:
            if len(raw) > _MAX_CONFIG_BYTES:
                raise ValueError
            value = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_object
            )
            if type(value) is not dict or set(value) != {"version", "replacements"}:
                raise ValueError
            if type(value["version"]) is not int or value["version"] != 1:
                raise ValueError
            configured = value["replacements"]
            if type(configured) is not dict or len(configured) > _MAX_REPLACEMENTS:
                raise ValueError
            replacements = {}
            for source, target in configured.items():
                normalized_source = _term(source)
                normalized_target = _term(target)
                if normalized_source in replacements:
                    raise ValueError
                replacements[normalized_source] = normalized_target
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("invalid group homophone configuration") from None
        keys = sorted(replacements, key=len, reverse=True)
        pattern = (
            re.compile("|".join(re.escape(key) for key in keys))
            if keys
            else None
        )
        return replacements, pattern

    def _reload_if_changed(self) -> None:
        try:
            raw = _read_bounded(self._path)
            digest = hashlib.sha256(raw).digest()
            if digest == self._observed_digest:
                self._reload_error_active = False
                return
            self._observed_digest = digest
            replacements, pattern = self._parse(raw)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            if not self._reload_error_active:
                _LOGGER.warning(
                    "group homophone map reload rejected; keeping last valid map"
                )
            self._reload_error_active = True
            return
        self._replacements = replacements
        self._pattern = pattern
        self._reload_error_active = False

    def replace(self, text: str) -> str:
        if type(text) is not str:
            raise ValueError("invalid group reply")
        self._reload_if_changed()
        if self._pattern is None:
            return text
        result = self._pattern.sub(
            lambda match: self._replacements[match.group(0)], text
        )
        if len(result) > _MAX_OUTPUT_CHARS:
            raise ValueError("filtered group reply is too long")
        return result


__all__ = ["GroupHomophoneFilter"]
