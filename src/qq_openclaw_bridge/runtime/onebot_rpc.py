import asyncio
import itertools
import json
import math
import uuid
from collections.abc import Mapping
from typing import Dict, Optional


class OneBotRpcError(RuntimeError):
    pass


_CONTROL_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)
_CLIENT_IDS = itertools.count(1)
_MAX_RESPONSE_NODES = 4096
_MAX_RESPONSE_DEPTH = 24


def _canonical_value(value, state, depth=0):
    if depth > _MAX_RESPONSE_DEPTH:
        raise ValueError("malformed OneBot RPC response")
    state[0] += 1
    if state[0] > _MAX_RESPONSE_NODES:
        raise ValueError("malformed OneBot RPC response")
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("malformed OneBot RPC response")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_RESPONSE_NODES:
            raise ValueError("malformed OneBot RPC response")
        result = {}
        for key in value:
            if type(key) is not str or key in result:
                raise ValueError("malformed OneBot RPC response")
            result[key] = _canonical_value(value[key], state, depth + 1)
        return result
    if type(value) in (list, tuple):
        if len(value) > _MAX_RESPONSE_NODES:
            raise ValueError("malformed OneBot RPC response")
        return [
            _canonical_value(item, state, depth + 1)
            for item in value
        ]
    raise ValueError("malformed OneBot RPC response")


def combine_payload_consumers(*consumers):
    active = tuple(consumer for consumer in consumers if consumer is not None)
    if any(not callable(consumer) for consumer in active):
        raise ValueError("payload consumers must be callable")
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def consume(payload) -> bool:
        consumed = False
        control = None
        for consumer in active:
            try:
                result = consumer(payload)
            except BaseException as error:
                if type(error) in _CONTROL_EXCEPTIONS and control is None:
                    control = type(error)
                continue
            if type(result) is bool and result:
                consumed = True
        if control is not None:
            raise control().with_traceback(None) from None
        return consumed

    return consume


def combine_disconnect_hooks(*hooks):
    active = tuple(hook for hook in hooks if hook is not None)
    if any(not callable(hook) for hook in active):
        raise ValueError("disconnect hooks must be callable")
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def disconnect() -> None:
        control = None
        for hook in active:
            try:
                hook()
            except BaseException as error:
                if type(error) in _CONTROL_EXCEPTIONS and control is None:
                    control = type(error)
        if control is not None:
            raise control().with_traceback(None) from None

    return disconnect


class OneBotRpcClient:
    def __init__(self) -> None:
        self._pending: Dict[str, asyncio.Future] = {}
        self._namespace = format(next(_CLIENT_IDS), "x")

    async def request(
        self, websocket, action, params, timeout=5
    ) -> Mapping[str, object]:
        echo = "e4-rpc:%s:%s" % (self._namespace, uuid.uuid4().hex)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[echo] = future
        payload = {"action": action, "params": params, "echo": echo}
        try:
            await websocket.send(json.dumps(payload, ensure_ascii=False))
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except BaseException:
            if self._pending.get(echo) is future:
                self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            raise

    def consume(self, payload) -> bool:
        if not isinstance(payload, Mapping):
            return False
        try:
            echo = payload.get("echo")
        except BaseException as error:
            control = type(error) if type(error) in _CONTROL_EXCEPTIONS else None
            if control is not None:
                raise control().with_traceback(None) from None
            return False
        if type(echo) is not str:
            return False
        future = self._pending.get(echo)
        if future is None:
            return False
        try:
            canonical = _canonical_value(payload, [0])
        except ValueError:
            if self._pending.get(echo) is not future:
                return False
            self._pending.pop(echo, None)
            future.set_exception(OneBotRpcError("malformed OneBot RPC response"))
            return True
        except BaseException as error:
            control = type(error) if type(error) in _CONTROL_EXCEPTIONS else None
            if control is not None:
                raise control().with_traceback(None) from None
            return False
        if not self._valid_response(canonical) or canonical.get("echo") != echo:
            if self._pending.get(echo) is not future:
                return False
            self._pending.pop(echo, None)
            future.set_exception(OneBotRpcError("malformed OneBot RPC response"))
            return True
        if self._pending.get(echo) is not future:
            return False
        self._pending.pop(echo, None)
        future.set_result(canonical)
        return True

    def fail_pending(self, error: Optional[Exception] = None) -> None:
        failure = error or OneBotRpcError("OneBot RPC connection closed")
        pending = tuple(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(failure)

    @staticmethod
    def _valid_response(payload: Mapping) -> bool:
        return type(payload.get("status")) is str and "data" in payload


__all__ = [
    "OneBotRpcClient",
    "OneBotRpcError",
    "combine_disconnect_hooks",
    "combine_payload_consumers",
]
