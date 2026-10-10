import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from collections.abc import Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _WebSocket:
    def __init__(self):
        self.sent = asyncio.Queue()

    async def send(self, payload):
        await self.sent.put(json.loads(payload))


class OneBotRpcClientTests(unittest.IsolatedAsyncioTestCase):
    def test_combiners_preserve_zero_and_one_consumer_identity(self):
        from qq_openclaw_bridge.runtime.onebot_rpc import (
            combine_disconnect_hooks, combine_payload_consumers,
        )

        consumer = lambda payload: False
        hook = lambda: None
        self.assertIsNone(combine_payload_consumers())
        self.assertIs(combine_payload_consumers(None, consumer, None), consumer)
        self.assertIsNone(combine_disconnect_hooks())
        self.assertIs(combine_disconnect_hooks(None, hook, None), hook)

    def test_composite_consumers_all_run_and_only_exact_true_consumes(self):
        from qq_openclaw_bridge.runtime.onebot_rpc import combine_payload_consumers

        calls = []

        def consumer(name, result=None, error=None):
            def consume(payload):
                calls.append((name, payload))
                if error is not None:
                    raise error
                return result
            return consume

        combined = combine_payload_consumers(
            consumer("first", True),
            consumer("broken", error=RuntimeError("secret")),
            consumer("malicious", 1),
            consumer("last", False),
        )
        payload = {"echo": "x"}

        self.assertTrue(combined(payload))
        self.assertEqual(calls, [
            ("first", payload), ("broken", payload),
            ("malicious", payload), ("last", payload),
        ])
        self.assertEqual(combine_payload_consumers(lambda value: 1)(payload), 1)

    def test_composite_disconnect_hooks_all_run_despite_failure(self):
        from qq_openclaw_bridge.runtime.onebot_rpc import combine_disconnect_hooks

        calls = []

        def first():
            calls.append("first")
            raise RuntimeError("secret")

        combined = combine_disconnect_hooks(first, lambda: calls.append("last"))
        combined()

        self.assertEqual(calls, ["first", "last"])

    async def test_response_is_correlated_with_unpredictable_prefixed_echo(self):
        from qq_openclaw_bridge.runtime.onebot_rpc import OneBotRpcClient

        client = OneBotRpcClient()
        websocket = _WebSocket()
        first = asyncio.create_task(
            client.request(websocket, "get_msg", {"message_id": "9"})
        )
        first_action = await websocket.sent.get()
        second = asyncio.create_task(
            client.request(websocket, "get_msg", {"message_id": "10"})
        )
        second_action = await websocket.sent.get()

        self.assertEqual(first_action["action"], "get_msg")
        self.assertEqual(first_action["params"], {"message_id": "9"})
        self.assertRegex(first_action["echo"], r"^e4-rpc:[0-9a-f]+:[0-9a-f]{32}$")
        self.assertNotEqual(first_action["echo"], second_action["echo"])
        self.assertTrue(client.consume({
            "status": "ok",
            "data": {"message": "原文"},
            "echo": second_action["echo"],
        }))
        self.assertFalse(first.done())
        self.assertEqual((await second)["data"]["message"], "原文")
        self.assertTrue(client.consume({
            "status": "ok",
            "data": {"message": "第一条"},
            "echo": first_action["echo"],
        }))
        self.assertEqual((await first)["data"]["message"], "第一条")

    async def test_unknown_malformed_and_late_echoes_never_resolve_another_call(self):
        from qq_openclaw_bridge.runtime.onebot_rpc import OneBotRpcClient

        client = OneBotRpcClient()
        websocket = _WebSocket()
        pending = asyncio.create_task(client.request(websocket, "get_msg", {}))
        action = await websocket.sent.get()

        self.assertFalse(client.consume({"status": "ok", "data": {}, "echo": "other"}))
        self.assertFalse(client.consume([action["echo"]]))
        self.assertFalse(pending.done())
        self.assertTrue(client.consume({"echo": action["echo"], "status": []}))
        with self.assertRaises(RuntimeError):
            await pending
        self.assertFalse(client.consume({
            "status": "ok", "data": {}, "echo": action["echo"]
        }))

    async def test_timeout_cancellation_and_disconnect_clean_exact_pending_calls(self):
        from qq_openclaw_bridge.runtime.onebot_rpc import OneBotRpcClient

        client = OneBotRpcClient()
        websocket = _WebSocket()
        with self.assertRaises(asyncio.TimeoutError):
            await client.request(websocket, "get_msg", {}, timeout=0.001)
        timed_out = await websocket.sent.get()
        self.assertFalse(client.consume({
            "status": "ok", "data": {}, "echo": timed_out["echo"]
        }))

        cancelled = asyncio.create_task(client.request(websocket, "get_msg", {}))
        cancelled_action = await websocket.sent.get()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertFalse(client.consume({
            "status": "ok", "data": {}, "echo": cancelled_action["echo"]
        }))

        first = asyncio.create_task(client.request(websocket, "get_msg", {}))
        first_action = await websocket.sent.get()
        second = asyncio.create_task(client.request(websocket, "get_msg", {}))
        second_action = await websocket.sent.get()
        client.fail_pending()
        for task in (first, second):
            with self.assertRaises(RuntimeError):
                await task
        self.assertFalse(client.consume({
            "status": "ok", "data": {}, "echo": first_action["echo"]
        }))
        self.assertFalse(client.consume({
            "status": "ok", "data": {}, "echo": second_action["echo"]
        }))

    async def test_client_namespaces_prevent_cross_resolution_when_uuid_repeats(self):
        from qq_openclaw_bridge.runtime.onebot_rpc import (
            OneBotRpcClient, combine_payload_consumers,
        )

        fixed = type("FixedUuid", (), {"hex": "a" * 32})()
        with patch("qq_openclaw_bridge.runtime.onebot_rpc.uuid.uuid4", return_value=fixed):
            first_client = OneBotRpcClient()
            second_client = OneBotRpcClient()
            first_socket = _WebSocket()
            second_socket = _WebSocket()
            first = asyncio.create_task(first_client.request(first_socket, "get_msg", {}))
            second = asyncio.create_task(second_client.request(second_socket, "get_msg", {}))
            first_action = await first_socket.sent.get()
            second_action = await second_socket.sent.get()

        self.assertNotEqual(first_action["echo"], second_action["echo"])
        combined = combine_payload_consumers(first_client.consume, second_client.consume)
        self.assertTrue(combined({
            "status": "ok", "data": {"owner": "first"},
            "echo": first_action["echo"],
        }))
        self.assertEqual((await first)["data"], {"owner": "first"})
        self.assertFalse(second.done())
        self.assertTrue(combined({
            "status": "ok", "data": {"owner": "second"},
            "echo": second_action["echo"],
        }))
        self.assertEqual((await second)["data"], {"owner": "second"})

    async def test_hostile_mapping_does_not_remove_matching_pending_call(self):
        from qq_openclaw_bridge.runtime.onebot_rpc import OneBotRpcClient

        client = OneBotRpcClient()
        websocket = _WebSocket()
        pending = asyncio.create_task(client.request(websocket, "get_msg", {}))
        action = await websocket.sent.get()

        class HostileMapping(Mapping):
            def __getitem__(self, key):
                if key == "echo":
                    return action["echo"]
                raise RuntimeError("secret payload")

            def __iter__(self):
                raise RuntimeError("secret payload")

            def __len__(self):
                return 4

        self.assertFalse(client.consume(HostileMapping()))
        self.assertFalse(pending.done())
        self.assertTrue(client.consume({
            "status": "ok", "data": {"safe": True}, "echo": action["echo"]
        }))
        self.assertEqual((await pending)["data"], {"safe": True})


class QuoteResolverTests(unittest.IsolatedAsyncioTestCase):
    def _envelope(self, quote=None):
        from qq_openclaw_bridge.domain.inbound import InboundEnvelope

        return InboundEnvelope(7, "m1", 1.0, ("问题",), quote=quote)

    async def test_absent_and_embedded_quotes_do_not_call_rpc(self):
        from qq_openclaw_bridge.application.quotes import QuoteResolver
        from qq_openclaw_bridge.domain.inbound import QuoteRef

        class Rpc:
            async def request(self, *args, **kwargs):
                raise AssertionError("RPC must not run")

        resolver = QuoteResolver(Rpc())
        absent = await resolver.resolve(self._envelope(), object())
        embedded = await resolver.resolve(
            self._envelope(QuoteRef("q1", "事件内引用")), object()
        )

        self.assertEqual((absent.status, absent.text, absent.source_message_id),
                         ("absent", "", None))
        self.assertEqual((embedded.status, embedded.text, embedded.source_message_id),
                         ("available", "事件内引用", "q1"))

    async def test_fetches_structured_text_and_controls_all_failures(self):
        from qq_openclaw_bridge.application.quotes import QuoteResolver
        from qq_openclaw_bridge.domain.inbound import QuoteRef

        class Rpc:
            def __init__(self, response=None, error=None):
                self.response = response
                self.error = error
                self.calls = []

            async def request(self, websocket, action, params, timeout=5):
                self.calls.append((websocket, action, params, timeout))
                if self.error is not None:
                    raise self.error
                return self.response

        response = {
            "status": "ok",
            "data": {"message": [
                {"type": "text", "data": {"text": "引用"}},
                {"type": "image", "data": {"url": "https://ignored.invalid"}},
                {"type": "text", "data": {"text": "原文"}},
            ]},
        }
        rpc = Rpc(response)
        websocket = object()
        resolver = QuoteResolver(rpc)
        artifact = await resolver.resolve(
            self._envelope(QuoteRef("q9")), websocket
        )

        self.assertEqual(artifact.text, "引用 原文")
        self.assertEqual(artifact.status, "available")
        self.assertEqual(rpc.calls, [
            (websocket, "get_msg", {"message_id": "q9"}, 5)
        ])

        for failing_rpc in (
            Rpc({"status": "ok", "data": {"message": {"raw": "secret"}}}),
            Rpc(error=RuntimeError("https://secret.invalid/?token=leak")),
        ):
            with self.subTest(response=failing_rpc.response):
                unavailable = await QuoteResolver(failing_rpc).resolve(
                    self._envelope(QuoteRef("q10")), websocket
                )
                self.assertEqual(unavailable.status, "unavailable")
                self.assertEqual(unavailable.text, "")
                self.assertEqual(unavailable.source_message_id, "q10")
                self.assertNotIn("secret", repr(unavailable))


if __name__ == "__main__":
    unittest.main()
