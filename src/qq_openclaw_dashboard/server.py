import hmac
import json
import logging
import mimetypes
import re
import secrets
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from .controller import DashboardController


STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024
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
_ROUTING_LABEL_KEYS = {
    "decision_id",
    "verdict",
    "correct_level",
    "expected_updated_at",
}
_SERVER_LOGGER = logging.getLogger("qq_openclaw_dashboard.server")
_SAFE_ERROR_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def create_server(
    controller: DashboardController,
    host: str = "127.0.0.1",
    port: int = 18790,
    token: Optional[str] = None,
) -> ThreadingHTTPServer:
    dashboard_token = token or secrets.token_urlsafe(32)

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "chichu-dashboard/1.0"

        def do_GET(self) -> None:
            if not self._host_allowed():
                self._json_error(HTTPStatus.FORBIDDEN, "Host 不受信任")
                return
            path = urlparse(self.path).path
            if path == "/api/state":
                self._json_response({"ok": True, **controller.get_state()})
                return
            if path == "/":
                self._serve_file(STATIC_DIR / "index.html", inject_token=True)
                return
            if path.startswith("/assets/"):
                asset_name = Path(path).name
                self._serve_file(STATIC_DIR / asset_name)
                return
            self._json_error(HTTPStatus.NOT_FOUND, "页面不存在")

        def do_POST(self) -> None:
            if not self._host_allowed() or not self._origin_allowed():
                self._json_error(HTTPStatus.FORBIDDEN, "请求来源不受信任")
                return
            supplied_token = self.headers.get("X-Dashboard-Token", "")
            if not hmac.compare_digest(supplied_token, dashboard_token):
                self._json_error(HTTPStatus.FORBIDDEN, "面板令牌无效")
                return
            try:
                payload = self._read_json()
                message = self._dispatch_post(urlparse(self.path).path, payload)
                self._json_response({"ok": True, "message": message})
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                correlation_id = secrets.token_hex(12)
                error_class = exc.__class__.__name__
                if _SAFE_ERROR_CLASS.fullmatch(error_class) is None:
                    error_class = "UnexpectedError"
                _SERVER_LOGGER.error(
                    "dashboard_request_failed correlation_id=%s error_class=%s",
                    correlation_id,
                    error_class,
                )
                self._json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "服务器内部错误，请稍后重试。",
                )

        def _dispatch_post(self, path: str, payload: Dict[str, object]) -> str:
            if path == "/api/routing/config":
                return controller.update_routing_config(_routing_config(payload))
            if path == "/api/routing/config/rollback":
                _exact_keys(payload, {"target_version"})
                return controller.rollback_routing_config(
                    _strict_positive_integer(payload, "target_version")
                )
            if path == "/api/routing/label":
                _exact_keys(payload, _ROUTING_LABEL_KEYS, {"note"})
                return controller.label_routing_decision(
                    _strict_positive_integer(payload, "decision_id"),
                    _routing_enum(payload, "verdict", {"correct", "upgrade", "downgrade"}),
                    _routing_enum(payload, "correct_level", {"L0", "L1", "L2", "L3"}),
                    _routing_timestamp(payload),
                    _routing_note(payload),
                )
            if path.startswith("/api/service/"):
                return controller.service_action(path.rsplit("/", 1)[-1])
            if path.startswith("/api/tasks/") and path.endswith("/cancel"):
                task_id = path[len("/api/tasks/") : -len("/cancel")]
                if not task_id or "/" in task_id:
                    raise ValueError("任务 ID 不合法")
                return controller.cancel_task(task_id)
            if path == "/api/media/settings":
                return controller.update_media_settings(payload)
            if path == "/api/media/secret":
                return controller.replace_media_secret(_string(payload, "key"))
            if path == "/api/orchestration/mode":
                return controller.update_orchestration_mode(
                    _exact_string(payload, "mode")
                )
            if path == "/api/memories":
                return controller.add_memory(_string(payload, "text"))
            if path == "/api/memories/delete":
                return controller.delete_memory(_integer(payload, "index"))
            if path == "/api/memories/clear":
                return controller.clear_memories()
            if path == "/api/memory-candidates/confirm":
                return controller.confirm_memory_candidate(_integer(payload, "id"))
            if path == "/api/memory-candidates/reject":
                return controller.reject_memory_candidate(_integer(payload, "id"))
            if path == "/api/todos":
                return controller.add_todo(
                    _string(payload, "command"), user_id=controller.default_user_id()
                )
            if path == "/api/todos/complete":
                return controller.complete_todo(_integer(payload, "id"))
            if path == "/api/todos/delete":
                return controller.delete_todo(_integer(payload, "id"))
            if path == "/api/todos/cancel-reminder":
                return controller.cancel_reminder(_integer(payload, "id"))
            if path == "/api/todos/reschedule":
                return controller.reschedule_todo(
                    _integer(payload, "id"),
                    _string(payload, "due"),
                    controller.default_user_id(),
                )
            if path == "/api/styles/delete":
                return controller.delete_style(_integer(payload, "index"))
            if path == "/api/styles/clear":
                return controller.clear_styles()
            if path == "/api/backups":
                backup = controller.create_backup()
                return "备份已创建：%s" % backup["name"]
            if path == "/api/backups/restore":
                return controller.restore_backup(_string(payload, "name"))
            if path == "/api/backups/verify":
                return controller.verify_backup(_string(payload, "name"))
            if path == "/api/backups/prune":
                return controller.prune_backups()
            raise ValueError("不支持的面板操作")

        def _read_json(self) -> Dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("请求长度不合法") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError("请求内容过大")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("请求不是合法 JSON") from exc
            if not isinstance(value, dict):
                raise ValueError("请求必须是 JSON 对象")
            return value

        def _serve_file(self, path: Path, inject_token: bool = False) -> None:
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                self._json_error(HTTPStatus.NOT_FOUND, "资源不存在")
                return
            if inject_token:
                content = content.replace(b"__DASHBOARD_TOKEN__", dashboard_token.encode())
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", "%s; charset=utf-8" % content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _json_response(self, payload: Dict[str, object], status=HTTPStatus.OK) -> None:
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _json_error(self, status: HTTPStatus, message: str) -> None:
            self._json_response({"ok": False, "error": message}, status=status)

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")

        def _host_allowed(self) -> bool:
            host = self.headers.get("Host", "").split(":", 1)[0].strip("[]")
            return host in ("127.0.0.1", "localhost", "::1")

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlparse(origin)
            return parsed.hostname in ("127.0.0.1", "localhost", "::1")

        def log_message(self, format_string: str, *args) -> None:
            return

    return ThreadingHTTPServer((host, port), DashboardHandler)


def _string(payload: Dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少参数：%s" % name)
    return value.strip()


def _exact_string(payload: Dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError("缺少参数：%s" % name)
    return value


def _integer(payload: Dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool):
        raise ValueError("参数 %s 必须是整数" % name)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("参数 %s 必须是整数" % name) from exc
    if result < 1:
        raise ValueError("参数 %s 必须大于 0" % name)
    return result


def _exact_keys(payload: Dict[str, object], required, optional=()) -> None:
    expected = set(required)
    optional_keys = set(optional)
    if set(payload) - expected - optional_keys or expected - set(payload):
        raise ValueError("请求字段不合法")


def _strict_positive_integer(payload: Dict[str, object], name: str) -> int:
    value = payload.get(name)
    if type(value) is not int or value < 1:
        raise ValueError("参数 %s 必须是正整数" % name)
    return value


def _routing_config(payload: Dict[str, object]) -> Dict[str, object]:
    _exact_keys(payload, _ROUTING_CONFIG_KEYS)
    return payload


def _routing_enum(payload: Dict[str, object], name: str, allowed) -> str:
    value = payload.get(name)
    if type(value) is not str or value not in allowed:
        raise ValueError("参数 %s 不合法" % name)
    return value


def _routing_timestamp(payload: Dict[str, object]) -> str:
    value = payload.get("expected_updated_at")
    if type(value) is not str or not value or len(value) > 64:
        raise ValueError("参数 expected_updated_at 不合法")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("参数 expected_updated_at 不合法") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("参数 expected_updated_at 不合法")
    return value


def _routing_note(payload: Dict[str, object]) -> str:
    value = payload.get("note", "")
    if (
        type(value) is not str
        or len(value) > 300
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError("参数 note 不合法")
    return value
