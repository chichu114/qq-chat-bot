import math
import time
from typing import Any, Dict, List, Mapping, Optional

from .domain.inbound import (
    AttachmentRef,
    GroupInboundEnvelope,
    InboundEnvelope,
    InboundSegment,
    QuoteRef,
)


_ATTACHMENT_KINDS = {
    "image": "image",
    "record": "audio",
    "file": "file",
}
_MAX_QQ_USER_ID = 10**20 - 1
_MAX_GROUP_TEXT_LENGTH = 4096
_MAX_ECHO_LENGTH = 256
_CONTROL_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


def _as_int(value: object) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_qq_user_id(value: object) -> Optional[int]:
    if type(value) is int:
        return value if 0 < value <= _MAX_QQ_USER_ID else None
    if (
        type(value) is not str
        or len(value) > 20
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    try:
        user_id = int(value)
    except (ValueError, OverflowError):
        return None
    return user_id if 0 < user_id <= _MAX_QQ_USER_ID else None


def _local_finite_time() -> float:
    current = time.time()
    return current if math.isfinite(current) else 0.0


def _as_finite_float(value: object) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return _local_finite_time()
    return timestamp if math.isfinite(timestamp) else _local_finite_time()


def _as_string(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _attachment_from_segment(
    segment_type: str, data: Mapping[str, object]
) -> Optional[AttachmentRef]:
    file_id = _as_string(data.get("file_id")) or _as_string(data.get("file")) or ""
    url = _as_string(data.get("url"))
    if not file_id and not url:
        return None

    name = _as_string(data.get("name")) or ""
    declared_size = _as_int(data.get("file_size"))
    declared_mime = _as_string(data.get("mime")) or _as_string(data.get("mime_type"))
    return AttachmentRef(
        kind=_ATTACHMENT_KINDS[segment_type],
        file_id=file_id,
        url=url,
        name=name,
        declared_size=declared_size,
        declared_mime=declared_mime,
    )


def parse_inbound_message(event: Mapping[str, object]) -> Optional[InboundEnvelope]:
    user_id = parse_qq_user_id(event.get("user_id"))
    if user_id is None:
        return None

    message = event.get("message")
    text_parts: List[str] = []
    attachments: List[AttachmentRef] = []
    quote: Optional[QuoteRef] = None
    segments: List[InboundSegment] = []

    if isinstance(message, str):
        text = message.strip()
        if text:
            text_parts.append(text)
            segments.append(InboundSegment("text", 0))
    elif isinstance(message, list):
        for segment in message:
            if not isinstance(segment, Mapping):
                continue
            segment_type = segment.get("type")
            if not isinstance(segment_type, str) or not segment_type:
                continue

            if segment_type not in {"text", "reply", *_ATTACHMENT_KINDS}:
                attachments.append(AttachmentRef(kind="unsupported", name=segment_type))
                segments.append(InboundSegment("attachment", len(attachments) - 1))
                continue

            data = segment.get("data")
            if not isinstance(data, Mapping):
                continue

            if segment_type == "text":
                text = data.get("text")
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())
                    segments.append(InboundSegment("text", len(text_parts) - 1))
            elif segment_type == "reply":
                quote_id = _as_string(data.get("id")) or _as_string(data.get("message_id"))
                if quote_id:
                    if quote is not None:
                        return None
                    quote = QuoteRef(
                        message_id=quote_id,
                        embedded_text=_as_string(data.get("text")) or "",
                    )
                    segments.append(InboundSegment("quote", 0))
            elif segment_type in _ATTACHMENT_KINDS:
                attachment = _attachment_from_segment(segment_type, data)
                if attachment is not None:
                    attachments.append(attachment)
                    segments.append(InboundSegment("attachment", len(attachments) - 1))
    else:
        return None

    if not text_parts and not attachments and quote is None:
        return None

    return InboundEnvelope(
        user_id=user_id,
        message_id=_as_string(event.get("message_id")) or "",
        timestamp=_as_finite_float(event.get("time")),
        text_parts=tuple(text_parts),
        attachments=tuple(attachments),
        quote=quote,
        segments=tuple(segments),
    )


def _strict_event_timestamp(value: object) -> Optional[float]:
    if type(value) not in (int, float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def parse_group_message(
    event: Mapping[str, object], bot_qq: int
) -> Optional[GroupInboundEnvelope]:
    control_type = None
    try:
        result = _parse_group_message(event, bot_qq)
    except BaseException as exc:
        if type(exc) in _CONTROL_EXCEPTIONS:
            control_type = type(exc)
        else:
            return None
    if control_type is not None:
        raise control_type().with_traceback(None) from None
    return result


def _parse_group_message(
    event: Mapping[str, object], bot_qq: int
) -> Optional[GroupInboundEnvelope]:
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return None

    parsed_bot = bot_qq if type(bot_qq) is int else None
    if parsed_bot is not None and not 0 < parsed_bot <= _MAX_QQ_USER_ID:
        parsed_bot = None
    group_id = parse_qq_user_id(event.get("group_id"))
    user_id = parse_qq_user_id(event.get("user_id"))
    message_id = parse_qq_user_id(event.get("message_id"))
    timestamp = _strict_event_timestamp(event.get("time"))
    if None in (parsed_bot, group_id, user_id, message_id, timestamp):
        return None

    message = event.get("message")
    if type(message) is not list:
        return None

    text_parts: List[str] = []
    quote: Optional[QuoteRef] = None
    mentioned_bot = False
    for segment in message:
        if not isinstance(segment, Mapping):
            return None
        segment_type = segment.get("type")
        if type(segment_type) is not str or segment_type not in {"text", "reply", "at"}:
            return None
        data = segment.get("data")
        if not isinstance(data, Mapping):
            return None

        if segment_type == "text":
            value = data.get("text")
            if type(value) is not str:
                return None
            if value.strip():
                text_parts.append(value.strip())
        elif segment_type == "at":
            at_qq = parse_qq_user_id(data.get("qq"))
            if at_qq is None:
                return None
            if at_qq == parsed_bot:
                mentioned_bot = True
        else:
            if quote is not None:
                return None
            has_id = "id" in data
            has_message_id = "message_id" in data
            if has_id == has_message_id:
                return None
            reply_id = parse_qq_user_id(
                data["id"] if has_id else data["message_id"]
            )
            if reply_id is None:
                return None
            quote = QuoteRef(message_id=str(reply_id))

    normalized_text = " ".join(text_parts).strip()
    if not normalized_text or len(normalized_text) > _MAX_GROUP_TEXT_LENGTH:
        return None

    try:
        return GroupInboundEnvelope(
            group_id=group_id,
            user_id=user_id,
            message_id=str(message_id),
            timestamp=timestamp,
            text=normalized_text,
            mentioned_bot=mentioned_bot,
            quote=quote,
        )
    except ValueError:
        return None


def extract_text(message: Any) -> Optional[str]:
    if isinstance(message, str):
        text = message.strip()
        return text or None

    if not isinstance(message, list):
        return None

    parts: List[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") != "text":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        text = data.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())

    if not parts:
        return None
    return " ".join(parts).strip() or None


def build_send_private_msg(
    user_id: int, message: str, echo: Optional[str] = None
) -> Dict[str, object]:
    action: Dict[str, object] = {
        "action": "send_private_msg",
        "params": {"user_id": user_id, "message": message},
    }
    if echo is not None:
        action["echo"] = echo
    return action


def build_send_group_msg(
    group_id: int, message: str, echo: Optional[str] = None
) -> Dict[str, object]:
    if type(group_id) is not int or not 0 < group_id <= _MAX_QQ_USER_ID:
        raise ValueError("group_id is invalid")
    if (
        type(message) is not str
        or not message.strip()
        or len(message) > _MAX_GROUP_TEXT_LENGTH
    ):
        raise ValueError("message is invalid")
    if echo is not None and (
        type(echo) is not str or len(echo) > _MAX_ECHO_LENGTH
    ):
        raise ValueError("echo is invalid")

    action: Dict[str, object] = {
        "action": "send_group_msg",
        "params": {"group_id": group_id, "message": message},
    }
    if echo is not None:
        action["echo"] = echo
    return action
