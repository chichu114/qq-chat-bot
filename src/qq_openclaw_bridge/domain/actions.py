import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


MAX_TEXT_LENGTH = 512
SQLITE_MAX_INTEGER = 2**63 - 1
MAX_PAYLOAD_DEPTH = 32
MAX_PAYLOAD_NODES = 10000
MAX_PAYLOAD_BYTES = 65536


def _strict_utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ValueError("action payload strings must be valid UTF-8") from error


def _consume_bytes(state, amount: int) -> None:
    state["bytes"] += amount
    if state["bytes"] > MAX_PAYLOAD_BYTES:
        raise ValueError("action payload exceeds maximum byte size")


def _json_string_size(value: str) -> int:
    encoded = _strict_utf8(value)
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("action payload exceeds maximum byte size")
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(serialized.encode("utf-8", errors="strict"))


def _canonical_value(value, depth=0, state=None):
    if depth > MAX_PAYLOAD_DEPTH:
        raise ValueError("action payload exceeds maximum depth")
    if state is None:
        state = {"nodes": 0, "active": set(), "bytes": 0}
    state["nodes"] += 1
    if state["nodes"] > MAX_PAYLOAD_NODES:
        raise ValueError("action payload contains too many values")
    if value is None:
        _consume_bytes(state, 4)
        return None
    if type(value) is bool:
        _consume_bytes(state, 4 if value else 5)
        return value
    if type(value) is int:
        _consume_bytes(state, len(str(value)))
        return value
    if type(value) is str:
        _consume_bytes(state, _json_string_size(value))
        return value
    if type(value) is list:
        identity = id(value)
        if identity in state["active"]:
            raise ValueError("action payload contains a cyclic value")
        state["active"].add(identity)
        try:
            _consume_bytes(state, 2)
            result = []
            for index, item in enumerate(value):
                if index:
                    _consume_bytes(state, 1)
                result.append(_canonical_value(item, depth + 1, state))
            return result
        finally:
            state["active"].remove(identity)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in state["active"]:
            raise ValueError("action payload contains a cyclic value")
        state["active"].add(identity)
        try:
            _consume_bytes(state, 2)
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if type(key) is not str:
                    raise ValueError("action payload keys must be exact strings")
                if index:
                    _consume_bytes(state, 1)
                _consume_bytes(state, _json_string_size(key))
                _consume_bytes(state, 1)
                result[key] = _canonical_value(item, depth + 1, state)
            return result
        finally:
            state["active"].remove(identity)
    raise ValueError("action payload contains an unsupported value")


def canonical_payload(payload: Mapping[str, object]) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("action payload must be a mapping")
    normalized = _canonical_value(payload)
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        encoded = canonical.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ValueError("action payload must be valid UTF-8") from error
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("action payload exceeds maximum byte size")
    return canonical


def payload_sha256(payload: Mapping[str, object]) -> str:
    canonical = canonical_payload(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0 or value > SQLITE_MAX_INTEGER:
        raise ValueError("%s must be a positive SQLite integer" % field)
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("%s must be a non-empty exact string" % field)
    if len(value) > MAX_TEXT_LENGTH:
        raise ValueError("%s is too long" % field)
    return value


def _aware_datetime(value: object, field: str) -> datetime:
    try:
        aware = (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )
    except Exception as error:
        raise ValueError("%s must be timezone-aware" % field) from error
    if not aware:
        raise ValueError("%s must be timezone-aware" % field)
    return value


@dataclass(frozen=True)
class ActionIntent:
    intent_id: str
    approval_id: str
    user_id: int
    source_message_id: str
    session_key: str
    action_type: str
    adapter_id: str
    schema_version: int
    payload_json: str
    payload_sha256: str
    config_version: int
    risk_tier: str
    impact_summary: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _text(self.intent_id, "intent_id")
        _text(self.approval_id, "approval_id")
        _positive_integer(self.user_id, "user_id")
        _text(self.source_message_id, "source_message_id")
        _text(self.session_key, "session_key")
        _text(self.action_type, "action_type")
        _text(self.adapter_id, "adapter_id")
        _positive_integer(self.schema_version, "schema_version")
        _positive_integer(self.config_version, "config_version")
        _text(self.risk_tier, "risk_tier")
        _text(self.impact_summary, "impact_summary")
        created = _aware_datetime(self.created_at, "created_at")
        expires = _aware_datetime(self.expires_at, "expires_at")
        if expires <= created:
            raise ValueError("expires_at must be after created_at")
        if type(self.payload_json) is not str:
            raise ValueError("payload_json must be a canonical string")
        try:
            decoded = json.loads(self.payload_json)
        except (TypeError, ValueError) as error:
            raise ValueError("payload_json must be valid JSON") from error
        canonical = canonical_payload(decoded)
        if canonical != self.payload_json:
            raise ValueError("payload_json is not canonical")
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if type(self.payload_sha256) is not str or self.payload_sha256 != digest:
            raise ValueError("payload_sha256 does not match payload_json")

    @classmethod
    def create(
        cls,
        *,
        user_id: int,
        source_message_id: str,
        session_key: str,
        action_type: str,
        adapter_id: str,
        payload: Mapping[str, object],
        config_version: int,
        risk_tier: str,
        impact_summary: str,
        created_at: datetime,
        expires_at: datetime,
        schema_version: int = 1,
        intent_id: Optional[str] = None,
        approval_id: Optional[str] = None,
    ) -> "ActionIntent":
        created = _aware_datetime(created_at, "created_at")
        expires = _aware_datetime(expires_at, "expires_at")
        if expires <= created:
            raise ValueError("expires_at must be after created_at")
        canonical = canonical_payload(payload)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(
            intent_id=_text(
                uuid.uuid4().hex if intent_id is None else intent_id,
                "intent_id",
            ),
            approval_id=_text(
                uuid.uuid4().hex if approval_id is None else approval_id,
                "approval_id",
            ),
            user_id=_positive_integer(user_id, "user_id"),
            source_message_id=_text(
                source_message_id, "source_message_id"
            ),
            session_key=_text(session_key, "session_key"),
            action_type=_text(action_type, "action_type"),
            adapter_id=_text(adapter_id, "adapter_id"),
            schema_version=_positive_integer(
                schema_version, "schema_version"
            ),
            payload_json=canonical,
            payload_sha256=digest,
            config_version=_positive_integer(
                config_version, "config_version"
            ),
            risk_tier=_text(risk_tier, "risk_tier"),
            impact_summary=_text(impact_summary, "impact_summary"),
            created_at=created,
            expires_at=expires,
        )


__all__ = ["ActionIntent", "canonical_payload", "payload_sha256"]
