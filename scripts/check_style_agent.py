#!/usr/bin/env python3
"""Read-only readiness check for the optional community style agent."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Mapping, Optional
from urllib.parse import quote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from qq_openclaw_bridge.config import Config
from qq_openclaw_bridge.persistence.community_style import CommunityStyleRepository
from qq_openclaw_bridge.runtime.community_style_config import (
    load_community_style_settings,
)
from qq_openclaw_bridge.runtime.ollama import OllamaClient, OllamaHealth


_MAX_COUNT = 2 ** 63 - 1
_OLLAMA_TIMEOUT_SECONDS = 5.0
_SNAPSHOT_CHUNK_BYTES = 64 * 1024
_OLLAMA_ERRORS = frozenset((
    "timeout", "connection", "http_status", "model_missing",
    "invalid_response", "response_too_large", "redirect_rejected",
))


@dataclass(frozen=True)
class CheckResult:
    exit_code: int
    stdout: str


@dataclass(frozen=True)
class _SnapshotLimits:
    main_bytes: int
    wal_bytes: int
    shm_bytes: int
    total_bytes: int
    deadline_seconds: float


_SNAPSHOT_LIMITS = _SnapshotLimits(
    main_bytes=512 * 1024 * 1024,
    wal_bytes=256 * 1024 * 1024,
    shm_bytes=64 * 1024 * 1024,
    total_bytes=640 * 1024 * 1024,
    deadline_seconds=10.0,
)


class _DatabaseBusy(RuntimeError):
    pass


class _DatabaseMissing(RuntimeError):
    pass


class _DatabaseSnapshotUnavailable(RuntimeError):
    pass


class _SourceStreamMismatch(RuntimeError):
    pass


class _ReadOnlySqliteDatabase:
    """Minimal Task 3 database interface with no lock, pragma, or migration writes."""

    def __init__(self, path: Path) -> None:
        candidate = Path(path).expanduser()
        try:
            details = candidate.lstat()
        except (OSError, ValueError):
            raise ValueError("database_unavailable") from None
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("database_unavailable")
        self.path = candidate

    def connect(self) -> sqlite3.Connection:
        uri = "file:%s?mode=ro" % quote(
            str(self.path.absolute()), safe="/"
        )
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    @contextmanager
    def locked(self):
        yield

    def harden_files(self) -> None:
        return None


def _default_repository_factory(path: Path):
    return CommunityStyleRepository(_ReadOnlySqliteDatabase(path))


def _default_ollama_factory(settings, *, timeout_seconds: float):
    return OllamaClient(settings, timeout_seconds=timeout_seconds)


def _safe_call(function, *args, **kwargs):
    control = None
    try:
        return True, function(*args, **kwargs)
    except BaseException as error:
        if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
            error.args = ()
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            control = type(error)
        else:
            return False, None
    if control is GeneratorExit:
        raise GeneratorExit() from None
    if control is KeyboardInterrupt:
        raise KeyboardInterrupt() from None
    raise SystemExit() from None


def _bootstrap_enabled(env):
    if not isinstance(env, Mapping):
        return None
    ok, raw = _safe_call(env.get, "ENABLE_GROUP_AGENT", None)
    if not ok:
        return None
    if raw is None:
        return False
    if type(raw) is not str:
        return None
    value = raw.strip().casefold()
    if not value or value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    return None


def _file_stamp(path: Path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("database_unavailable")
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _source_state(path: Path):
    database = _file_stamp(path)
    if database is None:
        raise _DatabaseMissing()
    sidecars = tuple(
        _file_stamp(Path(str(path) + suffix))
        for suffix in ("-wal", "-shm", "-journal")
    )
    return database, sidecars


def _validated_limits(limits):
    if type(limits) is not _SnapshotLimits:
        raise _DatabaseSnapshotUnavailable()
    values = (
        limits.main_bytes, limits.wal_bytes, limits.shm_bytes,
        limits.total_bytes,
    )
    if any(type(value) is not int or value < 1 for value in values):
        raise _DatabaseSnapshotUnavailable()
    if (
        type(limits.deadline_seconds) not in (int, float)
        or isinstance(limits.deadline_seconds, bool)
        or not math.isfinite(float(limits.deadline_seconds))
        or not 0 < float(limits.deadline_seconds) <= 300.0
    ):
        raise _DatabaseSnapshotUnavailable()
    return limits


def _validate_snapshot_sizes(state, limits):
    main_size = state[0][3]
    wal_size = 0 if state[1][0] is None else state[1][0][3]
    shm_size = 0 if state[1][1] is None else state[1][1][3]
    if (
        main_size > limits.main_bytes
        or wal_size > limits.wal_bytes
        or shm_size > limits.shm_bytes
        or main_size + wal_size + shm_size > limits.total_bytes
    ):
        raise _DatabaseSnapshotUnavailable()


def _clock_value(clock):
    try:
        value = clock()
    except BaseException as error:
        if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
            raise
        raise _DatabaseSnapshotUnavailable() from None
    if (
        type(value) not in (int, float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or abs(float(value)) > 10 ** 12
    ):
        raise _DatabaseSnapshotUnavailable()
    return float(value)


def _check_deadline(clock, deadline):
    if _clock_value(clock) >= deadline:
        raise _DatabaseSnapshotUnavailable()


def _copy_bounded(
    input_file,
    output_file,
    *,
    expected_size,
    byte_limit,
    deadline,
    clock,
    chunk_bytes=_SNAPSHOT_CHUNK_BYTES,
    mismatch_error=_DatabaseSnapshotUnavailable,
):
    if (
        type(expected_size) is not int
        or type(byte_limit) is not int
        or type(chunk_bytes) is not int
        or expected_size < 0
        or byte_limit < 1
        or expected_size > byte_limit
        or not 1 <= chunk_bytes <= _SNAPSHOT_CHUNK_BYTES
    ):
        raise _DatabaseSnapshotUnavailable()
    copied = 0
    while True:
        _check_deadline(clock, deadline)
        remaining = expected_size - copied
        read_size = min(chunk_bytes, remaining + 1)
        try:
            chunk = input_file.read(read_size)
        except BaseException as error:
            if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                raise
            raise _DatabaseSnapshotUnavailable() from None
        _check_deadline(clock, deadline)
        if type(chunk) is not bytes or len(chunk) > read_size:
            raise _DatabaseSnapshotUnavailable()
        if not chunk:
            if copied != expected_size:
                raise mismatch_error()
            return copied
        if len(chunk) > remaining:
            raise mismatch_error()
        try:
            written = output_file.write(chunk)
        except BaseException as error:
            if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                raise
            raise _DatabaseSnapshotUnavailable() from None
        if written != len(chunk):
            raise _DatabaseSnapshotUnavailable()
        copied += len(chunk)


@contextmanager
def _read_only_copy(
    path: Path,
    *,
    limits=_SNAPSHOT_LIMITS,
    clock=None,
    temp_parent=None,
):
    """Copy a coherent DB/WAL view; never let SQLite touch the source directory."""
    limits = _validated_limits(limits)
    source = Path(path).expanduser()
    try:
        before = _source_state(source)
    except (_DatabaseMissing, _DatabaseBusy, _DatabaseSnapshotUnavailable):
        raise
    except BaseException as error:
        if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
            raise
        raise _DatabaseSnapshotUnavailable() from None
    if before[1][2] is not None:
        raise _DatabaseBusy()
    _validate_snapshot_sizes(before, limits)
    clock = time.monotonic if clock is None else clock
    if not callable(clock):
        raise _DatabaseSnapshotUnavailable()
    deadline = _clock_value(clock) + float(limits.deadline_seconds)
    if not math.isfinite(deadline) or abs(deadline) > 10 ** 12:
        raise _DatabaseSnapshotUnavailable()

    def source_changed():
        try:
            return _source_state(source) != before
        except _DatabaseMissing:
            return True
        except BaseException as error:
            if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                raise
            return None

    manager = None
    try:
        try:
            manager = tempfile.TemporaryDirectory(
                prefix="qq-style-readiness-", dir=temp_parent
            )
            temp_dir = manager.__enter__()
        except BaseException as error:
            if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                raise
            raise _DatabaseSnapshotUnavailable() from None
        target = Path(temp_dir) / "state.sqlite3"

        def copy_exact(source_path, target_path, expected, byte_limit):
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(str(source_path), flags)
            except FileNotFoundError:
                raise _DatabaseBusy() from None
            except BaseException as error:
                if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                    raise
                raise _DatabaseSnapshotUnavailable() from None
            pending_type = None
            try:
                try:
                    opened = os.fstat(descriptor)
                except BaseException as error:
                    if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                        raise
                    raise _DatabaseSnapshotUnavailable() from None
                opened_stamp = (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened_stamp != expected
                ):
                    raise _DatabaseBusy()
                try:
                    with os.fdopen(descriptor, "rb", closefd=False) as input_file:
                        with target_path.open("xb") as output_file:
                            _copy_bounded(
                                input_file,
                                output_file,
                                expected_size=expected[3],
                                byte_limit=byte_limit,
                                deadline=deadline,
                                clock=clock,
                                mismatch_error=_SourceStreamMismatch,
                            )
                except _SourceStreamMismatch:
                    changed = source_changed()
                    if changed is True:
                        raise _DatabaseBusy() from None
                    raise _DatabaseSnapshotUnavailable() from None
                except (_DatabaseBusy, _DatabaseSnapshotUnavailable):
                    raise
                except BaseException as error:
                    if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                        raise
                    raise _DatabaseSnapshotUnavailable() from None
            except BaseException as error:
                pending_type = type(error)
                raise
            finally:
                try:
                    os.close(descriptor)
                except BaseException as error:
                    if pending_type in (GeneratorExit, KeyboardInterrupt, SystemExit):
                        pass
                    elif type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                        raise
                    elif pending_type is not _DatabaseBusy:
                        raise _DatabaseSnapshotUnavailable() from None

        copy_exact(source, target, before[0], limits.main_bytes)
        wal_stamp = before[1][0]
        if wal_stamp is not None:
            copy_exact(
                Path(str(source) + "-wal"),
                Path(str(target) + "-wal"),
                wal_stamp,
                limits.wal_bytes,
            )
        changed = source_changed()
        if changed is True:
            raise _DatabaseBusy()
        if changed is None:
            raise _DatabaseSnapshotUnavailable()
        try:
            yield target
        finally:
            changed = source_changed()
            if changed is True:
                raise _DatabaseBusy()
            if changed is None:
                raise _DatabaseSnapshotUnavailable()
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except BaseException as error:
                if type(error) in (GeneratorExit, KeyboardInterrupt, SystemExit):
                    raise
                raise _DatabaseSnapshotUnavailable() from None


def _read_production_status(path: Path, repository_factory):
    try:
        with _read_only_copy(path) as snapshot_path:
            repository = repository_factory(snapshot_path)
            return "ok", repository.status_snapshot()
    except _DatabaseMissing:
        return "missing", None
    except _DatabaseBusy:
        return "busy", None
    except _DatabaseSnapshotUnavailable:
        return "snapshot_unavailable", None


def _empty_payload(enabled: bool):
    return {
        "schema_version": 1,
        "component": "community_style_agent",
        "enabled": bool(enabled),
        "status": "not_ready",
        "checks": {
            "config": "invalid",
            "database": "skipped",
            "ollama": "skipped",
        },
        "counts": {
            "sources": 0,
            "authorizations": 0,
            "samples": 0,
            "profiles": 0,
            "groups": 0,
        },
    }


def _render(payload, json_output: bool) -> str:
    if json_output:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    enabled = "true" if payload["enabled"] else "false"
    checks = payload["checks"]
    counts = payload["counts"]
    return (
        "community_style_agent status=%s enabled=%s config=%s database=%s "
        "ollama=%s sources=%d authorizations=%d samples=%d profiles=%d groups=%d\n"
        % (
            payload["status"], enabled, checks["config"], checks["database"],
            checks["ollama"], counts["sources"], counts["authorizations"],
            counts["samples"], counts["profiles"], counts["groups"],
        )
    )


def _result(payload, exit_code: int, json_output: bool) -> CheckResult:
    return CheckResult(exit_code=exit_code, stdout=_render(payload, json_output))


def _count(value) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        raise ValueError("invalid_status_count")
    return value


def _sum_states(container, names) -> int:
    if type(container) is not dict or set(container) != set(names):
        raise ValueError("invalid_status_shape")
    total = sum(_count(container[name]) for name in names)
    return _count(total)


def _status_counts(snapshot):
    if type(snapshot) is not dict:
        raise ValueError("invalid_status_shape")
    expected = {
        "sources", "authorizations", "samples", "profiles", "receipts",
        "enabled_groups",
    }
    if not expected <= set(snapshot):
        raise ValueError("invalid_status_shape")
    _sum_states(snapshot["receipts"], ("processing", "sent", "failed"))
    return {
        "sources": _sum_states(
            snapshot["sources"], ("idle", "running", "retry", "paused")
        ),
        "authorizations": _count(snapshot["authorizations"].get("active"))
        if type(snapshot["authorizations"]) is dict
        and set(snapshot["authorizations"]) == {"active", "revoked", "expired"}
        else (_ for _ in ()).throw(ValueError("invalid_status_shape")),
        "samples": _count(snapshot["samples"].get("active"))
        if type(snapshot["samples"]) is dict
        and set(snapshot["samples"]) == {"active", "removed"}
        else (_ for _ in ()).throw(ValueError("invalid_status_shape")),
        "profiles": _count(snapshot["profiles"].get("active"))
        if type(snapshot["profiles"]) is dict
        and set(snapshot["profiles"]) == {"candidate", "active", "retired"}
        else (_ for _ in ()).throw(ValueError("invalid_status_shape")),
        "groups": _count(snapshot["enabled_groups"]),
    }


def _configuration_counts(settings):
    profiles = {source.profile for source in settings.sources}
    return {
        "sources": _count(len(settings.sources)),
        "authorizations": _count(sum(
            len(source.authorizations) for source in settings.sources
        )),
        "samples": 0,
        "profiles": _count(len(profiles)),
        "groups": _count(len(settings.groups)),
    }


def _is_loopback(url: str) -> bool:
    ok, parsed = _safe_call(urlsplit, url)
    if not ok:
        return False
    ok, hostname = _safe_call(lambda: parsed.hostname)
    if not ok or type(hostname) is not str:
        return False
    if hostname.rstrip(".").casefold() == "localhost":
        return True
    ok, address = _safe_call(ipaddress.ip_address, hostname)
    return bool(ok and address.is_loopback)


def _health_status(health) -> Optional[str]:
    if type(health) is not OllamaHealth:
        return "invalid_response"
    if type(health.reachable) is not bool or type(health.model_available) is not bool:
        return "invalid_response"
    error = health.error_class
    if error is not None and (type(error) is not str or error not in _OLLAMA_ERRORS):
        return "invalid_response"
    if health.reachable and health.model_available and error is None:
        return "ok"
    if not health.model_available and error in _OLLAMA_ERRORS:
        return error
    return "invalid_response"


def _resolved_path(value: str, project_dir: Path) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else project_dir / candidate


def run_check(
    env,
    *,
    repository=None,
    ollama=None,
    json_output: bool = False,
    repository_factory=None,
    ollama_factory=None,
    project_dir: Optional[Path] = None,
    config_only: bool = False,
) -> CheckResult:
    """Validate configuration, inspect sanitized state, and GET Ollama tags only."""
    enabled = _bootstrap_enabled(env)
    payload = _empty_payload(enabled is True)
    if (
        enabled is None
        or type(json_output) is not bool
        or type(config_only) is not bool
    ):
        payload["status"] = "invalid_config"
        return _result(payload, 2, bool(json_output))
    if not enabled:
        payload["status"] = "disabled"
        payload["checks"] = {
            "config": "disabled", "database": "skipped", "ollama": "skipped",
        }
        return _result(payload, 0, json_output)

    ok, config = _safe_call(Config.from_env, env)
    if not ok:
        payload["status"] = "invalid_config"
        return _result(payload, 2, json_output)
    payload["enabled"] = bool(config.enable_group_agent)
    if not config.enable_group_agent:
        payload["status"] = "disabled"
        payload["checks"] = {
            "config": "disabled", "database": "skipped", "ollama": "skipped",
        }
        return _result(payload, 0, json_output)

    base = Path.cwd() if project_dir is None else Path(project_dir)
    config_path = _resolved_path(config.style_agent_config_path, base)
    ok, settings = _safe_call(load_community_style_settings, config_path)
    if not ok or (
        not settings.ollama.allow_remote
        and not _is_loopback(settings.ollama.base_url)
    ):
        payload["status"] = "invalid_config"
        return _result(payload, 2, json_output)
    payload["checks"]["config"] = "ok"
    if not settings.enabled:
        payload["enabled"] = False
        payload["status"] = "disabled"
        payload["checks"] = {
            "config": "disabled", "database": "skipped", "ollama": "skipped",
        }
        return _result(payload, 0, json_output)

    if config_only:
        ok, counts = _safe_call(_configuration_counts, settings)
        if not ok:
            payload["checks"]["config"] = "invalid"
            payload["status"] = "invalid_config"
            return _result(payload, 2, json_output)
        payload["counts"] = counts
        payload["status"] = "config_valid"
        return _result(payload, 0, json_output)

    if repository is None:
        database_path = _resolved_path(config.state_database_path, base)
        factory = repository_factory or _default_repository_factory
        ok, database_result = _safe_call(
            _read_production_status, database_path, factory
        )
        if not ok:
            payload["checks"]["database"] = "invalid"
            return _result(payload, 1, json_output)
        database_status, snapshot = database_result
        if database_status != "ok":
            payload["checks"]["database"] = database_status
            return _result(payload, 1, json_output)
        ok = True
    else:
        ok, snapshot = _safe_call(repository.status_snapshot)
    if ok:
        ok, counts = _safe_call(_status_counts, snapshot)
    if not ok:
        payload["checks"]["database"] = "invalid"
        return _result(payload, 1, json_output)
    payload["counts"] = counts
    payload["checks"]["database"] = "ok"

    if ollama is None:
        factory = ollama_factory or _default_ollama_factory
        ok, ollama = _safe_call(
            factory, settings.ollama, timeout_seconds=_OLLAMA_TIMEOUT_SECONDS
        )
        if not ok:
            payload["checks"]["ollama"] = "invalid_response"
            return _result(payload, 1, json_output)
    ok, health = _safe_call(ollama.health)
    status = _health_status(health) if ok else "invalid_response"
    payload["checks"]["ollama"] = status
    if status != "ok":
        return _result(payload, 1, json_output)

    payload["status"] = "ready"
    return _result(payload, 0, json_output)


def _parser():
    parser = argparse.ArgumentParser(
        description="Read-only community style agent readiness check.",
        epilog=(
            "Exit codes: 0=ready, config-valid, or disabled; "
            "1=not-ready; 2=invalid-config."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit fixed-schema JSON")
    parser.add_argument(
        "--config-only", action="store_true",
        help="validate configuration without checking SQLite or Ollama",
    )
    parser.add_argument(
        "--project-dir", type=Path, default=PROJECT_ROOT,
        help="project root used for relative configuration paths",
    )
    return parser


def main(argv=None) -> int:
    arguments = _parser().parse_args(argv)
    environment = dict(os.environ)
    result = run_check(
        environment,
        json_output=arguments.json,
        project_dir=arguments.project_dir,
        config_only=arguments.config_only,
    )
    sys.stdout.write(result.stdout)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
