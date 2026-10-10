import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Mapping, Tuple


_COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:@-]{1,160}\Z")
_SAFE_FIELDS = (
    "run_id",
    "task_id",
    "message_id",
    "approval_id",
    "intent_id",
    "session_id",
    "error_class",
)
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(\bauthorization\s*[:=]\s*bearer\s+)[^\s,;]+"
    ),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
        r"client[_-]?secret|secret)\s*[:=]\s*)[^\s,;]+"
    ),
)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


class _JsonLineFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "component": self.component,
            "level": record.levelname.lower(),
            "message": _redact_message(record.getMessage()),
        }
        for field in _SAFE_FIELDS:
            value = getattr(record, field, None)
            if value is None:
                continue
            normalized = str(value)
            if _SAFE_IDENTIFIER.fullmatch(normalized):
                payload[field] = normalized
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class _HardenedRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        if Path(self.baseFilename).is_symlink():
            raise RuntimeError("refusing to use a symlink as a process log")
        stream = super()._open()
        self._harden_file(Path(self.baseFilename))
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        base_path = Path(self.baseFilename)
        for path in base_path.parent.glob(base_path.name + "*"):
            self._harden_file(path)

    @staticmethod
    def _harden_file(path: Path) -> None:
        if path.is_symlink():
            raise RuntimeError("refusing to use a symlink as a process log")
        if path.exists():
            os.chmod(path, 0o600)


def logging_options_from_env(
    env: Mapping[str, str],
) -> Tuple[str, int, int]:
    level = env.get("LOG_LEVEL", "info").strip().lower() or "info"
    if level not in _LEVELS:
        raise ValueError("LOG_LEVEL must be debug, info, warning, error, or critical")
    max_bytes = _positive_int(env, "LOG_MAX_BYTES", 10485760)
    backup_count = _positive_int(env, "LOG_BACKUP_COUNT", 5)
    return level, max_bytes, backup_count


def configure_process_logging(
    component: str,
    log_dir: Path,
    level: str,
    max_bytes: int,
    backup_count: int,
) -> logging.Logger:
    if _COMPONENT.fullmatch(component) is None:
        raise ValueError("component must be a safe identifier")
    normalized_level = level.strip().lower() if isinstance(level, str) else ""
    if normalized_level not in _LEVELS:
        raise ValueError("level must be debug, info, warning, error, or critical")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if (
        isinstance(backup_count, bool)
        or not isinstance(backup_count, int)
        or backup_count < 1
    ):
        raise ValueError("backup_count must be a positive integer")

    directory = Path(log_dir).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError("refusing to use a symlink as the process log directory")
    os.chmod(directory, 0o700)

    handler = None
    try:
        handler = _HardenedRotatingFileHandler(
            directory / (component + ".log"),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler._chichu_process_handler = True
        handler.setLevel(_LEVELS[normalized_level])
        handler.setFormatter(_JsonLineFormatter(component))
    except Exception:
        if handler is not None:
            handler.close()
        raise

    root = logging.getLogger()
    existing_handlers = list(root.handlers)
    for existing in existing_handlers:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(_LEVELS[normalized_level])
    for existing in existing_handlers:
        existing.close()
    return root


def write_safe_bootstrap_error(component: str, error: Exception) -> None:
    error_class = error.__class__.__name__
    if _COMPONENT.fullmatch(component) is None:
        component = "process"
    if _SAFE_IDENTIFIER.fullmatch(error_class) is None:
        error_class = "Exception"
    payload = {
        "component": component,
        "level": "critical",
        "message": "logging_bootstrap_failed",
        "error_class": error_class,
    }
    try:
        sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("%s must be a positive integer" % name) from exc
    if value < 1:
        raise ValueError("%s must be a positive integer" % name)
    return value


def _redact_message(message: str) -> str:
    redacted = message
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
    return _OPENAI_KEY.sub("[REDACTED]", redacted)
