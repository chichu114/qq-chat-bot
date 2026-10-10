from typing import Callable, Mapping, Optional, Set

from .models import (
    AgentRequest,
    GroupMessageDecision,
    GroupMessageRequest,
    MessageDecision,
)
from .onebot import parse_group_message, parse_inbound_message, parse_qq_user_id
from .rate_limit import RateLimiter


_CONTROL_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)
_PAIR_RESULTS = frozenset(("allowed", "first_limited", "second_limited"))


def evaluate_event(
    event: Mapping[str, object],
    allowed_users: set,
    rate_limiter: RateLimiter,
    enable_media_pipeline: bool = False,
    enable_quote_resolution: bool = False,
    duplicate_checker: Optional[Callable[[str], bool]] = None,
) -> MessageDecision:
    if event.get("post_type") != "message":
        return MessageDecision(False, "ignored_non_message")

    if event.get("message_type") != "private":
        return MessageDecision(False, "ignored_non_private")

    user_id = parse_qq_user_id(event.get("user_id"))
    if user_id is None:
        return MessageDecision(False, "ignored_missing_user")

    if user_id not in allowed_users:
        return MessageDecision(False, "ignored_not_allowed")

    envelope = parse_inbound_message(event)
    if envelope is None:
        return MessageDecision(False, "unsupported_message")

    has_supported_media = any(
        attachment.kind in {"image", "audio", "file"}
        for attachment in envelope.attachments
    )
    has_supported_quote = enable_quote_resolution and envelope.quote is not None
    if not envelope.text and not (
        (enable_media_pipeline and has_supported_media) or has_supported_quote
    ):
        return MessageDecision(False, "unsupported_message", envelope=envelope)

    if (
        duplicate_checker is not None
        and envelope.message_id
        and duplicate_checker(envelope.message_id)
    ):
        return MessageDecision(False, "duplicate_message", envelope=envelope)

    if not rate_limiter.allow(str(user_id)):
        return MessageDecision(False, "rate_limited")

    request = None
    if envelope.text or has_supported_quote:
        request = AgentRequest(
            user_id=envelope.user_id,
            message_id=envelope.message_id,
            text=envelope.text,
            timestamp=envelope.timestamp,
        )

    return MessageDecision(
        True,
        "accepted",
        request=request,
        envelope=envelope,
    )


def evaluate_group_event(
    event: Mapping[str, object],
    *,
    bot_qq: int,
    allowed_groups: Set[int],
    group_limiter: RateLimiter,
    user_limiter: RateLimiter,
    reply_sender_id: Optional[int] = None,
    allow_untriggered: bool = False,
) -> GroupMessageDecision:
    control_type = None
    try:
        result = _evaluate_group_event(
            event,
            bot_qq=bot_qq,
            allowed_groups=allowed_groups,
            group_limiter=group_limiter,
            user_limiter=user_limiter,
            reply_sender_id=reply_sender_id,
            allow_untriggered=allow_untriggered,
        )
    except BaseException as exc:
        if type(exc) in _CONTROL_EXCEPTIONS:
            control_type = type(exc)
        else:
            return GroupMessageDecision(False, "invalid_group_event")
    if control_type is not None:
        raise control_type().with_traceback(None) from None
    return result


def _evaluate_group_event(
    event: Mapping[str, object],
    *,
    bot_qq: int,
    allowed_groups: Set[int],
    group_limiter: RateLimiter,
    user_limiter: RateLimiter,
    reply_sender_id: Optional[int] = None,
    allow_untriggered: bool = False,
) -> GroupMessageDecision:
    if event.get("post_type") != "message":
        return GroupMessageDecision(False, "ignored_non_message")
    if event.get("message_type") != "group":
        return GroupMessageDecision(False, "ignored_non_group")

    group_id = parse_qq_user_id(event.get("group_id"))
    if group_id is None:
        return GroupMessageDecision(False, "ignored_missing_group")
    if group_id not in allowed_groups:
        return GroupMessageDecision(False, "ignored_not_allowed")

    envelope = parse_group_message(event, bot_qq)
    if envelope is None:
        message = event.get("message")
        trigger_shaped = isinstance(message, list) and any(
            isinstance(segment, Mapping)
            and segment.get("type") in {"at", "reply"}
            for segment in message
        )
        reason = "ignored_not_triggered" if trigger_shaped else "unsupported_message"
        return GroupMessageDecision(False, reason)

    parsed_bot = (
        bot_qq
        if type(bot_qq) is int and 0 < bot_qq <= 10**20 - 1
        else None
    )
    parsed_reply_sender = (
        reply_sender_id
        if type(reply_sender_id) is int and 0 < reply_sender_id <= 10**20 - 1
        else None
    )
    reply_triggered = (
        envelope.quote is not None
        and parsed_bot is not None
        and parsed_reply_sender == parsed_bot
    )
    if not envelope.mentioned_bot and not reply_triggered and allow_untriggered is not True:
        return GroupMessageDecision(
            False, "ignored_not_triggered", envelope=envelope
        )

    control_type = None
    try:
        rate_decision = group_limiter.allow_pair(
            f"group:{group_id}",
            user_limiter,
            f"group:{group_id}:user:{envelope.user_id}",
        )
        if type(rate_decision) is not str or rate_decision not in _PAIR_RESULTS:
            return GroupMessageDecision(
                False, "rate_limit_failed", envelope=envelope
            )
        if rate_decision == "first_limited":
            return GroupMessageDecision(
                False, "group_rate_limited", envelope=envelope
            )
        if rate_decision == "second_limited":
            return GroupMessageDecision(
                False, "user_rate_limited", envelope=envelope
            )
    except BaseException as exc:
        if type(exc) in _CONTROL_EXCEPTIONS:
            control_type = type(exc)
        else:
            return GroupMessageDecision(
                False, "rate_limit_failed", envelope=envelope
            )
    if control_type is not None:
        raise control_type().with_traceback(None) from None

    request = GroupMessageRequest(
        group_id=envelope.group_id,
        user_id=envelope.user_id,
        message_id=envelope.message_id,
        text=envelope.text,
        timestamp=envelope.timestamp,
    )
    return GroupMessageDecision(
        True,
        "accepted",
        request=request,
        envelope=envelope,
    )
