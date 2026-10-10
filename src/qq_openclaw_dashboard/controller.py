import json
import os
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from qq_openclaw_bridge.backups import BackupManager
from qq_openclaw_bridge.housekeeper import Housekeeper
from qq_openclaw_bridge.style_memory import StyleMemory
from qq_openclaw_bridge.storage import WorkspaceStateStore
from .commands import DashboardCommandService
from .queries import DashboardQueryService


DEFAULT_WORKSPACE = "~/.openclaw/workspace"
MANAGED_WORKSPACE_FILES = (
    "MEMORY.md",
    "TODO.md",
    "STYLE.md",
    "REMINDER_STATE.md",
)
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_MODEL_IDENTIFIER = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z"
)


class DashboardController:
    def __init__(
        self,
        project_dir: Path,
        memory_path: Path,
        todo_path: Path,
        style_path: Path,
        reminder_state_path: Path,
        runner: Optional[Callable] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        onebot_probe: Optional[Callable[[], bool]] = None,
        backup_dir: Optional[Path] = None,
        state_store: Optional[WorkspaceStateStore] = None,
        backup_key_provider: Optional[object] = None,
        launch_agent_probe: Optional[Callable[[str], object]] = None,
        routing_model_provider: Optional[Callable[[], object]] = None,
    ) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.memory_path = Path(memory_path).expanduser()
        self.todo_path = Path(todo_path).expanduser()
        self.style_path = Path(style_path).expanduser()
        self.reminder_state_path = Path(reminder_state_path).expanduser()
        self.log_path = self.project_dir / "logs" / "bridge.log"
        self.pid_path = self.project_dir / "logs" / "bridge.pid"
        self.health_path = self.project_dir / "logs" / "health.json"
        self.watchdog_pid_path = self.project_dir / "logs" / "watchdog.pid"
        self.service_script = self.project_dir / "scripts" / "bridge_service.sh"
        self.watchdog_script = self.project_dir / "scripts" / "watchdog_service.sh"
        self._runner = runner or subprocess.run
        self._now_provider = now_provider or (
            lambda: datetime.now(tz=ZoneInfo("Asia/Shanghai"))
        )
        self._env = _read_env_file(self.project_dir / ".env")
        self.state_store = state_store
        self._onebot_probe = onebot_probe or self._probe_onebot
        configured_backup_dir = Path(
            backup_dir or self._env.get("AUTO_BACKUP_DIR", "backups")
        ).expanduser()
        if not configured_backup_dir.is_absolute():
            configured_backup_dir = self.project_dir / configured_backup_dir
        self.backup_dir = configured_backup_dir
        self.backup_manager = BackupManager(
            backup_dir=self.backup_dir,
            sources=self._managed_sources(),
            now_provider=self._now_provider,
            interval_hours=_env_int(self._env.get("AUTO_BACKUP_INTERVAL_HOURS"), 24),
            retention_days=_env_int(self._env.get("AUTO_BACKUP_RETENTION_DAYS"), 30),
            max_auto_backups=_env_int(self._env.get("AUTO_BACKUP_MAX_FILES"), 30),
            key_provider=backup_key_provider,
        )
        self._query_service = DashboardQueryService(
            self.project_dir,
            self.memory_path,
            self.todo_path,
            self.style_path,
            self.reminder_state_path,
            self.state_store,
            self.backup_manager,
            self._now_provider,
            self._onebot_probe,
            launch_agent_probe,
        )
        self._command_service = DashboardCommandService(
            self._housekeeper,
            self._style_memory,
            self.project_dir,
            lambda: _read_env_file(self.project_dir / ".env"),
            self.backup_manager,
            self._runner,
            self.service_script,
            self.watchdog_script,
            self.pid_path,
            getattr(self.state_store, "orchestration_repository", None),
            getattr(self.state_store, "routing_repository", None),
            routing_model_provider,
        )

    @classmethod
    def from_project(cls, project_dir: Path) -> "DashboardController":
        project = Path(project_dir).expanduser().resolve()
        env = _read_env_file(project / ".env")
        workspace = Path(os.path.expanduser(env.get("OPENCLAW_WORKSPACE", DEFAULT_WORKSPACE)))
        memory_path = _env_path(env, "HOUSEKEEPER_MEMORY_PATH", workspace / "MEMORY.md")
        todo_path = _env_path(env, "HOUSEKEEPER_TODO_PATH", workspace / "TODO.md")
        style_path = _env_path(env, "STYLE_MEMORY_PATH", workspace / "STYLE.md")
        reminder_state_path = _env_path(
            env, "REMINDER_STATE_PATH", workspace / "REMINDER_STATE.md"
        )
        openclaw_config_path = Path(
            os.path.expanduser(
                env.get("OPENCLAW_CONFIG_PATH", "~/.openclaw/openclaw.json")
            )
        )
        state_store = None
        if _env_bool(env.get("ENABLE_SQLITE_STORE"), True):
            database_path = _env_path(
                env, "STATE_DATABASE_PATH", workspace / "chichu.sqlite3"
            )
            state_store = WorkspaceStateStore(database_path)
            state_store.ensure_documents(
                {
                    "memory": (memory_path, "# chichu MEMORY\n"),
                    "todo": (todo_path, "# chichu TODO\n\n## 待办\n"),
                    "style": (style_path, "# chichu STYLE\n"),
                    "reminder_state": (reminder_state_path, "status=active\n"),
                }
            )
        return cls(
            project_dir=project,
            memory_path=memory_path,
            todo_path=todo_path,
            style_path=style_path,
            reminder_state_path=reminder_state_path,
            state_store=state_store,
            routing_model_provider=_openclaw_model_identifier_provider(
                openclaw_config_path
            ),
        )

    def get_state(self) -> Dict[str, object]:
        self._env = _read_env_file(self.project_dir / ".env")
        return self._query_service.get_state()

    def add_memory(self, text: str) -> str:
        return self._command_service.add_memory(text)

    def delete_memory(self, index: int) -> str:
        return self._command_service.delete_memory(index)

    def clear_memories(self) -> str:
        return self._command_service.clear_memories()

    def confirm_memory_candidate(self, fact_id: int) -> str:
        if self.state_store is None:
            raise ValueError("E3 记忆数据库未启用")
        user_id = self.default_user_id() or 0
        fact = self.state_store.memory_repository.confirm_fact(user_id, fact_id)
        if fact is None or fact["status"] != "confirmed":
            raise ValueError("候选记忆不存在或已经处理")
        self._housekeeper().handle("记住：%s" % fact["content"], user_id=user_id)
        return "已确认记忆 #%d：%s" % (fact["id"], fact["content"])

    def reject_memory_candidate(self, fact_id: int) -> str:
        if self.state_store is None:
            raise ValueError("E3 记忆数据库未启用")
        user_id = self.default_user_id() or 0
        fact = self.state_store.memory_repository.reject_fact(user_id, fact_id)
        if fact is None or fact["status"] != "rejected":
            raise ValueError("候选记忆不存在或已经处理")
        return "已拒绝记忆 #%d。" % fact["id"]

    def add_todo(self, command: str, user_id: Optional[int] = None) -> str:
        return self._command_service.add_todo(command, user_id)

    def complete_todo(self, item_id: int) -> str:
        return self._command_service.complete_todo(item_id)

    def delete_todo(self, item_id: int) -> str:
        return self._command_service.delete_todo(item_id)

    def cancel_reminder(self, item_id: int) -> str:
        return self._command_service.cancel_reminder(item_id)

    def reschedule_todo(self, item_id: int, due_hint: str, user_id: Optional[int]) -> str:
        return self._command_service.reschedule_todo(item_id, due_hint, user_id)

    def delete_style(self, index: int) -> str:
        return self._command_service.delete_style(index)

    def clear_styles(self) -> str:
        return self._command_service.clear_styles()

    def service_action(self, action: str) -> str:
        return self._command_service.service_action(action)

    def default_user_id(self) -> Optional[int]:
        return self._command_service.default_user_id()

    def update_orchestration_mode(self, mode: str) -> str:
        return self._command_service.update_orchestration_mode(mode)

    def update_routing_config(self, config: Dict[str, object]) -> str:
        return self._command_service.update_routing_config(config)

    def rollback_routing_config(self, target_version: int) -> str:
        return self._command_service.rollback_routing_config(target_version)

    def label_routing_decision(
        self,
        decision_id: int,
        verdict: str,
        correct_level: str,
        expected_updated_at: str,
        note: str = "",
    ) -> str:
        user_id = self.default_user_id()
        if user_id is None:
            raise ValueError("未配置可管理的 QQ 用户")
        return self._command_service.label_routing_decision(
            decision_id,
            user_id,
            verdict,
            correct_level,
            expected_updated_at,
            note,
        )

    def create_backup(self, prefix: str = "backup") -> Dict[str, object]:
        return self._command_service.create_backup(prefix)

    def list_backups(self) -> List[Dict[str, object]]:
        return self._command_service.list_backups()

    def verify_backup(self, name: str) -> str:
        return self._command_service.verify_backup(name)

    def prune_backups(self) -> str:
        return self._command_service.prune_backups()

    def restore_backup(self, name: str) -> str:
        return self._command_service.restore_backup(name)

    def cancel_task(self, task_id: str) -> str:
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise ValueError("任务 ID 不合法")
        if self.state_store is None:
            raise ValueError("长任务数据库未启用")
        user_id = self.default_user_id()
        if user_id is None:
            raise ValueError("未配置可管理的 QQ 用户")
        task = self.state_store.e4_repository.request_cancel(task_id, user_id)
        if task is None:
            raise ValueError("任务不存在、不可取消或不属于当前用户")
        return "任务 %s 已请求取消。" % task_id

    def update_media_settings(self, settings: Dict[str, object]) -> str:
        return self._command_service.update_media_settings(settings)

    def replace_media_secret(self, secret: str) -> str:
        return self._command_service.replace_media_secret(secret)

    def _housekeeper(self) -> Housekeeper:
        now = self._now_provider()
        return Housekeeper(
            memory_path=self.memory_path,
            todo_path=self.todo_path,
            reminder_state_path=self.reminder_state_path,
            date_provider=lambda: now.date(),
            now_provider=lambda: now,
            state_store=self.state_store,
        )

    def _style_memory(self) -> StyleMemory:
        today = self._now_provider().date()
        return StyleMemory(
            self.style_path,
            date_provider=lambda: today,
            state_store=self.state_store,
        )

    def _managed_sources(self) -> Dict[str, Path]:
        workspace_dir = self.memory_path.parent
        sources = {
            "project/.env": self.project_dir / ".env",
            "workspace/MEMORY.md": self.memory_path,
            "workspace/TODO.md": self.todo_path,
            "workspace/STYLE.md": self.style_path,
            "workspace/REMINDER_STATE.md": self.reminder_state_path,
        }
        for filename in (
            "AGENTS.md",
            "SOUL.md",
            "IDENTITY.md",
            "USER.md",
            "TOOLS.md",
            "HEARTBEAT.md",
        ):
            sources["workspace/%s" % filename] = workspace_dir / filename
        if self.state_store is not None:
            sources["workspace/chichu.sqlite3"] = self.state_store.database_path
        return sources

    def _probe_onebot(self) -> bool:
        raw_url = self._env.get("ONEBOT_WS_URL", "")
        parsed = urlparse(raw_url)
        if not parsed.hostname or not parsed.port:
            return False
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=0.25):
                return True
        except OSError:
            return False


def _read_env_file(path: Path) -> Dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values: Dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_path(env: Dict[str, str], name: str, default: Path) -> Path:
    raw = env.get(name, "").strip()
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else Path(default)


def _env_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(raw: Optional[str], default: int) -> int:
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _openclaw_model_identifier_provider(config_path: Path) -> Callable[[], object]:
    path = Path(config_path).expanduser()

    def provider():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            models = payload["agents"]["defaults"]["models"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("OpenClaw 模型列表不可用") from exc
        if type(models) is not dict:
            raise ValueError("OpenClaw 模型列表不可用")
        return frozenset(
            identifier
            for identifier in models
            if (
                type(identifier) is str
                and len(identifier) <= 128
                and _MODEL_IDENTIFIER.fullmatch(identifier) is not None
            )
        )

    return provider
