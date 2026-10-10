import asyncio
import json
import logging
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _Connection:
    def __init__(self, messages):
        self._messages = iter(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._messages)
        except StopIteration:
            raise asyncio.CancelledError()


class _ErrorConnection(_Connection):
    async def __anext__(self):
        raise RuntimeError("connection failed")


class RuntimeSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_json_and_cancels_connection_scoped_workers(self):
        from qq_openclaw_bridge.runtime.supervisor import WebSocketSupervisor

        events = []
        cancelled = []

        async def event_callback(event, websocket):
            events.append(event)

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        connection = _Connection(
            ["not-json", json.dumps([1, 2]), json.dumps({"user_id": 1})]
        )
        supervisor = WebSocketSupervisor(
            "ws://example",
            event_callback,
            logging.getLogger("test.supervisor"),
            connect_factory=lambda url: connection,
            worker_factories=(lambda websocket: worker(),),
        )

        with self.assertRaises(asyncio.CancelledError):
            await supervisor.run_forever()

        self.assertEqual(events, [{"user_id": 1}])
        self.assertEqual(cancelled, [True])

    async def test_rpc_consumer_runs_before_dispatch_and_disconnect_precedes_cleanup(self):
        from qq_openclaw_bridge.runtime.supervisor import WebSocketSupervisor

        order = []
        events = []

        async def event_callback(event, websocket):
            events.append(event)

        def consume(payload):
            order.append(("consume", payload.get("echo")))
            return payload.get("echo") == "rpc"

        def disconnect():
            order.append(("disconnect", None))

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                order.append(("worker_cleanup", None))

        connection = _Connection([
            json.dumps({"echo": "rpc", "status": "ok", "data": {}}),
            json.dumps({"user_id": 1}),
        ])
        supervisor = WebSocketSupervisor(
            "ws://example",
            event_callback,
            logging.getLogger("test.supervisor"),
            connect_factory=lambda url: connection,
            worker_factories=(lambda websocket: worker(),),
            payload_consumer=consume,
            disconnect_hook=disconnect,
        )

        with self.assertRaises(asyncio.CancelledError):
            await supervisor.run_forever()

        self.assertEqual(events, [{"user_id": 1}])
        self.assertEqual(order[:2], [("consume", "rpc"), ("consume", None)])
        self.assertLess(order.index(("disconnect", None)),
                        order.index(("worker_cleanup", None)))

    async def test_disconnect_hook_runs_on_connection_error_before_reconnect(self):
        from qq_openclaw_bridge.runtime.supervisor import WebSocketSupervisor

        order = []

        async def stop_reconnect(delay):
            order.append(("sleep", delay))
            raise asyncio.CancelledError()

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                order.append(("worker_cleanup", None))

        supervisor = WebSocketSupervisor(
            "ws://example",
            lambda event, websocket: None,
            logging.getLogger("test.supervisor.error"),
            connect_factory=lambda url: _ErrorConnection([]),
            worker_factories=(lambda websocket: worker(),),
            disconnect_hook=lambda: order.append(("disconnect", None)),
            sleep=stop_reconnect,
        )

        with self.assertLogs("test.supervisor.error", level="ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                await supervisor.run_forever()

        self.assertLess(order.index(("disconnect", None)),
                        order.index(("worker_cleanup", None)))
        self.assertEqual(order[-1], ("sleep", 1.0))


if __name__ == "__main__":
    unittest.main()
