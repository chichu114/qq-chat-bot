"""Acknowledged, idempotent delivery for QQ group replies."""

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Callable, Optional

from .group_chat import GroupReply
from ..models import GroupMessageRequest


_CONTROL_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


def _clean_control(error: BaseException) -> Optional[BaseException]:
    if type(error) not in _CONTROL_EXCEPTIONS:
        return None
    return type(error)()


def _sent_message_id(response: object) -> str:
    try:
        if not isinstance(response, Mapping):
            raise ValueError
        status = response.get("status")
        if type(status) is not str or status != "ok":
            raise ValueError
        retcode = response.get("retcode")
        if type(retcode) is not int or retcode != 0:
            raise ValueError
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise ValueError
        value = data.get("message_id")
        if type(value) is int:
            message_id = value
        elif (
            type(value) is str
            and len(value) <= 20
            and value.isascii()
            and value.isdecimal()
        ):
            message_id = int(value)
        else:
            raise ValueError
        if not 0 < message_id <= 10 ** 20 - 1:
            raise ValueError
        return str(message_id)
    except BaseException as error:
        control = _clean_control(error)
        if control is not None:
            raise control from None
        raise ValueError("invalid OneBot acknowledgement") from None


def _safe_delivery_error(error: BaseException) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection"
    if isinstance(error, ValueError):
        return "invalid_ack"
    return "rpc_error"


def _captured_call(function, arguments):
    """Never let a foreign BaseException escape through an executor future."""
    try:
        return "ok", function(*arguments)
    except BaseException as error:
        if type(error) in _CONTROL_EXCEPTIONS:
            return "control", type(error)
        return "error", None


class GroupDeliveryService:
    def __init__(
        self,
        repository,
        rpc,
        *,
        timeout_seconds: float = 5.0,
        now_provider: Optional[Callable[[], datetime]] = None,
        text_filter: Optional[Callable[[str], str]] = None,
    ) -> None:
        required = (
            "claim_group_message", "complete_group_message", "fail_group_message"
        )
        if any(not callable(getattr(repository, name, None)) for name in required):
            raise ValueError("invalid group delivery repository")
        if not callable(getattr(rpc, "request", None)):
            raise ValueError("invalid group delivery RPC client")
        if (
            type(timeout_seconds) not in (int, float)
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= 300
        ):
            raise ValueError("invalid group delivery timeout")
        self._repository = repository
        self._rpc = rpc
        self._timeout_seconds = float(timeout_seconds)
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        if not callable(self._now):
            raise ValueError("invalid group delivery clock")
        self._text_filter = text_filter or (lambda text: text)
        if not callable(self._text_filter):
            raise ValueError("invalid group text filter")

    async def send(
        self,
        websocket,
        request: GroupMessageRequest,
        reply: GroupReply,
        *,
        claim_token: Optional[str] = None,
    ) -> bool:
        if type(request) is not GroupMessageRequest or type(reply) is not GroupReply:
            raise ValueError("invalid group delivery input")
        token = claim_token
        if token is None:
            try:
                token = await self.claim(request)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                control = _clean_control(error)
                if control is not None:
                    raise control from None
                return False
        elif type(token) is not str or not token or len(token) > 64:
            raise ValueError("invalid group delivery claim")
        if token is None:
            return False
        control_type = None
        error_class = None
        try:
            outbound_text = self._text_filter(reply.text)
            if (
                type(outbound_text) is not str
                or not outbound_text
                or len(outbound_text) > 4096
            ):
                raise ValueError("invalid filtered group reply")
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            control_type = type(error) if type(error) in _CONTROL_EXCEPTIONS else None
            if control_type is None:
                error_class = "filter_error"
        if control_type is None and error_class is None:
            try:
                response = await self._rpc.request(
                    websocket,
                    "send_group_msg",
                    {"group_id": request.group_id, "message": outbound_text},
                    timeout=self._timeout_seconds,
                )
                outbound_id = _sent_message_id(response)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                control_type = (
                    type(error) if type(error) in _CONTROL_EXCEPTIONS else None
                )
                if control_type is None:
                    error_class = _safe_delivery_error(error)
        if control_type is not None:
            raise control_type().with_traceback(None) from None
        if error_class is not None:
            try:
                await self._finish(
                    self._repository.fail_group_message,
                    request.group_id,
                    request.message_id,
                    token,
                    error_class,
                    self._safe_now(),
                )
            except asyncio.CancelledError:
                raise
            except BaseException as finish_error:
                control = _clean_control(finish_error)
                if control is not None:
                    raise control from None
            return False

        for attempt in range(2):
            try:
                return bool(await self._finish(
                    self._repository.complete_group_message,
                    request.group_id,
                    request.message_id,
                    token,
                    outbound_id,
                    self._safe_now(),
                ))
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                control = _clean_control(error)
                if control is not None:
                    raise control from None
                if attempt == 1:
                    return False
        return False

    async def claim(self, request: GroupMessageRequest) -> Optional[str]:
        if type(request) is not GroupMessageRequest:
            raise ValueError("invalid group delivery request")
        token = await asyncio.to_thread(
            self._repository.claim_group_message,
            request.group_id,
            request.message_id,
            self._safe_now(),
        )
        if token is not None and (type(token) is not str or not token or len(token) > 64):
            raise RuntimeError("invalid group receipt claim")
        return token

    async def _finish(self, function, *arguments):
        task = asyncio.create_task(asyncio.to_thread(
            _captured_call, function, arguments
        ))
        cancelled = False
        while True:
            try:
                kind, value = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                continue
        if cancelled:
            raise asyncio.CancelledError() from None
        if kind == "control":
            raise value().with_traceback(None) from None
        if kind == "error":
            raise RuntimeError("group delivery persistence failed") from None
        return value

    def _safe_now(self) -> datetime:
        try:
            value = self._now()
            if (
                type(value) is not datetime
                or value.tzinfo is None
                or value.utcoffset() is None
                or value.replace(fold=0).utcoffset()
                != value.replace(fold=1).utcoffset()
            ):
                raise ValueError
            return value
        except BaseException as error:
            control = _clean_control(error)
            if control is not None:
                raise control from None
            raise RuntimeError("group delivery clock failed") from None


__all__ = ["GroupDeliveryService"]
