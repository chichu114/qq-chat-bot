import json
import os
import re
import socket
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol
from urllib.parse import urlparse


class Probe(Protocol):
    def run(self) -> bool:
        ...


class PidFileProbe:
    def __init__(self, path: Path) -> None:
        self._path = path

    def run(self) -> bool:
        try:
            pid = int(self._path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return True
        except (FileNotFoundError, ValueError, OSError):
            return False


class LaunchAgentProbe:
    _ALLOWED_LABELS = frozenset(("com.chichu.qq-bridge",))

    def __init__(
        self,
        label: str,
        runner: Callable = subprocess.run,
        uid: Optional[int] = None,
    ) -> None:
        if label not in self._ALLOWED_LABELS:
            raise ValueError("unsupported LaunchAgent label")
        self._target = "gui/%d/%s" % (
            os.getuid() if uid is None else uid,
            label,
        )
        self._runner = runner

    def run(self) -> bool:
        try:
            result = self._runner(
                ["/bin/launchctl", "print", self._target],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and bool(
            re.search(
                r"(?m)^\s*state = running\s*$",
                str(result.stdout or ""),
            )
        )


class UrlProbe:
    def __init__(self, raw: Optional[str], default_port: Optional[int] = None) -> None:
        self._raw = raw
        self._default_port = default_port

    def run(self) -> bool:
        if not self._raw:
            return False
        parsed = urlparse(self._raw)
        host = parsed.hostname
        port = parsed.port or self._default_port
        if not host or not port:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False


class OneBotStatusProbe:
    def __init__(
        self,
        raw: Optional[str],
        timeout_seconds: float = 2.0,
        status_provider: Optional[Callable[[str, float], Mapping[str, object]]] = None,
    ) -> None:
        self._raw = raw
        self._timeout_seconds = timeout_seconds
        self._status_provider = status_provider or _read_onebot_status

    def run(self) -> bool:
        if not self._raw:
            return False
        try:
            payload = self._status_provider(self._raw, self._timeout_seconds)
            data = payload.get("data")
            return (
                payload.get("status") == "ok"
                and payload.get("retcode") == 0
                and isinstance(data, Mapping)
                and data.get("online") is True
                and data.get("good") is True
            )
        except Exception:
            return False


def _read_onebot_status(raw: str, timeout_seconds: float) -> Mapping[str, object]:
    from websockets.sync.client import connect

    echo = "watchdog-%d" % time.time_ns()
    deadline = time.monotonic() + timeout_seconds
    with connect(
        raw,
        open_timeout=timeout_seconds,
        close_timeout=min(1.0, timeout_seconds),
    ) as websocket:
        websocket.send(
            json.dumps(
                {"action": "get_status", "params": {}, "echo": echo},
                separators=(",", ":"),
            )
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("OneBot get_status timed out")
            payload = json.loads(websocket.recv(timeout=remaining))
            if isinstance(payload, dict) and payload.get("echo") == echo:
                return payload


class ProxyProbe:
    def __init__(self, env: Dict[str, str]) -> None:
        self._env = env

    def run(self) -> bool:
        proxy = next(
            (
                self._env.get(name)
                for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
                if self._env.get(name)
            ),
            None,
        )
        return True if not proxy else UrlProbe(proxy).run()


class OpenClawProbe:
    def __init__(self, env: Dict[str, str], project_dir: Path) -> None:
        self._env = env
        self._project_dir = project_dir

    def run(self) -> bool:
        if env_bool(self._env, "OPENCLAW_USE_GATEWAY", True):
            return UrlProbe(
                self._env.get(
                    "OPENCLAW_GATEWAY_PROBE_URL", "ws://127.0.0.1:18789"
                ),
                18789,
            ).run()
        command = self._env.get("OPENCLAW_COMMAND", "scripts/openclaw.sh")
        path = Path(command)
        if not path.is_absolute():
            path = self._project_dir / path
        return path.is_file()


class QueueProbe:
    def __init__(self, env: Dict[str, str]) -> None:
        self._env = env

    def run(self) -> bool:
        if not env_bool(self._env, "ENABLE_SQLITE_STORE", True):
            return True
        raw_path = self._env.get("STATE_DATABASE_PATH", "").strip()
        if not raw_path:
            return True
        database_path = Path(
            os.path.expandvars(os.path.expanduser(raw_path))
        )
        if not database_path.is_file():
            return False
        try:
            with sqlite3.connect(str(database_path), timeout=1) as connection:
                healthy = connection.execute("PRAGMA quick_check").fetchone()[0]
                failed = connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE status='failed'"
                ).fetchone()[0]
            return healthy == "ok" and int(failed) == 0
        except (sqlite3.Error, OSError):
            return False


class RoutingProbe:
    """Read F1 persistence only; watchdog checks must never invoke a model."""

    def __init__(
        self,
        env: Dict[str, str],
        project_dir: Path,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._env = env
        self._project_dir = project_dir
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def run(self) -> bool:
        if not env_bool(self._env, "ENABLE_SHADOW_ROUTING", False):
            return True
        if not env_bool(self._env, "ENABLE_SQLITE_STORE", True):
            return False
        raw_path = self._env.get("STATE_DATABASE_PATH", "").strip()
        if not raw_path:
            return False
        database_path = Path(
            os.path.expandvars(os.path.expanduser(raw_path))
        )
        if not database_path.is_absolute():
            database_path = self._project_dir / database_path
        if not database_path.is_file():
            return False
        try:
            from qq_openclaw_bridge.runtime.readiness import (
                routing_health_from_connection,
            )

            with sqlite3.connect(
                database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
            ) as connection:
                return routing_health_from_connection(connection, self._now_provider())
        except (OSError, sqlite3.Error, ValueError):
            return False


def env_bool(env: Dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")
