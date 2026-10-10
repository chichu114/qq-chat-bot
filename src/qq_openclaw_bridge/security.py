import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from .domain.actions import ActionIntent, canonical_payload, payload_sha256


CANCEL_COMMANDS = ("取消操作", "放弃操作")
CONFIRM_PATTERN = re.compile(r"^(?:确认执行|确定执行)(?:\s+([A-Z0-9]{6}))?$")


@dataclass(frozen=True)
class ActionExecutionToken:
    intent_id: str
    user_id: int
    payload_sha256: str
    config_version: int


@dataclass(frozen=True)
class SecurityDecision:
    intercepted: bool = False
    reply: Optional[str] = None
    command: Optional[str] = None
    approval_id: Optional[str] = None
    intent_id: Optional[str] = None
    execution_token: Optional[ActionExecutionToken] = None


@dataclass(frozen=True)
class PendingAction:
    approval_id: str
    command: str
    description: str
    expires_at: datetime
    intent_id: Optional[str] = None
    payload_sha256: Optional[str] = None
    config_version: Optional[int] = None
    action_type: Optional[str] = None
    adapter_id: Optional[str] = None
    payload_json: Optional[str] = None


@dataclass(frozen=True)
class LocalAction:
    action_type: str
    adapter_id: str
    payload: Mapping[str, object]
    command: str
    description: str


class ConfirmationGuard:
    def __init__(
        self,
        timeout_seconds: int = 120,
        now_provider: Optional[Callable[[], datetime]] = None,
        approval_store: Optional[object] = None,
        id_provider: Optional[Callable[[], str]] = None,
        config_version: int = 1,
        binding_key: Optional[bytes] = None,
    ) -> None:
        self.timeout_seconds = max(1, timeout_seconds)
        self._now_provider = now_provider or (
            lambda: datetime.now(tz=ZoneInfo("Asia/Shanghai"))
        )
        self._pending: Dict[int, PendingAction] = {}
        self.approval_store = approval_store
        self.action_repository = getattr(
            approval_store, "action_intent_repository", None
        )
        self._binding_key = (
            _approval_binding_key(binding_key)
            if self.action_repository is not None
            else None
        )
        self._id_provider = id_provider or (lambda: secrets.token_hex(3).upper())
        if type(config_version) is not int or config_version <= 0:
            raise ValueError("config_version must be positive")
        self.config_version = config_version

    def evaluate(
        self,
        user_id: int,
        text: str,
        message_id: Optional[object] = None,
        session_key: Optional[str] = None,
    ) -> SecurityDecision:
        normalized = " ".join(text.strip().split())
        now = self._now_provider()
        pending = self._get_pending(user_id)
        confirm_match = CONFIRM_PATTERN.fullmatch(normalized)

        if pending is not None and now > pending.expires_at:
            self._discard_pending(pending)
            if confirm_match is not None or normalized in CANCEL_COMMANDS:
                return SecurityDecision(
                    intercepted=True,
                    reply="上一条待确认操作已过期。真要做就重新发，别拿过期单据来盖章。",
                )
            pending = None

        if confirm_match is not None:
            return self._confirm(user_id, pending, confirm_match, now)

        if normalized in CANCEL_COMMANDS:
            if pending is None:
                return SecurityDecision(
                    intercepted=True,
                    reply="现在没有待确认操作，取消了个寂寞。",
                )
            self._discard_pending(pending)
            return SecurityDecision(
                intercepted=True,
                reply="已取消：%s。还算清醒，没让手速替脑子做决定。"
                % pending.description,
            )

        local_action = _parse_local_action(normalized)
        risk = local_action.description if local_action is not None else _classify_risk(normalized)
        if risk is None:
            return SecurityDecision()

        if local_action is None and self.action_repository is not None:
            if pending is not None:
                self._discard_pending(pending)
            return SecurityDecision(
                intercepted=True,
                reply="该高风险操作无授权适配器，已拒绝执行。",
            )

        replaced = pending is not None
        if pending is not None:
            self._discard_pending(pending)
        approval_id = self._id_provider()
        expires_at = now + timedelta(seconds=self.timeout_seconds)
        intent = None
        if local_action is not None and self.action_repository is not None:
            local_hash = payload_sha256(local_action.payload)
            if self.action_repository.find_executing(
                user_id, local_action.action_type, local_hash
            ) is not None:
                return SecurityDecision(
                    intercepted=True,
                    reply="同一操作上一笔仍是结果待核验状态。先检查实际结果，暂勿重复发起。",
                )
            intent = ActionIntent.create(
                approval_id=approval_id,
                user_id=user_id,
                source_message_id=(
                    str(message_id)
                    if message_id not in (None, "")
                    else "approval:%s" % approval_id
                ),
                session_key=(
                    session_key
                    if isinstance(session_key, str) and session_key.strip()
                    else "agent:main:qq-%s" % user_id
                ),
                action_type=local_action.action_type,
                adapter_id=local_action.adapter_id,
                payload=local_action.payload,
                config_version=self.config_version,
                risk_tier="high",
                impact_summary=risk,
                created_at=now,
                expires_at=expires_at,
            )
            self.action_repository.propose(intent)

        binding = (
            None
            if intent is None
            else _intent_binding(intent, self._binding_key)
        )
        new_pending = PendingAction(
            approval_id=approval_id,
            command=(
                _encode_intent_reference(binding)
                if intent is not None
                else normalized
            ),
            description=risk,
            expires_at=expires_at,
            intent_id=None if intent is None else intent.intent_id,
            payload_sha256=None if intent is None else intent.payload_sha256,
            config_version=None if intent is None else intent.config_version,
            action_type=None if intent is None else intent.action_type,
            adapter_id=None if intent is None else intent.adapter_id,
            payload_json=None if intent is None else intent.payload_json,
        )
        try:
            self._save_pending(user_id, new_pending, now)
        except Exception:
            if intent is not None:
                self._cancel_intent(new_pending)
            raise
        prefix = "已替换上一条待确认操作。\n" if replaced else ""
        return SecurityDecision(
            intercepted=True,
            approval_id=approval_id,
            intent_id=None if intent is None else intent.intent_id,
            reply=(
                "%s风险操作待确认：%s。\n"
                "审批码：%s。%d 秒内回复“确认执行 %s”继续，回复“取消操作”放弃。"
                % (
                    prefix,
                    risk,
                    approval_id,
                    self.timeout_seconds,
                    approval_id,
                )
            ),
        )

    def _confirm(self, user_id, pending, match, now) -> SecurityDecision:
        if pending is None:
            return SecurityDecision(
                intercepted=True,
                reply="现在没有待确认操作。别空按确认键，我这儿不是抽奖机。",
            )
        supplied_id = match.group(1)
        if supplied_id is None:
            return SecurityDecision(
                intercepted=True,
                reply="请带上审批码，格式是“确认执行 %s”。少一截都不算数。"
                % pending.approval_id,
            )
        if supplied_id != pending.approval_id:
            return SecurityDecision(
                intercepted=True,
                reply="审批码不匹配。当前待确认编号是 %s，别拿错单据。"
                % pending.approval_id,
            )
        if self.action_repository is None:
            self._delete_approval(pending)
            return SecurityDecision(
                intercepted=True,
                approval_id=pending.approval_id,
                command=pending.command,
            )

        try:
            row = self.action_repository.get(pending.intent_id)
            command = _command_from_intent(
                row, user_id, pending.approval_id, pending
            )
            consumed = self.action_repository.approve_and_consume(
                pending.intent_id,
                pending.approval_id,
                user_id,
                str(pending.action_type),
                str(pending.adapter_id),
                str(pending.payload_json),
                str(pending.payload_sha256),
                int(pending.config_version),
                pending.expires_at,
                now=now,
            )
        except Exception:
            self._discard_pending(pending)
            return SecurityDecision(
                intercepted=True,
                reply="审批内容校验失败，操作没有执行。请重新发起，别重复确认旧单。",
            )

        self._delete_approval(pending)
        token = ActionExecutionToken(
            intent_id=str(consumed["intent_id"]),
            user_id=int(consumed["user_id"]),
            payload_sha256=str(consumed["payload_sha256"]),
            config_version=int(consumed["config_version"]),
        )
        return SecurityDecision(
            intercepted=True,
            approval_id=pending.approval_id,
            intent_id=pending.intent_id,
            command=command,
            execution_token=token,
        )

    def _get_pending(self, user_id: int) -> Optional[PendingAction]:
        if self.approval_store is None:
            return self._pending.get(user_id)
        record = self.approval_store.get_pending_approval(user_id)
        if record is None:
            return None
        approval_id = str(record["approval_id"])
        command = str(record["command"])
        invalid_reference = False
        try:
            binding = _decode_intent_reference(command, self._binding_key)
            if binding is None and self.action_repository is not None:
                raise ValueError("unsigned persistent approval")
            if binding is not None:
                record_expiry = datetime.fromisoformat(str(record["expires_at"]))
                signed_expiry = datetime.fromisoformat(str(binding["expires_at"]))
                if (
                    binding["approval_id"] != approval_id
                    or binding["user_id"] != int(record["user_id"])
                    or signed_expiry != record_expiry
                ):
                    raise ValueError("approval binding metadata changed")
        except ValueError:
            binding = None
            invalid_reference = True
        referenced_intent_id = (
            "invalid-reference"
            if invalid_reference
            else (None if binding is None else str(binding["intent_id"]))
        )
        return PendingAction(
            approval_id=approval_id,
            command=command,
            description=str(record["description"]),
            expires_at=datetime.fromisoformat(str(record["expires_at"])),
            intent_id=referenced_intent_id,
            config_version=(
                None if binding is None else binding.get("config_version")
            ),
            action_type=(
                None if binding is None else binding.get("action_type")
            ),
            adapter_id=(
                None if binding is None else binding.get("adapter_id")
            ),
            payload_json=(
                None if binding is None else binding.get("payload_json")
            ),
            payload_sha256=(
                None if binding is None else binding.get("payload_sha256")
            ),
        )

    def _save_pending(
        self, user_id: int, pending: PendingAction, created_at: datetime
    ) -> None:
        if self.approval_store is None:
            self._pending[user_id] = pending
            return
        self.approval_store.save_approval(
            approval_id=pending.approval_id,
            user_id=user_id,
            command=pending.command,
            description=pending.description,
            expires_at=pending.expires_at.isoformat(timespec="seconds"),
            created_at=created_at.isoformat(timespec="seconds"),
        )

    def _discard_pending(self, pending: PendingAction) -> None:
        self._cancel_intent(pending)
        self._delete_approval(pending)

    def _cancel_intent(self, pending: PendingAction) -> None:
        if (
            self.action_repository is None
            or pending.intent_id is None
            or pending.payload_sha256 is None
            or pending.config_version is None
        ):
            return
        try:
            self.action_repository.cancel(
                pending.intent_id,
                int(self.action_repository.get(pending.intent_id)["user_id"]),
                pending.payload_sha256,
                pending.config_version,
                now=self._now_provider(),
            )
        except Exception:
            pass

    def _delete_approval(self, pending: PendingAction) -> None:
        if self.approval_store is None:
            for user_id, current in list(self._pending.items()):
                if current.approval_id == pending.approval_id:
                    self._pending.pop(user_id, None)
            return
        self.approval_store.delete_approval(pending.approval_id)


def _parse_local_action(text: str) -> Optional[LocalAction]:
    indexed = (
        (r"^(?:删除|删掉|移除)记忆\s*#?(\d+)$", "memory.delete", "local.housekeeper", "删除记忆 %d", "删除第 %d 条长期记忆"),
        (r"^(?:删除|删掉|移除)待办\s*#?(\d+)$", "todo.delete", "local.housekeeper", "删除待办 %d", "删除待办 #%d"),
        (r"^删除风格\s*(\d+)$", "style.delete", "local.style", "删除风格 %d", "删除第 %d 条风格反馈"),
    )
    for pattern, action_type, adapter_id, command, description in indexed:
        match = re.fullmatch(pattern, text)
        if match:
            index = int(match.group(1))
            return LocalAction(action_type, adapter_id, {"index": index}, command % index, description % index)
    fixed = {
        "清空记忆": LocalAction("memory.clear", "local.housekeeper", {}, "清空记忆", "清空全部长期记忆"),
        "清除记忆": LocalAction("memory.clear", "local.housekeeper", {}, "清空记忆", "清空全部长期记忆"),
        "清空长期记忆": LocalAction("memory.clear", "local.housekeeper", {}, "清空记忆", "清空全部长期记忆"),
        "清空已完成": LocalAction("todo.completed.clear", "local.housekeeper", {}, "清空已完成", "清空所有已完成待办"),
        "清理已完成": LocalAction("todo.completed.clear", "local.housekeeper", {}, "清空已完成", "清空所有已完成待办"),
        "删除已完成": LocalAction("todo.completed.clear", "local.housekeeper", {}, "清空已完成", "清空所有已完成待办"),
        "清空风格反馈": LocalAction("style.clear", "local.style", {}, "清空风格反馈", "清空全部风格反馈"),
        "清空风格沉淀": LocalAction("style.clear", "local.style", {}, "清空风格反馈", "清空全部风格反馈"),
    }
    return fixed.get(text)


def _approval_binding_key(supplied: Optional[bytes]) -> bytes:
    if type(supplied) is not bytes or len(supplied) < 32:
        raise ValueError("persistent approval binding_key must contain at least 32 bytes")
    return supplied


def _binding_signature(binding: Mapping[str, object], key: bytes) -> str:
    encoded = json.dumps(
        dict(binding),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _intent_binding(intent: ActionIntent, key: bytes) -> Dict[str, object]:
    binding = {
        "intent_id": intent.intent_id,
        "approval_id": intent.approval_id,
        "user_id": intent.user_id,
        "action_type": intent.action_type,
        "adapter_id": intent.adapter_id,
        "payload_json": intent.payload_json,
        "payload_sha256": intent.payload_sha256,
        "config_version": intent.config_version,
        "expires_at": intent.expires_at.isoformat(timespec="seconds"),
    }
    binding["signature"] = _binding_signature(binding, key)
    return binding


def _encode_intent_reference(binding: Mapping[str, object]) -> str:
    return "action-intent:" + json.dumps(
        dict(binding),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_intent_reference(
    command: str, key: Optional[bytes]
) -> Optional[Dict[str, object]]:
    prefix = "action-intent:"
    if not command.startswith(prefix):
        return None
    try:
        binding = json.loads(command[len(prefix) :])
    except (TypeError, ValueError) as error:
        raise ValueError("invalid action intent reference") from error
    required = {
        "intent_id",
        "approval_id",
        "user_id",
        "action_type",
        "adapter_id",
        "payload_json",
        "payload_sha256",
        "config_version",
        "expires_at",
        "signature",
    }
    if type(binding) is not dict or set(binding) != required:
        raise ValueError("invalid action intent reference")
    for field in required - {"config_version", "user_id"}:
        if type(binding[field]) is not str or not binding[field]:
            raise ValueError("invalid action intent reference")
    if type(binding["config_version"]) is not int or binding["config_version"] <= 0:
        raise ValueError("invalid action intent reference")
    if type(binding["user_id"]) is not int or binding["user_id"] <= 0:
        raise ValueError("invalid action intent reference")
    if type(key) is not bytes or len(key) < 32:
        raise ValueError("action intent verification key unavailable")
    unsigned = dict(binding)
    signature = unsigned.pop("signature")
    if not hmac.compare_digest(signature, _binding_signature(unsigned, key)):
        raise ValueError("action intent reference signature mismatch")
    try:
        expiry = datetime.fromisoformat(binding["expires_at"])
        aware = expiry.tzinfo is not None and expiry.utcoffset() is not None
    except (TypeError, ValueError):
        aware = False
    if not aware:
        raise ValueError("invalid action intent expiry")
    return binding


def _command_from_intent(
    row, user_id: int, approval_id: str, pending: PendingAction
) -> str:
    if row is None or int(row["user_id"]) != user_id:
        raise ValueError("action intent owner mismatch")
    if str(row["approval_id"]) != approval_id or row["status"] != "proposed":
        raise ValueError("action intent approval mismatch")
    expected = {
        "action_type": pending.action_type,
        "adapter_id": pending.adapter_id,
        "payload_json": pending.payload_json,
        "payload_sha256": pending.payload_sha256,
        "config_version": pending.config_version,
    }
    if any(value is None for value in expected.values()):
        raise ValueError("action intent binding is incomplete")
    for field, value in expected.items():
        if row[field] != value:
            raise ValueError("action intent binding changed")
    try:
        row_expiry = datetime.fromisoformat(str(row["expires_at"]))
    except ValueError as error:
        raise ValueError("action intent expiry is invalid") from error
    if row_expiry != pending.expires_at:
        raise ValueError("action intent expiry changed")
    payload_json = str(pending.payload_json)
    payload = json.loads(payload_json)
    if canonical_payload(payload) != payload_json:
        raise ValueError("action intent payload is not canonical")
    if payload_sha256(payload) != pending.payload_sha256:
        raise ValueError("action intent payload hash mismatch")
    action_type = str(pending.action_type)
    adapter_id = str(pending.adapter_id)
    if action_type in {"memory.delete", "todo.delete", "style.delete"}:
        if type(payload) is not dict or set(payload) != {"index"}:
            raise ValueError("invalid indexed action payload")
        index = payload["index"]
        if type(index) is not int or index <= 0:
            raise ValueError("invalid action index")
        mapping = {
            "memory.delete": ("local.housekeeper", "删除记忆 %d"),
            "todo.delete": ("local.housekeeper", "删除待办 %d"),
            "style.delete": ("local.style", "删除风格 %d"),
        }
        expected_adapter, template = mapping[action_type]
        if adapter_id != expected_adapter:
            raise ValueError("action adapter mismatch")
        return template % index
    mapping = {
        "memory.clear": ("local.housekeeper", "清空记忆"),
        "todo.completed.clear": ("local.housekeeper", "清空已完成"),
        "style.clear": ("local.style", "清空风格反馈"),
    }
    if action_type not in mapping or type(payload) is not dict or payload:
        raise ValueError("unknown local action")
    expected_adapter, command = mapping[action_type]
    if adapter_id != expected_adapter:
        raise ValueError("action adapter mismatch")
    return command


def _classify_risk(text: str) -> Optional[str]:
    local = _parse_local_action(text)
    if local is not None:
        return local.description
    action = re.sub(r"^(?:请|帮我|替我|麻烦)\s*", "", text)
    sensitive_rules: Tuple[Tuple[str, str], ...] = (
        (r"^(?:给|向).+(?:发|发送|转发)(?:消息|邮件|文件)", "请求 OpenClaw 外发消息或文件"),
        (r"^(?:发送|转发).+(?:给|到).+", "请求 OpenClaw 外发消息或文件"),
        (r"^(?:删除|删掉|清空).*(?:文件|文件夹|目录)", "请求 OpenClaw 删除本地文件"),
        (r"^(?:执行|运行).*(?:命令|脚本)", "请求 OpenClaw 执行命令或脚本"),
        (r"^(?:修改|更改|重置).*(?:系统|网络|代理).*(?:配置|设置)", "请求 OpenClaw 修改系统配置"),
        (r"^(?:转账|付款|支付|购买)", "请求 OpenClaw 执行支付或购买"),
    )
    for pattern, description in sensitive_rules:
        if re.search(pattern, action):
            return description
    return None


__all__ = [
    "ActionExecutionToken",
    "ConfirmationGuard",
    "PendingAction",
    "SecurityDecision",
]
