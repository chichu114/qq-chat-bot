from datetime import datetime
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
from typing import Callable, Dict, Optional
from urllib.parse import urlparse


_MEDIA_SECRET_SERVICE = "qq-openclaw-media"
_MEDIA_SECRET_ACCOUNT = "media-api"
_MEDIA_SECRET_REF = "keychain:%s:%s" % (
    _MEDIA_SECRET_SERVICE,
    _MEDIA_SECRET_ACCOUNT,
)
_MODEL_IDENTIFIER = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z"
)
_MEDIA_SETTING_KEYS = {
    "enabled",
    "base_url",
    "image_model",
    "audio_model",
    "timeout_seconds",
    "requests_per_minute",
    "daily_budget",
    "max_concurrent_calls",
}
_ROUTING_CONFIG_KEYS = {
    "classifier_enabled",
    "classifier_model",
    "confidence_threshold_percent",
    "classifier_timeout_seconds",
    "max_daily_calls",
    "failure_threshold",
    "cooldown_seconds",
    "rule_version",
}
_ROUTING_VERDICTS = {"correct", "upgrade", "downgrade"}
_ROUTING_LEVELS = {"L0", "L1", "L2", "L3"}


class DashboardCommandService:
    def __init__(
        self,
        housekeeper_factory: Callable,
        style_factory: Callable,
        project_dir: Path,
        env_provider: Callable[[], Dict[str, str]],
        backup_manager,
        runner: Callable,
        bridge_script: Path,
        watchdog_script: Path,
        bridge_pid_path: Path,
        orchestration_repository=None,
        routing_repository=None,
        routing_model_provider: Optional[Callable[[], object]] = None,
    ) -> None:
        self._housekeeper_factory = housekeeper_factory
        self._style_factory = style_factory
        self._project_dir = project_dir
        self._env_provider = env_provider
        self._backup_manager = backup_manager
        self._runner = runner
        self._bridge_script = bridge_script
        self._watchdog_script = watchdog_script
        self._bridge_pid_path = bridge_pid_path
        self._orchestration_repository = orchestration_repository
        self._routing_repository = routing_repository
        self._routing_model_provider = routing_model_provider or (lambda: ())

    def add_memory(self, text: str) -> str:
        value = text.strip()
        if not value:
            raise ValueError("记忆内容不能为空")
        return self._housekeeper_factory().handle("记住：%s" % value) or "未识别记忆内容"

    def delete_memory(self, index: int) -> str:
        return self._housekeeper_factory().handle("删除记忆 %d" % index) or "删除失败"

    def clear_memories(self) -> str:
        return self._housekeeper_factory().handle("清空记忆") or "清空失败"

    def add_todo(self, command: str, user_id: Optional[int] = None) -> str:
        value = command.strip()
        if not value:
            raise ValueError("待办内容不能为空")
        if not any(marker in value for marker in ("待办", "提醒我")):
            value = "待办：%s" % value
        result = self._housekeeper_factory().handle(value, user_id=user_id)
        if result is None:
            raise ValueError("无法识别这条待办或提醒")
        return result

    def complete_todo(self, item_id: int) -> str:
        return self._housekeeper_factory().handle("完成待办 %d" % item_id) or "完成失败"

    def delete_todo(self, item_id: int) -> str:
        return self._housekeeper_factory().handle("删除待办 %d" % item_id) or "删除失败"

    def cancel_reminder(self, item_id: int) -> str:
        return self._housekeeper_factory().handle("取消提醒 %d" % item_id) or "取消失败"

    def reschedule_todo(
        self, item_id: int, due_hint: str, user_id: Optional[int]
    ) -> str:
        value = due_hint.strip()
        if not value:
            raise ValueError("改期时间不能为空")
        return (
            self._housekeeper_factory().handle(
                "把提醒 %d 改到%s" % (item_id, value), user_id=user_id
            )
            or "改期失败"
        )

    def delete_style(self, index: int) -> str:
        return self._style_factory().handle_command("删除风格 %d" % index) or "删除失败"

    def clear_styles(self) -> str:
        return self._style_factory().handle_command("清空风格反馈") or "清空失败"

    def service_action(self, action: str) -> str:
        if action not in ("start", "stop", "restart", "status"):
            raise ValueError("不支持的服务操作")
        env = self._env_provider()
        watchdog_enabled = _env_bool(env.get("ENABLE_WATCHDOG"), True)
        outputs = []
        if watchdog_enabled and action in ("stop", "restart"):
            outputs.append(self._run_service(self._watchdog_script, "stop"))
        bridge_action = "restart" if action == "restart" else action
        outputs.append(self._run_service(self._bridge_script, bridge_action))
        if watchdog_enabled and action in ("start", "restart"):
            outputs.append(self._run_service(self._watchdog_script, "start"))
        return "\n".join(value for value in outputs if value)

    def _run_service(self, script: Path, action: str) -> str:
        result = self._runner(
            ["/bin/bash", str(script), action],
            cwd=str(self._project_dir),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        output = (result.stdout or result.stderr or "操作完成").strip()
        if result.returncode != 0:
            raise RuntimeError(output)
        return output

    def default_user_id(self) -> Optional[int]:
        raw = self._env_provider().get("ALLOWED_QQ_USERS", "")
        for value in raw.split(","):
            try:
                return int(value.strip())
            except ValueError:
                continue
        return None

    def update_orchestration_mode(self, mode: str) -> str:
        if type(mode) is not str or mode not in ("legacy_direct", "direct"):
            raise ValueError("编排模式只能是 legacy_direct 或 direct")
        if self._orchestration_repository is None:
            raise ValueError("编排配置数据库未启用")

        env_path = self._project_dir / ".env"
        try:
            original_env = env_path.read_bytes()
        except FileNotFoundError:
            original_env = None
        _update_env_file(env_path, {"ORCHESTRATION_MODE": mode})
        try:
            config = self._orchestration_repository.create_config_version(
                {"mode": mode}, activate=True
            )
        except Exception:
            _restore_env_file(env_path, original_env)
            raise
        return "编排模式已保存为 %s（配置版本 %s），重启后生效。" % (
            mode,
            config["version"],
        )

    def update_routing_config(self, config: Dict[str, object]) -> str:
        repository = self._routing_repository_or_error()
        canonical = _routing_config(config)
        try:
            allowed = self._routing_model_provider()
        except Exception as exc:
            raise ValueError("路由模型白名单不可用") from exc
        if not isinstance(allowed, (list, tuple, set, frozenset)):
            raise ValueError("路由模型白名单不可用")
        identifiers = set()
        for item in allowed:
            identifiers.add(_model_identifier(item, "routing model"))
        if canonical["classifier_model"] not in identifiers:
            raise ValueError("分类模型不在允许列表中")
        version = repository.append_active_config(canonical)
        return "路由配置已保存为版本 %s。" % version["version"]

    def rollback_routing_config(self, target_version: int) -> str:
        repository = self._routing_repository_or_error()
        if type(target_version) is not int or target_version < 1:
            raise ValueError("target_version 必须是正整数")
        target = repository.get_config(target_version)
        if target is None:
            raise ValueError("路由配置版本不存在")
        version = repository.create_config(
            target["config"], activate=True, force_new=True
        )
        return "路由配置已回滚为新版本 %s。" % version["version"]

    def label_routing_decision(
        self,
        decision_id: int,
        user_id: int,
        verdict: str,
        correct_level: str,
        expected_updated_at: str,
        note: str = "",
    ) -> str:
        repository = self._routing_repository_or_error()
        if type(decision_id) is not int or decision_id < 1:
            raise ValueError("decision_id 必须是正整数")
        if type(user_id) is not int or user_id < 1:
            raise ValueError("未配置可管理的 QQ 用户")
        if type(verdict) is not str or verdict not in _ROUTING_VERDICTS:
            raise ValueError("verdict 不合法")
        if type(correct_level) is not str or correct_level not in _ROUTING_LEVELS:
            raise ValueError("correct_level 不合法")
        _routing_timestamp(expected_updated_at)
        if (
            type(note) is not str
            or len(note) > 300
            or any(not char.isprintable() for char in note)
        ):
            raise ValueError("note 必须是不超过 300 字符的可打印文本")
        repository.label(
            decision_id,
            user_id,
            verdict,
            correct_level,
            note,
            expected_updated_at,
        )
        return "路由决策已标注。"

    def _routing_repository_or_error(self):
        if self._routing_repository is None:
            raise ValueError("路由配置数据库未启用")
        return self._routing_repository

    def create_backup(self, prefix: str = "backup"):
        kind = "manual" if prefix in ("backup", "manual") else prefix
        return self._backup_manager.create_backup(kind)

    def list_backups(self):
        return self._backup_manager.list_backups()

    def verify_backup(self, name: str) -> str:
        result = self._backup_manager.verify_backup(name)
        if not result["valid"]:
            raise ValueError("备份校验失败：%s" % result["error"])
        return "校验通过，共 %d 个受管文件。" % result["file_count"]

    def prune_backups(self) -> str:
        removed = self._backup_manager.prune()
        return "备份保留策略执行完成，清理 %d 份过期自动备份。" % removed

    def restore_backup(self, name: str) -> str:
        was_running = _pid_is_running(_read_pid(self._bridge_pid_path))
        if was_running:
            self.service_action("stop")
        try:
            return self._backup_manager.restore_backup(name)
        finally:
            if was_running:
                self.service_action("start")

    def update_media_settings(self, settings: Dict[str, object]) -> str:
        if not isinstance(settings, dict) or not settings:
            raise ValueError("媒体设置不能为空")
        unknown = set(settings) - _MEDIA_SETTING_KEYS
        if unknown:
            raise ValueError("不支持的媒体设置：%s" % sorted(unknown)[0])

        updates = {}
        if "enabled" in settings:
            enabled = settings["enabled"]
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须是布尔值")
            updates["ENABLE_MEDIA_PIPELINE"] = "true" if enabled else "false"
        if "base_url" in settings:
            updates["MEDIA_API_BASE_URL"] = _media_base_url(settings["base_url"])
        if "image_model" in settings:
            updates["MEDIA_IMAGE_MODEL"] = _model_identifier(
                settings["image_model"], "image_model"
            )
        if "audio_model" in settings:
            updates["MEDIA_AUDIO_MODEL"] = _model_identifier(
                settings["audio_model"], "audio_model"
            )
        if "timeout_seconds" in settings:
            updates["MEDIA_API_TIMEOUT_SECONDS"] = str(
                _bounded_integer(settings["timeout_seconds"], "timeout_seconds", 1, 600)
            )
        if "requests_per_minute" in settings:
            updates["MEDIA_API_REQUESTS_PER_MINUTE"] = str(
                _bounded_integer(
                    settings["requests_per_minute"],
                    "requests_per_minute",
                    1,
                    600,
                )
            )
        if "daily_budget" in settings:
            updates["MEDIA_API_DAILY_BUDGET"] = _budget(settings["daily_budget"])
        if "max_concurrent_calls" in settings:
            updates["MEDIA_MAX_CONCURRENT_CALLS"] = str(
                _bounded_integer(
                    settings["max_concurrent_calls"],
                    "max_concurrent_calls",
                    1,
                    32,
                )
            )

        _update_env_file(self._project_dir / ".env", updates)
        return "媒体设置已更新，重启智能体后生效。"

    def replace_media_secret(self, secret: str) -> str:
        if not isinstance(secret, str):
            raise ValueError("媒体 API 密钥不能为空")
        value = secret.strip()
        if (
            not value
            or len(value) > 4096
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("媒体 API 密钥格式不合法")
        result = self._runner(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                _MEDIA_SECRET_SERVICE,
                "-a",
                _MEDIA_SECRET_ACCOUNT,
                "-w",
                value,
            ],
            cwd=str(self._project_dir),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("媒体 API 密钥写入 Keychain 失败")
        _update_env_file(
            self._project_dir / ".env",
            {
                "MEDIA_API_SECRET_REF": _MEDIA_SECRET_REF,
                "MEDIA_API_KEY": "",
            },
        )
        return "媒体 API 密钥已更新。"


def _read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_is_running(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _env_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _bounded_integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数" % name)
    if value < minimum or value > maximum:
        raise ValueError("%s 超出允许范围" % name)
    return value


def _budget(value: object) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("daily_budget 必须是非负数字") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000"):
        raise ValueError("daily_budget 超出允许范围")
    return format(amount.normalize(), "f") if amount else "0"


def _model_identifier(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError("%s 格式不合法" % name)
    normalized = value.strip()
    if len(normalized) > 128 or _MODEL_IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError("%s 格式不合法" % name)
    return normalized


def _routing_config(config: object) -> Dict[str, object]:
    if type(config) is not dict or set(config) != _ROUTING_CONFIG_KEYS:
        raise ValueError("路由配置字段不合法")
    if type(config["classifier_enabled"]) is not bool:
        raise ValueError("classifier_enabled 必须是布尔值")
    return {
        "classifier_enabled": config["classifier_enabled"],
        "classifier_model": _model_identifier(
            config["classifier_model"], "classifier_model"
        ),
        "confidence_threshold_percent": _bounded_integer(
            config["confidence_threshold_percent"],
            "confidence_threshold_percent",
            50,
            100,
        ),
        "classifier_timeout_seconds": _bounded_integer(
            config["classifier_timeout_seconds"],
            "classifier_timeout_seconds",
            1,
            120,
        ),
        "max_daily_calls": _bounded_integer(
            config["max_daily_calls"], "max_daily_calls", 0, 10000
        ),
        "failure_threshold": _bounded_integer(
            config["failure_threshold"], "failure_threshold", 1, 20
        ),
        "cooldown_seconds": _bounded_integer(
            config["cooldown_seconds"], "cooldown_seconds", 1, 3600
        ),
        "rule_version": _model_identifier(config["rule_version"], "rule_version"),
    }


def _routing_timestamp(value: object) -> None:
    if type(value) is not str or not value or len(value) > 64:
        raise ValueError("expected_updated_at 必须是带时区时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expected_updated_at 必须是带时区时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expected_updated_at 必须是带时区时间")


def _media_base_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("base_url 格式不合法")
    normalized = value.strip().rstrip("/")
    if not normalized or len(normalized) > 2048:
        raise ValueError("base_url 格式不合法")
    try:
        parsed = urlparse(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url 格式不合法") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url 格式不合法")
    if parsed.scheme == "http" and parsed.hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        raise ValueError("非本机媒体 API 必须使用 HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("base_url 端口不合法")
    return normalized


def _update_env_file(path: Path, updates: Dict[str, str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(updates)
    update_keys = set(updates)
    written = set()
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in update_keys:
                if key not in written:
                    output.append("%s=%s" % (key, updates[key]))
                    written.add(key)
                    remaining.pop(key, None)
                continue
        output.append(line)
    for key, value in remaining.items():
        output.append("%s=%s" % (key, value))

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.tmp" % path.name.lstrip("."))
    temporary.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def _restore_env_file(path: Path, original: Optional[bytes]) -> None:
    if original is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    temporary = path.with_name(".%s.rollback" % path.name.lstrip("."))
    temporary.write_bytes(original)
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
