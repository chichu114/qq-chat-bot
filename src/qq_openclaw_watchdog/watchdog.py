import json
import os
import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .probes import (
    LaunchAgentProbe,
    OneBotStatusProbe,
    OpenClawProbe,
    ProxyProbe,
    QueueProbe,
    RoutingProbe,
    UrlProbe,
)
from .recovery import NapCatRestarter


@dataclass(frozen=True)
class WatchdogSettings:
    check_interval_seconds: int = 30
    failure_threshold: int = 3
    onebot_failure_threshold: int = 2
    onebot_auto_recovery: bool = False
    restart_cooldown_seconds: int = 300
    max_restarts: int = 3
    restart_window_seconds: int = 1800
    notifications: bool = True

    @classmethod
    def from_env(cls, env: Dict[str, str]) -> "WatchdogSettings":
        return cls(
            check_interval_seconds=_env_int(env, "WATCHDOG_CHECK_INTERVAL_SECONDS", 30),
            failure_threshold=_env_int(env, "WATCHDOG_FAILURE_THRESHOLD", 3),
            onebot_failure_threshold=_env_int(
                env, "WATCHDOG_ONEBOT_FAILURE_THRESHOLD", 2
            ),
            onebot_auto_recovery=_env_bool(
                env, "WATCHDOG_ONEBOT_AUTO_RECOVERY", False
            ),
            restart_cooldown_seconds=_env_int(env, "WATCHDOG_RESTART_COOLDOWN_SECONDS", 300),
            max_restarts=_env_int(env, "WATCHDOG_MAX_RESTARTS", 3),
            restart_window_seconds=_env_int(env, "WATCHDOG_RESTART_WINDOW_SECONDS", 1800),
            notifications=_env_bool(env, "WATCHDOG_NOTIFICATIONS", True),
        )


class Watchdog:
    def __init__(
        self,
        project_dir: Path,
        settings: WatchdogSettings,
        bridge_probe: Optional[Callable[[], bool]] = None,
        onebot_probe: Optional[Callable[[], bool]] = None,
        proxy_probe: Optional[Callable[[], bool]] = None,
        openclaw_probe: Optional[Callable[[], bool]] = None,
        queue_probe: Optional[Callable[[], bool]] = None,
        routing_probe: Optional[Callable[[], bool]] = None,
        runner: Optional[Callable] = None,
        notifier: Optional[Callable[[str, str], None]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        state_store: Optional[object] = None,
        onebot_restarter: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.settings = settings
        self.health_path = self.project_dir / "logs" / "health.json"
        self.bridge_pid_path = self.project_dir / "logs" / "bridge.pid"
        self.service_script = self.project_dir / "scripts" / "bridge_service.sh"
        self.env = _read_env(self.project_dir / ".env")
        self._bridge_probe_object = LaunchAgentProbe(
            "com.chichu.qq-bridge"
        )
        self._onebot_probe_object = OneBotStatusProbe(
            self.env.get("ONEBOT_WS_URL")
        )
        self._proxy_probe_object = ProxyProbe(self.env)
        self._openclaw_probe_object = OpenClawProbe(self.env, self.project_dir)
        self._queue_probe_object = QueueProbe(self.env)
        self._now_provider = now_provider or (
            lambda: datetime.now(tz=ZoneInfo("Asia/Shanghai"))
        )
        self._routing_probe_object = RoutingProbe(
            self.env, self.project_dir, now_provider=self._now_provider
        )
        self._bridge_probe = bridge_probe or self._bridge_probe_object.run
        self._onebot_probe = onebot_probe or self._onebot_probe_object.run
        self._proxy_probe = proxy_probe or self._proxy_probe_object.run
        self._openclaw_probe = openclaw_probe or self._openclaw_probe_object.run
        self._queue_probe = queue_probe or self._queue_probe_object.run
        self._routing_probe = routing_probe or self._routing_probe_object.run
        self._runner = runner or subprocess.run
        self._notifier = notifier or _mac_notification
        self._state_store = state_store
        napcat_config = self.env.get("NAPCAT_WEBUI_CONFIG_PATH", "").strip()
        self._onebot_recovery_object = (
            NapCatRestarter(
                self.env.get("NAPCAT_WEBUI_URL", "http://127.0.0.1:6099"),
                Path(os.path.expandvars(os.path.expanduser(napcat_config))),
            )
            if napcat_config
            else None
        )
        self._onebot_restarter = onebot_restarter or (
            self._onebot_recovery_object.run
            if self._onebot_recovery_object is not None
            else lambda: False
        )

    def check_once(self) -> Dict[str, object]:
        now = self._now_provider()
        previous = self._read_state()
        checks = {
            "bridge": bool(self._bridge_probe()),
            "onebot": bool(self._onebot_probe()),
            "proxy": bool(self._proxy_probe()),
            "openclaw": bool(self._openclaw_probe()),
            "queue": bool(self._queue_probe()),
            "routing": bool(self._routing_probe()),
        }
        failures = dict(previous.get("consecutive_failures", {}))
        for name, healthy in checks.items():
            failures[name] = 0 if healthy else int(failures.get(name, 0)) + 1

        history = _recent_restart_history(
            previous.get("restart_history", []),
            now,
            self.settings.restart_window_seconds,
        )
        onebot_history = _recent_restart_history(
            previous.get("onebot_restart_history", []),
            now,
            self.settings.restart_window_seconds,
        )
        onebot_recovery_blocked = bool(
            previous.get("onebot_recovery_blocked", False)
        ) and not checks["onebot"]
        status = "healthy"
        message = "全部检查正常。"

        dependency_failures = [
            label
            for key, label in (
                ("proxy", "代理"),
                ("openclaw", "OpenClaw Gateway"),
                ("queue", "消息队列"),
            )
            if not checks[key]
        ]
        routing_unhealthy = not checks["routing"]
        if not checks["onebot"]:
            status = "degraded"
            message = "OneBot 当前离线；正在确认是否为连续故障。"
            if onebot_recovery_blocked:
                status = "blocked"
                message = "OneBot 自动恢复已锁定，等待扫码或人工检查后重新上线。"
            elif (
                self.settings.onebot_auto_recovery
                and failures["onebot"]
                >= max(1, self.settings.onebot_failure_threshold)
            ):
                last_restart = _latest_datetime(onebot_history)
                in_cooldown = (
                    last_restart is not None
                    and now
                    < last_restart
                    + timedelta(seconds=self.settings.restart_cooldown_seconds)
                )
                if len(onebot_history) >= max(1, self.settings.max_restarts):
                    onebot_recovery_blocked = True
                    status = "blocked"
                    message = "OneBot 自动恢复已达到窗口上限，停止重启并等待扫码或人工检查。"
                elif in_cooldown:
                    message = "OneBot 仍离线，但处于自动恢复冷却期，暂不重复重启。"
                else:
                    onebot_history.append(now.isoformat(timespec="seconds"))
                    self._write_state(
                        {
                            "status": "recovering",
                            "message": "已预留一次 NapCat 自动重启，正在执行恢复。",
                            "checks": checks,
                            "consecutive_failures": failures,
                            "restart_history": history,
                            "restart_count": len(history),
                            "onebot_restart_history": onebot_history,
                            "onebot_restart_count": len(onebot_history),
                            "onebot_recovery_blocked": False,
                            "last_alert_key": str(
                                previous.get("last_alert_key", "")
                            ),
                            "updated_at": now.isoformat(timespec="seconds"),
                        }
                    )
                    try:
                        restarted = bool(self._onebot_restarter())
                    except Exception:
                        restarted = False
                    if restarted:
                        status = "recovering"
                        message = "已触发 NapCat 自动重启，正在等待 QQ 重新上线。"
                    else:
                        message = "NapCat 自动重启失败，等待下一轮检查。"
            if not checks["bridge"]:
                message = "%s 桥接进程同时异常。" % message
        elif checks["bridge"]:
            if dependency_failures:
                status = "degraded"
                message = "%s 不可用；桥接保持运行，等待外部依赖恢复。" % "、".join(dependency_failures)
            elif routing_unhealthy:
                status = "degraded"
                message = "影子路由不可用；桥接保持运行，等待路由观察恢复。"
        else:
            status = "degraded"
            message = "桥接进程异常，已连续检测失败 %d 次。" % failures["bridge"]
            if dependency_failures:
                message = "%s 不可用；暂不重启桥接，避免无效循环。" % "、".join(dependency_failures)
            elif failures["bridge"] >= max(1, self.settings.failure_threshold):
                last_restart = _latest_datetime(history)
                in_cooldown = (
                    last_restart is not None
                    and now < last_restart + timedelta(seconds=self.settings.restart_cooldown_seconds)
                )
                if len(history) >= max(1, self.settings.max_restarts):
                    status = "blocked"
                    message = "自动重启已达到窗口上限，停止重启并等待人工检查。"
                elif in_cooldown:
                    message = "桥接仍异常，但处于重启冷却期，暂不重复拉起。"
                else:
                    history.append(now.isoformat(timespec="seconds"))
                    result = self._restart_bridge()
                    if result:
                        status = "recovering"
                        message = "已触发一次受限自动重启，正在观察恢复结果。"
                    else:
                        status = "degraded"
                        message = "自动重启执行失败，等待下一轮检查。"

            if routing_unhealthy:
                message = "%s 影子路由仍异常。" % message

        if status == "healthy" and previous.get("status") in ("degraded", "blocked", "recovering"):
            message = "服务和依赖已经恢复正常。"

        alert_key = "%s:%s" % (status, message)
        previous_alert = previous.get("last_alert_key")
        should_notify = status != "healthy" or previous.get("status") in (
            "degraded", "blocked", "recovering"
        )
        state = {
            "status": status,
            "message": message,
            "checks": checks,
            "consecutive_failures": failures,
            "restart_history": history,
            "restart_count": len(history),
            "onebot_restart_history": onebot_history,
            "onebot_restart_count": len(onebot_history),
            "onebot_recovery_blocked": onebot_recovery_blocked,
            "last_alert_key": alert_key if should_notify else "",
            "updated_at": now.isoformat(timespec="seconds"),
        }
        self._record_health_changes(checks, now)
        self._write_state(state)
        if self.settings.notifications and should_notify and alert_key != previous_alert:
            try:
                self._notifier("chichu 自愈监控", message)
            except Exception as error:
                state["notification_error"] = error.__class__.__name__
                state["last_alert_key"] = str(previous_alert or "")
                self._write_state(state)
        return state

    def run_forever(self) -> None:
        while True:
            try:
                self.check_once()
            except Exception as exc:
                self._write_emergency_state(str(exc))
            time.sleep(max(5, self.settings.check_interval_seconds))

    def _restart_bridge(self) -> bool:
        result = self._runner(
            ["/bin/bash", str(self.service_script), "start"],
            cwd=str(self.project_dir),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        return result.returncode == 0

    def _probe_bridge(self) -> bool:
        return self._bridge_probe_object.run()

    def _probe_onebot(self) -> bool:
        return self._onebot_probe_object.run()

    def _probe_proxy(self) -> bool:
        return self._proxy_probe_object.run()

    def _probe_openclaw(self) -> bool:
        if _env_bool(self.env, "OPENCLAW_USE_GATEWAY", True):
            return _probe_url(
                self.env.get(
                    "OPENCLAW_GATEWAY_PROBE_URL", "ws://127.0.0.1:18789"
                ),
                default_port=18789,
            )
        return OpenClawProbe(self.env, self.project_dir).run()

    def _probe_queue(self) -> bool:
        return self._queue_probe_object.run()

    def _probe_routing(self) -> bool:
        return self._routing_probe_object.run()

    def _read_state(self) -> Dict[str, object]:
        try:
            value = json.loads(self.health_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_state(self, state: Dict[str, object]) -> None:
        self.health_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.health_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.health_path)

    def _write_emergency_state(self, detail: str) -> None:
        self._write_state(
            {
                "status": "degraded",
                "message": "watchdog 检查异常：%s" % detail[:200],
                "checks": {},
                "restart_history": [],
                "restart_count": 0,
                "updated_at": self._now_provider().isoformat(timespec="seconds"),
            }
        )

    def _record_health_changes(
        self, checks: Dict[str, bool], now: datetime
    ) -> None:
        if self._state_store is None:
            self._state_store = _existing_state_store(
                self.env, self.project_dir
            )
        if self._state_store is None:
            return
        try:
            repository = self._state_store.e4_repository
        except AttributeError:
            return
        for component, healthy in checks.items():
            try:
                repository.record_health_change(
                    component,
                    "healthy" if healthy else "unhealthy",
                    now=now,
                )
            except (OSError, sqlite3.Error):
                continue


def _read_env(path: Path) -> Dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    result = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _existing_state_store(
    env: Dict[str, str], project_dir: Path
) -> Optional[object]:
    if not _env_bool(env, "ENABLE_SQLITE_STORE", True):
        return None
    raw_path = env.get("STATE_DATABASE_PATH", "").strip()
    if not raw_path:
        return None
    database_path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not database_path.is_absolute():
        database_path = project_dir / database_path
    if not database_path.is_file():
        return None
    try:
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        return WorkspaceStateStore(database_path)
    except (OSError, sqlite3.Error, ValueError):
        return None


def _env_int(env: Dict[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(env: Dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _pid_file_running(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, OSError):
        return False


def _probe_url(raw: Optional[str], default_port: Optional[int] = None) -> bool:
    if not raw:
        return False
    parsed = urlparse(raw)
    host = parsed.hostname
    port = parsed.port or default_port
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _recent_restart_history(raw: object, now: datetime, window_seconds: int) -> List[str]:
    if not isinstance(raw, list):
        return []
    cutoff = now - timedelta(seconds=max(1, window_seconds))
    result = []
    for value in raw:
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None and now.tzinfo is not None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            if parsed >= cutoff:
                result.append(parsed.isoformat(timespec="seconds"))
        except ValueError:
            continue
    return result


def _latest_datetime(values: List[str]) -> Optional[datetime]:
    if not values:
        return None
    return max(datetime.fromisoformat(value) for value in values)


def _mac_notification(title: str, message: str) -> None:
    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    subprocess.run(
        ["/usr/bin/osascript", "-e", 'display notification "%s" with title "%s"' % (safe_message, safe_title)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
