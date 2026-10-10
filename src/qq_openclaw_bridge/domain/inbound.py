from dataclasses import dataclass
import math
from typing import Optional, Tuple


_MAX_QQ_ID = 10**20 - 1
_MAX_GROUP_TEXT_LENGTH = 4096


def _require_contract_id(value: object, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} is invalid")
    if value < 1 or value > _MAX_QQ_ID:
        raise ValueError(f"{field} is invalid")
    return value


def _require_message_id(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 20
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise ValueError("message_id is invalid")
    parsed = int(value)
    if parsed < 1 or parsed > _MAX_QQ_ID:
        raise ValueError("message_id is invalid")
    return str(parsed)


def _require_group_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_GROUP_TEXT_LENGTH:
        raise ValueError(f"{field} is invalid")
    return normalized


@dataclass(frozen=True)
class AttachmentRef:
    kind: str
    file_id: str = ""
    url: Optional[str] = None
    name: str = ""
    declared_size: Optional[int] = None
    declared_mime: Optional[str] = None


@dataclass(frozen=True)
class QuoteRef:
    message_id: str
    embedded_text: str = ""


@dataclass(frozen=True)
class InboundSegment:
    kind: str
    index: int


@dataclass(frozen=True)
class InboundEnvelope:
    user_id: int
    message_id: str
    timestamp: float
    text_parts: Tuple[str, ...] = ()
    attachments: Tuple[AttachmentRef, ...] = ()
    quote: Optional[QuoteRef] = None
    segments: Tuple[InboundSegment, ...] = ()

    @property
    def text(self) -> str:
        return " ".join(part.strip() for part in self.text_parts if part.strip()).strip()


@dataclass(frozen=True)
class GroupInboundEnvelope:
    group_id: int
    user_id: int
    message_id: str
    timestamp: float
    text: str
    mentioned_bot: bool
    quote: Optional[QuoteRef] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", _require_contract_id(self.group_id, "group_id"))
        object.__setattr__(self, "user_id", _require_contract_id(self.user_id, "user_id"))
        object.__setattr__(self, "message_id", _require_message_id(self.message_id))
        if (
            type(self.timestamp) not in (int, float)
            or not math.isfinite(float(self.timestamp))
        ):
            raise ValueError("timestamp is invalid")
        object.__setattr__(self, "timestamp", float(self.timestamp))
        object.__setattr__(self, "text", _require_group_text(self.text, "text"))
        if type(self.mentioned_bot) is not bool:
            raise ValueError("mentioned_bot is invalid")
        if self.quote is not None:
            if type(self.quote) is not QuoteRef:
                raise ValueError("quote is invalid")
            message_id = _require_message_id(self.quote.message_id)
            embedded_text = self.quote.embedded_text
            if (
                type(embedded_text) is not str
                or len(embedded_text) > _MAX_GROUP_TEXT_LENGTH
            ):
                raise ValueError("quote is invalid")
            object.__setattr__(
                self,
                "quote",
                QuoteRef(message_id=message_id, embedded_text=embedded_text),
            )
