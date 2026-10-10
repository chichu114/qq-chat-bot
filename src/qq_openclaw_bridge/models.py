from dataclasses import dataclass
import math
from typing import Optional

from .domain.inbound import GroupInboundEnvelope, InboundEnvelope


_MAX_QQ_ID = 10**20 - 1
_MAX_GROUP_TEXT_LENGTH = 4096


def _valid_contract_id(value: object) -> bool:
    return (
        type(value) is int
        and 0 < value <= _MAX_QQ_ID
    )


def _valid_message_id(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 20
        and value.isascii()
        and value.isdecimal()
        and 0 < int(value) <= _MAX_QQ_ID
    )


@dataclass(frozen=True)
class AgentRequest:
    user_id: int
    message_id: str
    text: str
    timestamp: float
    context: str = ""
    session_key: Optional[str] = None
    media_context: str = ""
    task_id: Optional[str] = None


@dataclass(frozen=True)
class AgentResponse:
    text: str
    provider: Optional[str] = None
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    duration_ms: Optional[int] = None


@dataclass(frozen=True)
class GroupMessageRequest:
    group_id: int
    user_id: int
    message_id: str
    text: str
    timestamp: float
    quote_text: str = ""

    def __post_init__(self) -> None:
        if not _valid_contract_id(self.group_id):
            raise ValueError("group_id is invalid")
        if not _valid_contract_id(self.user_id):
            raise ValueError("user_id is invalid")
        if not _valid_message_id(self.message_id):
            raise ValueError("message_id is invalid")
        object.__setattr__(self, "message_id", str(int(self.message_id)))
        if type(self.text) is not str:
            raise ValueError("text is invalid")
        normalized = self.text.strip()
        if not normalized or len(normalized) > _MAX_GROUP_TEXT_LENGTH:
            raise ValueError("text is invalid")
        object.__setattr__(self, "text", normalized)
        if (
            type(self.timestamp) not in (int, float)
            or not math.isfinite(float(self.timestamp))
        ):
            raise ValueError("timestamp is invalid")
        object.__setattr__(self, "timestamp", float(self.timestamp))
        if (
            type(self.quote_text) is not str
            or len(self.quote_text) > _MAX_GROUP_TEXT_LENGTH
        ):
            raise ValueError("quote_text is invalid")


@dataclass(frozen=True)
class MessageDecision:
    accepted: bool
    reason: str
    request: Optional[AgentRequest] = None
    envelope: Optional[InboundEnvelope] = None


@dataclass(frozen=True)
class GroupMessageDecision:
    accepted: bool
    reason: str
    request: Optional[GroupMessageRequest] = None
    envelope: Optional[GroupInboundEnvelope] = None
