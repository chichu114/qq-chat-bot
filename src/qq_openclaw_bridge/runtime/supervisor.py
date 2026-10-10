import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional, Sequence


def _default_connect(url: str):
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError(
            "Missing runtime dependency: install with `python3 -m pip install -r requirements.txt`"
        ) from exc
    return websockets.connect(url)


class WebSocketSupervisor:
    def __init__(
        self,
        url: str,
        event_callback: Callable[[dict, object], Awaitable[object]],
        logger: logging.Logger,
        connect_factory: Callable[[str], object] = _default_connect,
        worker_factories: Sequence[
            Callable[[object], Awaitable[None]]
        ] = (),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        payload_consumer: Optional[Callable[[dict], bool]] = None,
        disconnect_hook: Optional[Callable[[], None]] = None,
    ) -> None:
        self._url = url
        self._event_callback = event_callback
        self._logger = logger
        self._connect_factory = connect_factory
        self._worker_factories = tuple(worker_factories)
        self._sleep = sleep
        self._payload_consumer = payload_consumer
        self._disconnect_hook = disconnect_hook

    async def run_forever(self) -> None:
        reconnect_delay = 1.0
        while True:
            try:
                async with self._connect_factory(self._url) as websocket:
                    self._logger.info("connected to OneBot WebSocket")
                    reconnect_delay = 1.0
                    workers = [
                        asyncio.create_task(factory(websocket))
                        for factory in self._worker_factories
                    ]
                    if workers:
                        await asyncio.sleep(0)
                    try:
                        async for raw_message in websocket:
                            try:
                                event = json.loads(raw_message)
                            except json.JSONDecodeError:
                                self._logger.warning(
                                    "ignored non-JSON WebSocket message"
                                )
                                continue
                            if isinstance(event, dict):
                                if (
                                    self._payload_consumer is not None
                                    and self._payload_consumer(event)
                                ):
                                    continue
                                await self._event_callback(event, websocket)
                    finally:
                        try:
                            if self._disconnect_hook is not None:
                                self._disconnect_hook()
                        finally:
                            for task in workers:
                                task.cancel()
                            if workers:
                                await asyncio.gather(
                                    *workers, return_exceptions=True
                                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "OneBot WebSocket connection failed; reconnecting in %.1fs",
                    reconnect_delay,
                )
                await self._sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
