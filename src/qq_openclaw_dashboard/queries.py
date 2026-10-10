import ipaddress
import json
import os
import re
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from qq_openclaw_bridge.domain.memory import extract_memory_items
from qq_openclaw_bridge.domain.routing import redacted_preview
from qq_openclaw_bridge.domain.scheduling import parse_metadata_datetime
from qq_openclaw_bridge.domain.todos import clean_todo_task_text, parse_todos
from qq_openclaw_bridge.style_memory import _extract_feedback_entries


_PUBLIC_MEDIA_STATUSES = ("pending", "ready", "processing", "failed", "expired")
_PUBLIC_ADAPTERS = ("echo", "http", "openclaw-cli")
_PUBLIC_BACKUP_KINDS = ("manual", "auto", "pre-restore", "unknown")
_PUBLIC_BACKUP_ENCRYPTIONS = ("encrypted", "legacy-unencrypted", "unknown")
_PUBLIC_HEALTH_STATUSES = (
    "healthy",
    "degraded",
    "recovering",
    "blocked",
    "stopped",
    "unknown",
)
_PUBLIC_HEALTH_MESSAGES = {
    "healthy": "自愈监控运行正常。",
    "degraded": "自愈监控检测到异常。",
    "recovering": "自愈监控正在恢复服务。",
    "blocked": "自愈监控已停止自动重启。",
    "stopped": "自愈监控当前未运行。",
    "unknown": "自愈监控尚未生成健康记录。",
}
_PUBLIC_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,95}\Z")
_PUBLIC_BACKUP_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,123}(?:\.zip|\.chichu-backup)\Z"
)
_PUBLIC_DNS_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_PUBLIC_LOG_SUMMARY = "运行日志已隐藏，请使用本机日志查看工具。"
_PUBLIC_MODEL_PROVIDERS = (
    "anthropic",
    "google",
    "openai",
    "openrouter",
    "xai",
)
_PUBLIC_ORCHESTRATION_MODES = ("legacy_direct", "direct")
_PUBLIC_ORCHESTRATION_STATUSES = (
    "received",
    "planned",
    "executing",
    "verifying",
    "accepted",
    "delivered",
    "fallback",
    "rejected",
    "failed",
    "cancelled",
)
_PUBLIC_FALLBACK_REASONS = (
    "budget_rejected",
    "verification_rejected",
)
_ROUTING_LEVELS = ("L0", "L1", "L2", "L3")
_ROUTING_CONFIG_FIELDS = (
    "classifier_enabled",
    "classifier_model",
    "confidence_threshold_percent",
    "classifier_timeout_seconds",
    "max_daily_calls",
    "failure_threshold",
    "cooldown_seconds",
    "rule_version",
)
_ROUTING_METRIC_FIELDS = (
    "decision_count",
    "labeled_count",
    "label_accuracy",
    "forced_risk_misses",
    "classifier_error_rate",
    "classifier_duration_p50_ms",
    "classifier_duration_p95_ms",
    "dispatch_overhead_p95_ms",
    "first_decision_at",
    "last_decision_at",
)
_PUBLIC_LOG_EVENTS = (
    ("connected to OneBot", "connected to OneBot WebSocket"),
    ("connected to OpenClaw Gateway", "connected to OpenClaw Gateway"),
    ("bridge started", "bridge started"),
)
_MANAGED_LAUNCH_AGENT_LABELS = frozenset(
    (
        "com.chichu.qq-bridge",
        "com.chichu.qq-dashboard",
        "com.chichu.qq-watchdog",
    )
)


def launch_agent_state(
    label: str,
    runner: Callable = subprocess.run,
    uid: Optional[int] = None,
):
    if label not in _MANAGED_LAUNCH_AGENT_LABELS:
        raise ValueError("unsupported LaunchAgent label")
    target = "gui/%d/%s" % (os.getuid() if uid is None else uid, label)
    try:
        result = runner(
            ["/bin/launchctl", "print", target],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    if result.returncode != 0:
        return False, None
    output = str(result.stdout or "")
    running = bool(re.search(r"(?m)^\s*state = running\s*$", output))
    pid_match = re.search(r"(?m)^\s*pid = ([0-9]+)\s*$", output)
    pid = int(pid_match.group(1)) if running and pid_match else None
    return running, pid


class DashboardQueryService:
    def __init__(
        self,
        project_dir: Path,
        memory_path: Path,
        todo_path: Path,
        style_path: Path,
        reminder_state_path: Path,
        state_store,
        backup_manager,
        now_provider: Callable[[], datetime],
        onebot_probe: Optional[Callable[[], bool]] = None,
        launch_agent_probe: Optional[Callable[[str], object]] = None,
    ) -> None:
        self.project_dir = project_dir
        self.memory_path = memory_path
        self.todo_path = todo_path
        self.style_path = style_path
        self.reminder_state_path = reminder_state_path
        self.state_store = state_store
        self.backup_manager = backup_manager
        self.now_provider = now_provider
        self.log_path = project_dir / "logs" / "bridge.log"
        self.pid_path = project_dir / "logs" / "bridge.pid"
        self.health_path = project_dir / "logs" / "health.json"
        self.watchdog_pid_path = project_dir / "logs" / "watchdog.pid"
        self._onebot_probe = onebot_probe
        self._launch_agent_probe = launch_agent_probe or launch_agent_state

    def get_state(self) -> Dict[str, object]:
        env = read_env_file(self.project_dir / ".env")
        now = self.now_provider()
        memory2 = self._memory2_state(env)
        memories = extract_memory_items(self._read_document("memory", self.memory_path))
        todos = parse_todos(self._read_document("todo", self.todo_path))
        styles = _extract_feedback_entries(self._read_document("style", self.style_path))
        running, pid = self._launch_agent_probe("com.chichu.qq-bridge")
        watchdog_running, watchdog_pid = self._launch_agent_probe(
            "com.chichu.qq-watchdog"
        )
        transport = (
            self.state_store.transport_stats()
            if self.state_store is not None
            else _empty_transport()
        )
        onebot = self._onebot_probe() if self._onebot_probe else probe_onebot(env)
        e4_state = self._e4_state(env, now)
        return {
            "service": {
                "running": running,
                "pid": pid if running else None,
                "watchdog_running": watchdog_running,
                "watchdog_pid": watchdog_pid if watchdog_running else None,
                "reminders_paused": "status=paused"
                in self._read_document("reminder_state", self.reminder_state_path),
            },
            "health": read_health(self.health_path, watchdog_running),
            "transport": transport,
            "connections": {
                "onebot": bool(onebot),
                "openclaw": (self.project_dir / "scripts" / "openclaw.sh").is_file(),
            },
            "config": {
                "adapter": public_enum(
                    env.get("AGENT_ADAPTER", "echo"), _PUBLIC_ADAPTERS
                ),
                "agent_id": public_agent_id(env.get("OPENCLAW_AGENT_ID", "main")),
                "model": public_model_config(
                    env.get("OPENCLAW_MODEL"), "使用 OpenClaw 默认模型"
                ),
                "memory_light_model": public_model_config(
                    env.get("MEMORY_LIGHT_MODEL"), "openai/gpt-5.4-nano"
                ),
                "onebot_url": public_host_port(env.get("ONEBOT_WS_URL")),
                "proxy_configured": any(
                    env.get(name) for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
                ),
                "security_confirmation": env_bool(env.get("ENABLE_SECURITY_CONFIRMATION"), True),
                "auto_backup": env_bool(env.get("ENABLE_AUTO_BACKUP"), True),
                "auto_backup_interval_hours": public_nonnegative_int(
                    self.backup_manager.interval_hours, default=1
                ),
                "auto_backup_retention_days": public_nonnegative_int(
                    self.backup_manager.retention_days, default=1
                ),
                "auto_backup_max_files": public_nonnegative_int(
                    self.backup_manager.max_auto_backups, default=1
                ),
                "watchdog": env_bool(env.get("ENABLE_WATCHDOG"), True),
                "sqlite_store": env_bool(env.get("ENABLE_SQLITE_STORE"), True),
                "gateway_mode": env_bool(env.get("OPENCLAW_USE_GATEWAY"), True),
                "gateway_url": public_host_port(
                    env.get(
                        "OPENCLAW_GATEWAY_PROBE_URL", "ws://127.0.0.1:18789"
                    )
                ),
            },
            "memories": [
                {"index": index, "date": item.created_at, "text": item.text}
                for index, item in enumerate(memories, 1)
            ],
            "memory2": memory2,
            "todos": [todo_dict(item) for item in todos],
            "styles": [
                {"index": index, "text": text}
                for index, text in enumerate(styles, 1)
            ],
            "logs": public_log_summary(self.log_path),
            "backups": public_backups(self.backup_manager.list_backups()),
            "orchestration": self._orchestration_state(env),
            "routing": self._routing_state(now),
            "updated_at": now.isoformat(timespec="seconds"),
            **e4_state,
        }

    def _routing_state(self, now: datetime) -> Dict[str, object]:
        repository = getattr(self.state_store, "routing_repository", None)
        active = repository.active_config() if repository is not None else None
        metrics = (
            public_routing_metrics(repository.metrics(now))
            if repository is not None
            else public_routing_metrics({})
        )
        decisions = (
            [public_routing_decision(item) for item in repository.recent_decisions(50)]
            if repository is not None
            else []
        )
        return {
            "config_version": active.get("version") if active is not None else None,
            "config": public_routing_config(
                active.get("config") if active is not None else None
            ),
            "metrics": metrics,
            "recent_decisions": decisions,
            "promotion_gate": routing_promotion_gate(metrics, now),
        }

    def _orchestration_state(self, env: Dict[str, str]) -> Dict[str, object]:
        mode = env.get("ORCHESTRATION_MODE", "legacy_direct").strip()
        if mode not in ("legacy_direct", "direct"):
            mode = "legacy_direct"

        repository = getattr(
            self.state_store, "orchestration_repository", None
        )
        active = repository.active_config() if repository is not None else None
        runs = repository.list_runs(limit=100) if repository is not None else []
        public_runs = public_orchestration_runs(runs)
        return {
            "mode": mode,
            "config_version": (
                active["version"] if active is not None else None
            ),
            "recent_runs": public_runs,
            "fallback_count": sum(
                1
                for run in public_runs
                if run.get("status") == "fallback"
                or bool(run.get("fallback_reason"))
            ),
            "ops_status": {
                "launch_agents": self._capability_status(
                    self.project_dir / "launchd"
                ),
                "logging": self._capability_status(
                    self.project_dir
                    / "src"
                    / "qq_openclaw_bridge"
                    / "runtime"
                    / "logging.py"
                ),
                "backup_encryption": self._capability_status(
                    self.project_dir
                    / "src"
                    / "qq_openclaw_bridge"
                    / "runtime"
                    / "backup_crypto.py"
                ),
            },
        }

    @staticmethod
    def _capability_status(path: Path) -> str:
        return "configured" if path.exists() else "pending"

    def _e4_state(
        self, env: Dict[str, str], now: datetime
    ) -> Dict[str, object]:
        media_settings = public_media_settings(env)
        empty = {
            "tasks": [],
            "model_stats": [],
            "approvals": [],
            "health_timeline": [],
            "media": {"total": 0, "settings": media_settings},
        }
        if self.state_store is None or not hasattr(
            self.state_store, "e4_repository"
        ):
            return empty

        current_utc = (
            now.replace(tzinfo=timezone.utc)
            if now.tzinfo is None
            else now.astimezone(timezone.utc)
        )
        repository = self.state_store.e4_repository
        tasks = repository.list_tasks(limit=100)
        model_stats = repository.model_stats(
            limit=100,
            since=current_utc - timedelta(hours=24),
            until=current_utc,
        )
        today_cost = float(repository.consumed_model_cost(current_utc.date()))
        daily_budget = optional_nonnegative_float(
            env.get("MEDIA_API_DAILY_BUDGET")
        )
        for item in model_stats:
            item["today_estimated_cost"] = today_cost
            item["daily_budget"] = daily_budget

        approvals = (
            self.state_store.list_recent_approvals(limit=100)
            if hasattr(self.state_store, "list_recent_approvals")
            else []
        )
        media_stats = repository.media_stats()
        media = {
            status: int(media_stats[status])
            for status in _PUBLIC_MEDIA_STATUSES
            if isinstance(media_stats.get(status), int)
        }
        media["total"] = int(media_stats.get("total", 0))
        media["settings"] = media_settings
        return {
            "tasks": public_rows(
                tasks,
                (
                    "id",
                    "user_id",
                    "kind",
                    "status",
                    "stage",
                    "cancel_requested",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "last_notified_at",
                ),
            ),
            "model_stats": public_model_stats(
                public_rows(
                    model_stats,
                    (
                        "provider",
                        "model",
                        "call_kind",
                        "calls",
                        "successes",
                        "error_rate",
                        "input_tokens",
                        "output_tokens",
                        "average_duration_ms",
                        "p50_latency_ms",
                        "p95_latency_ms",
                        "estimated_cost",
                        "known_input_token_calls",
                        "known_output_token_calls",
                        "known_duration_calls",
                        "known_cost_calls",
                        "today_estimated_cost",
                        "daily_budget",
                    ),
                )
            ),
            "approvals": public_approvals(
                public_rows(
                    approvals,
                    (
                        "approval_id",
                        "user_id",
                        "description",
                        "status",
                        "created_at",
                        "expires_at",
                    ),
                )
            ),
            "health_timeline": public_health_timeline(
                public_rows(
                    repository.health_timeline(limit=200),
                    (
                        "id",
                        "component",
                        "status",
                        "created_at",
                    ),
                )
            ),
            "media": media,
        }

    def _memory2_state(self, env: Dict[str, str]) -> Dict[str, object]:
        enabled = env_bool(env.get("ENABLE_MEMORY_2"), True)
        user_id = _first_user_id(env.get("ALLOWED_QQ_USERS", ""))
        if self.state_store is None or not hasattr(
            self.state_store, "memory_repository"
        ):
            return {
                "enabled": enabled,
                "light_model": public_model_config(
                    env.get("MEMORY_LIGHT_MODEL"), "openai/gpt-5.4-nano"
                ),
                "stats": {
                    "turns": 0,
                    "summaries": 0,
                    "pending": 0,
                    "confirmed": 0,
                    "rejected": 0,
                },
                "pending": [],
                "recent_summaries": [],
                "latest_analysis": None,
            }
        repository = self.state_store.memory_repository
        active_user = user_id or 0
        pending = repository.list_facts(active_user, status="pending", limit=30)
        summaries = repository.recent_summaries(active_user, limit=5)
        latest = repository.latest_analysis_run(active_user)
        if latest is not None and latest.get("detail"):
            latest = dict(latest)
            latest["detail"] = str(latest["detail"])[:160]
        if latest is not None:
            latest = dict(latest)
            latest["model"] = public_identifier(latest.get("model"))
        return {
            "enabled": enabled,
            "light_model": public_model_config(
                env.get("MEMORY_LIGHT_MODEL"), "openai/gpt-5.4-nano"
            ),
            "stats": repository.stats(active_user),
            "pending": [
                {
                    "id": item["id"],
                    "layer": item["layer"],
                    "category": item["category"],
                    "content": item["content"],
                    "confidence": item["confidence"],
                    "created_at": item["created_at"],
                }
                for item in pending
            ],
            "recent_summaries": [
                {
                    "id": item["id"],
                    "content": item["content"],
                    "model": public_identifier(item.get("model")),
                    "created_at": item["created_at"],
                }
                for item in summaries
            ],
            "latest_analysis": latest,
        }

    def _read_document(self, name: str, path: Path) -> str:
        if self.state_store is not None:
            defaults = {
                "memory": "# chichu MEMORY\n",
                "todo": "# chichu TODO\n\n## 待办\n",
                "style": "# chichu STYLE\n",
                "reminder_state": "status=active\n",
            }
            return self.state_store.read_document(name, path, defaults[name])
        return read_text(path)


def todo_dict(item) -> Dict[str, object]:
    due = parse_metadata_datetime(item.metadata.get("due"), ZoneInfo("Asia/Shanghai"))
    return {
        "id": item.item_id,
        "done": item.done,
        "created_at": item.created_at,
        "text": clean_todo_task_text(item.text),
        "display_text": item.text,
        "category": item.metadata.get("category"),
        "due": due.isoformat(timespec="seconds") if due else None,
        "repeat": item.metadata.get("repeat"),
        "reminded": bool(item.metadata.get("reminded")),
        "user_id": item.metadata.get("user"),
    }


def read_env_file(path: Path) -> Dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def public_log_summary(path: Path) -> str:
    raw = read_text(path)
    if not raw:
        return ""
    events = []
    for marker, public_text in _PUBLIC_LOG_EVENTS:
        if marker in raw and public_text not in events:
            events.append(public_text)
    return "\n".join(events) if events else _PUBLIC_LOG_SUMMARY


def public_enum(value, allowed, fallback: str = "unknown") -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in allowed else fallback


def public_config_state(value) -> str:
    return "configured" if isinstance(value, str) and value.strip() else "default"


def public_agent_id(value) -> str:
    return "main" if isinstance(value, str) and value.strip() == "main" else public_config_state(value)


def public_model_config(value, fallback: str) -> str:
    identifier = public_identifier(value, fallback="")
    if "/" not in identifier:
        return public_config_state(value) if value else fallback
    provider, model = identifier.split("/", 1)
    if provider not in _PUBLIC_MODEL_PROVIDERS or not model:
        return public_config_state(value)
    return identifier


def public_identifier(value, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    if (
        _PUBLIC_IDENTIFIER.fullmatch(normalized) is None
        or "//" in normalized
        or any(part in ("", ".", "..") for part in normalized.split("/"))
    ):
        return fallback
    return normalized


def public_host_port(value, fallback: str = "未配置") -> str:
    if not isinstance(value, str):
        return fallback
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError:
        return fallback
    if parsed.scheme.lower() not in ("http", "https", "ws", "wss"):
        return fallback
    host = parsed.hostname
    if not host or port is None:
        return fallback
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            normalized_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return fallback
        if (
            _PUBLIC_DNS_HOST.fullmatch(normalized_host) is None
            or ".." in normalized_host
        ):
            return fallback
    else:
        normalized_host = "[%s]" % address if address.version == 6 else str(address)
    return "%s:%d" % (normalized_host, port)


def public_media_settings(env: Dict[str, str]) -> Dict[str, object]:
    return {
        "enabled": env_bool(env.get("ENABLE_MEDIA_PIPELINE"), False),
        "base_url": public_media_base_url(env.get("MEDIA_API_BASE_URL")),
        "image_model": public_identifier(
            env.get("MEDIA_IMAGE_MODEL"), fallback=""
        ),
        "audio_model": public_identifier(
            env.get("MEDIA_AUDIO_MODEL"), fallback=""
        ),
        "timeout_seconds": public_env_int(
            env.get("MEDIA_API_TIMEOUT_SECONDS"), 90, 1, 600
        ),
        "requests_per_minute": public_env_int(
            env.get("MEDIA_API_REQUESTS_PER_MINUTE"), 10, 1, 600
        ),
        "daily_budget": optional_nonnegative_float(
            env.get("MEDIA_API_DAILY_BUDGET")
        ),
        "max_concurrent_calls": public_env_int(
            env.get("MEDIA_MAX_CONCURRENT_CALLS"), 2, 1, 32
        ),
        "secret_configured": bool(
            env.get("MEDIA_API_SECRET_REF") or env.get("MEDIA_API_KEY")
        ),
    }


def public_media_base_url(value: Optional[str]) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().rstrip("/")
    if not normalized or len(normalized) > 2048:
        return ""
    try:
        parsed = urlparse(normalized)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    if parsed.scheme == "http" and parsed.hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        return ""
    if port is not None and not 1 <= port <= 65535:
        return ""
    return normalized


def public_env_int(
    raw: Optional[str], default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(raw.strip()) if isinstance(raw, str) else default
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def public_nonnegative_int(value, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < 0 or value > 2**63 - 1:
        return default
    return value


def public_timestamp(value) -> Optional[str]:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds")


def public_backups(rows):
    result = []
    if not isinstance(rows, (list, tuple)):
        return result
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        basename = row["name"].replace("\\", "/").rsplit("/", 1)[-1]
        if _PUBLIC_BACKUP_NAME.fullmatch(basename) is None:
            continue
        result.append(
            {
                "name": basename,
                "size": public_nonnegative_int(row.get("size")),
                "mtime": public_timestamp(row.get("mtime")),
                "created_at": public_timestamp(row.get("created_at")),
                "kind": public_enum(row.get("kind"), _PUBLIC_BACKUP_KINDS),
                "encryption": public_enum(
                    row.get("encryption"), _PUBLIC_BACKUP_ENCRYPTIONS
                ),
                "valid": row.get("valid") is True,
                "file_count": public_nonnegative_int(row.get("file_count")),
            }
        )
    return result


def public_model_stats(rows):
    for row in rows:
        row["provider"] = public_identifier(row.get("provider"))
        row["model"] = public_identifier(row.get("model"))
        row["call_kind"] = public_identifier(row.get("call_kind"))
    return rows


def public_approvals(rows):
    for row in rows:
        row["approval_id"] = public_identifier(row.get("approval_id"))
        row["description"] = "待确认的敏感操作"
        row["status"] = public_identifier(row.get("status"))
        row["created_at"] = public_timestamp(row.get("created_at"))
        row["expires_at"] = public_timestamp(row.get("expires_at"))
    return rows


def public_health_timeline(rows):
    for row in rows:
        row["component"] = public_identifier(row.get("component"))
        row["status"] = public_identifier(row.get("status"))
        row["created_at"] = public_timestamp(row.get("created_at"))
    return rows


def public_orchestration_runs(rows):
    result = []
    for row in public_rows(
        rows,
        (
            "run_id",
            "mode",
            "risk_level",
            "status",
            "config_version",
            "started_at",
            "finished_at",
            "fallback_reason",
            "safe_error_class",
        ),
    ):
        result.append(
            {
                "run_id": public_identifier(row.get("run_id")),
                "mode": public_enum(
                    row.get("mode"), _PUBLIC_ORCHESTRATION_MODES
                ),
                "risk_level": public_identifier(row.get("risk_level")),
                "status": public_enum(
                    row.get("status"), _PUBLIC_ORCHESTRATION_STATUSES
                ),
                "config_version": public_nonnegative_int(
                    row.get("config_version")
                ),
                "started_at": public_timestamp(row.get("started_at")),
                "finished_at": public_timestamp(row.get("finished_at")),
                "fallback_reason": public_enum(
                    row.get("fallback_reason"),
                    _PUBLIC_FALLBACK_REASONS,
                    fallback="",
                ),
                "safe_error_class": public_identifier(
                    row.get("safe_error_class"), fallback=""
                ),
            }
        )
    return result


def public_rows(rows, fields):
    return [{field: row.get(field) for field in fields} for row in rows]


def optional_nonnegative_float(raw: Optional[str]) -> Optional[float]:
    if raw is None or not raw.strip():
        return None
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return float(value)


def read_health(path: Path, watchdog_running: bool) -> Dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    checks = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    status = (
        public_enum(raw.get("status"), _PUBLIC_HEALTH_STATUSES)
        if watchdog_running
        else "stopped"
    )
    return {
        "status": status,
        "message": _PUBLIC_HEALTH_MESSAGES[status],
        "checks": {
            name: bool(checks.get(name, False))
            for name in ("bridge", "onebot", "proxy", "openclaw", "queue")
        },
        "restart_count": public_nonnegative_int(raw.get("restart_count")),
        "updated_at": public_timestamp(raw.get("updated_at")),
    }


def read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def pid_is_running(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def probe_onebot(env: Dict[str, str]) -> bool:
    parsed = urlparse(env.get("ONEBOT_WS_URL", ""))
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.25):
            return True
    except OSError:
        return False


def env_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _empty_transport() -> Dict[str, object]:
    return {
        "outbox_pending": 0,
        "outbox_sending": 0,
        "outbox_failed": 0,
        "active_inbound": 0,
        "last_agent_success_at": None,
        "last_delivery_success_at": None,
        "circuit_state": "closed",
    }


def _first_user_id(raw: str) -> Optional[int]:
    for item in raw.split(","):
        try:
            return int(item.strip())
        except ValueError:
            continue
    return None


def public_routing_config(config) -> Dict[str, object]:
    if type(config) is not dict:
        return {}
    return {
        name: config[name]
        for name in _ROUTING_CONFIG_FIELDS
        if name in config
    }


def public_routing_decision(decision) -> Dict[str, object]:
    if type(decision) is not dict:
        return {}
    result = {
        name: decision.get(name)
        for name in (
            "id",
            "routing_config_version",
            "rule_version",
            "status",
            "rule_level",
            "model_level",
            "final_level",
            "confidence",
            "task_kind",
            "classifier_duration_ms",
            "created_at",
            "updated_at",
        )
    }
    preview = decision.get("message_preview")
    result["message_preview"] = redacted_preview(
        preview if isinstance(preview, str) else "", 160
    )
    result["labeled"] = bool(decision.get("labeled"))
    return result


def public_routing_metrics(metrics) -> Dict[str, object]:
    source = metrics if type(metrics) is dict else {}
    result = {name: source.get(name) for name in _ROUTING_METRIC_FIELDS}
    result["per_level_counts"] = {
        level: _public_nonnegative_int(
            source.get("per_level_counts", {}).get(level)
            if type(source.get("per_level_counts")) is dict
            else None
        )
        for level in _ROUTING_LEVELS
    }
    matrix = source.get("confusion_matrix")
    result["confusion_matrix"] = {
        predicted: {
            correct: _public_nonnegative_int(
                matrix.get(predicted, {}).get(correct)
                if type(matrix) is dict and type(matrix.get(predicted)) is dict
                else None
            )
            for correct in _ROUTING_LEVELS
        }
        for predicted in _ROUTING_LEVELS
    }
    return result


def routing_promotion_gate(metrics: Dict[str, object], now: datetime) -> Dict[str, bool]:
    first = metrics.get("first_decision_at")
    try:
        started = datetime.fromisoformat(first) if isinstance(first, str) else None
    except ValueError:
        started = None
    seven_days = bool(
        started is not None
        and started.tzinfo is not None
        and started <= now - timedelta(days=7)
    )
    accuracy = metrics.get("label_accuracy")
    overhead = metrics.get("dispatch_overhead_p95_ms")
    checks = {
        "seven_days": seven_days,
        "hundred_decisions": _public_nonnegative_int(metrics.get("decision_count")) >= 100,
        "thirty_labels": _public_nonnegative_int(metrics.get("labeled_count")) >= 30,
        "ninety_percent_accuracy": (
            type(accuracy) in (int, float) and accuracy >= 0.9
        ),
        "no_forced_risk_misses": _public_nonnegative_int(
            metrics.get("forced_risk_misses")
        ) == 0,
        "dispatch_overhead_p95": (
            type(overhead) in (int, float) and overhead <= 1500
        ),
    }
    checks["eligible"] = all(checks.values())
    return checks


def _public_nonnegative_int(value) -> int:
    return value if type(value) is int and value >= 0 else 0
