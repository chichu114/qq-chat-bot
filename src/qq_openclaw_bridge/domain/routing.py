"""Pure, deterministic routing contracts and safety rules."""

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..models import AgentRequest


_MAX_NORMALIZED_TEXT = 4096
_LEVELS = ("L0", "L1", "L2", "L3")
_MODEL_LEVELS = frozenset(_LEVELS[1:])
TASK_KINDS = (
    "smalltalk",
    "question",
    "analysis",
    "creative",
    "coding",
    "planning",
    "summarization",
    "translation",
    "task",
    "external_action",
    "brainstorm",
    "other",
)

_RISK_PHRASES = {
    "deletion": (
        "删除",
        "删掉",
        "移除",
        "清空",
        "销毁",
        "delete",
        "remove",
    ),
    "external_send": (
        "发给客户",
        "发给",
        "发送",
        "转发",
        "私信",
        "send",
        "email",
        "邮件",
    ),
    "publishing": (
        "发布",
        "公开",
        "上线",
        "推送",
        "publish",
        "post",
    ),
    "finance": (
        "付款",
        "支付",
        "转账",
        "提现",
        "退款",
        "下单",
        "买入",
        "卖出",
        "payment",
        "transfer",
    ),
    "credentials": (
        "api key",
        "apikey",
        "access key",
        "密钥",
        "密码",
        "token",
        "凭证",
        "secret",
        "credential",
    ),
    "system_control": (
        "关机",
        "重启",
        "杀进程",
        "执行命令",
        "运行脚本",
        "sudo",
        "shell",
        "shutdown",
        "restart",
    ),
}

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s]+")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(?P<label>\b(?:api[_ -]?key|access[_ -]?key|token|password|passwd|secret|credential)\b"
    r"|密码|密钥|凭证)"
    r"(?:\s*[:=]\s*|\s+)[^\s]+"
)
_QUERY_VALUE_RE = re.compile(r"([?&][^=\s&#]+)=([^&#\s]*)")
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{12,}(?!\d)")


@dataclass(frozen=True)
class RoutingContract:
    user_id: int
    source_message_id: str
    text: str
    local_handled: bool
    config_version: int

    @classmethod
    def from_request(
        cls, request: AgentRequest, local_handled: bool, config_version: int
    ) -> "RoutingContract":
        return cls(
            user_id=request.user_id,
            source_message_id=request.message_id,
            text=request.text,
            local_handled=local_handled,
            config_version=config_version,
        )


@dataclass(frozen=True)
class RuleDecision:
    level: Optional[str]
    needs_classifier: bool
    forced: bool = False
    risk_flags: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ClassifierDecision:
    level: str
    confidence: float
    task_kind: str
    reasons: Tuple[str, ...]
    brainstorm_required: bool


@dataclass(frozen=True)
class RouteResolution:
    final_level: str
    confidence: float
    task_kind: str
    reasons: Tuple[str, ...]
    brainstorm_required: bool
    brainstorm_mode: str


def _normalized_text(text: str) -> Tuple[str, bool]:
    if not isinstance(text, str):
        raise ValueError("routing text must be a string")
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = " ".join(normalized.split())
    return normalized[:_MAX_NORMALIZED_TEXT], len(normalized) > _MAX_NORMALIZED_TEXT


class DeterministicRiskRouter:
    """Apply only bounded phrase rules; all other text needs classification."""

    def evaluate(self, contract: RoutingContract) -> RuleDecision:
        text, input_truncated = _normalized_text(contract.text)
        flags = tuple(
            flag
            for flag, phrases in _RISK_PHRASES.items()
            if any(phrase in text for phrase in phrases)
        )
        if input_truncated:
            flags = ("input_truncated",) + flags
        if flags:
            return RuleDecision(
                level="L3",
                needs_classifier=False,
                forced=True,
                risk_flags=flags,
                reasons=flags,
            )
        if contract.local_handled:
            return RuleDecision(
                level="L0",
                needs_classifier=False,
                reasons=("local_handled",),
            )
        return RuleDecision(level=None, needs_classifier=True)


def _object_without_duplicate_keys(pairs: Any) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate classifier key")
        result[key] = value
    return result


def parse_classifier_response(text: str) -> ClassifierDecision:
    try:
        payload = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("invalid classifier JSON") from None

    if not isinstance(payload, dict):
        raise ValueError("classifier response must be a JSON object")

    expected_keys = {
        "level",
        "confidence",
        "task_kind",
        "reasons",
        "brainstorm_required",
    }
    if set(payload) != expected_keys:
        raise ValueError("classifier response has unknown or missing keys")

    level = payload["level"]
    if not isinstance(level, str) or level not in _MODEL_LEVELS:
        raise ValueError("classifier level must be L1, L2, or L3")

    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("classifier confidence must be numeric")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("classifier confidence must be finite and bounded")

    task_kind = payload["task_kind"]
    if not isinstance(task_kind, str) or task_kind not in TASK_KINDS:
        raise ValueError("classifier task kind is not allowed")

    reasons = payload["reasons"]
    if not isinstance(reasons, list) or len(reasons) > 8:
        raise ValueError("classifier reasons must contain at most eight items")
    if any(not isinstance(reason, str) or len(reason) > 64 for reason in reasons):
        raise ValueError("classifier reasons must be strings of at most 64 characters")

    brainstorm_required = payload["brainstorm_required"]
    if not isinstance(brainstorm_required, bool):
        raise ValueError("brainstorm_required must be boolean")

    return ClassifierDecision(
        level=level,
        confidence=float(confidence),
        task_kind=task_kind,
        reasons=tuple(reasons),
        brainstorm_required=brainstorm_required,
    )


def _append_unique(values: Tuple[str, ...], value: str) -> Tuple[str, ...]:
    return values if value in values else values + (value,)


def _brainstorm_mode(required: bool, level: str) -> str:
    if level == "L0":
        return "disabled"
    return "required" if required else "standard"


class ConservativeRouteResolver:
    def resolve(
        self,
        rule: RuleDecision,
        classifier: Optional[ClassifierDecision],
        threshold: float,
    ) -> RouteResolution:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("classifier threshold must be numeric")
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("classifier threshold must be finite and bounded")

        reasons = tuple(rule.reasons)
        if classifier is not None:
            for reason in classifier.reasons:
                reasons = _append_unique(reasons, reason)

        if rule.level == "L0" and not rule.forced:
            return RouteResolution(
                final_level="L0",
                confidence=1.0,
                task_kind="local_command",
                reasons=reasons,
                brainstorm_required=False,
                brainstorm_mode="disabled",
            )

        if rule.level == "L3" and rule.forced:
            if classifier is not None and classifier.level != "L3":
                reasons = _append_unique(reasons, "model_downgrade_blocked")
            return RouteResolution(
                final_level="L3",
                confidence=1.0,
                task_kind=classifier.task_kind if classifier else "forced_risk",
                reasons=reasons,
                brainstorm_required=classifier.brainstorm_required if classifier else False,
                brainstorm_mode=_brainstorm_mode(
                    classifier.brainstorm_required if classifier else False, "L3"
                ),
            )

        if classifier is None:
            reasons = _append_unique(reasons, "classifier_missing_fallback")
            return RouteResolution(
                final_level="L2",
                confidence=0.0,
                task_kind="other",
                reasons=reasons,
                brainstorm_required=False,
                brainstorm_mode="standard",
            )

        final_level = classifier.level
        if classifier.confidence < threshold:
            final_level = "L2"
            reasons = _append_unique(reasons, "low_confidence_fallback")

        return RouteResolution(
            final_level=final_level,
            confidence=classifier.confidence,
            task_kind=classifier.task_kind,
            reasons=reasons,
            brainstorm_required=classifier.brainstorm_required,
            brainstorm_mode=_brainstorm_mode(classifier.brainstorm_required, final_level),
        )


def redacted_preview(text: str, limit: int = 160) -> str:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("preview limit must be a non-negative integer")
    preview, _ = _normalized_text(text)
    preview = _BEARER_RE.sub("Bearer [REDACTED]", preview)
    preview = _SENSITIVE_FIELD_RE.sub(
        lambda match: match.group("label") + "=[REDACTED]",
        preview,
    )
    preview = _QUERY_VALUE_RE.sub(r"\1=[REDACTED]", preview)
    preview = _LONG_NUMBER_RE.sub("[REDACTED]", preview)
    return preview[:limit]
