import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class _Rpc:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def request(self, websocket, action, params, timeout=5):
        self.calls.append((websocket, action, params, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class GroupDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from qq_openclaw_bridge.application.group_chat import GroupReply
        from qq_openclaw_bridge.models import GroupMessageRequest
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = WorkspaceStateStore(
            Path(self.temp.name) / "state.sqlite3"
        ).community_style_repository
        self.request = GroupMessageRequest(77, 88, "123", "问题", 1.0)
        self.reply = GroupReply("回答", "profile", 2)

    async def test_delivery_requires_strict_ok_ack_and_records_message_id(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": 888}})
        delivery = GroupDeliveryService(
            self.repo, rpc, timeout_seconds=3, now_provider=lambda: NOW
        )
        websocket = object()

        self.assertTrue(await delivery.send(websocket, self.request, self.reply))
        self.assertEqual(rpc.calls, [(
            websocket,
            "send_group_msg",
            {"group_id": 77, "message": "回答"},
            3.0,
        )])
        self.assertTrue(self.repo.is_bot_message(77, "888"))

    async def test_delivery_filters_only_the_outbound_group_message(self):
        import json

        from qq_openclaw_bridge.application.group_chat import GroupReply
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )

        path = Path(self.temp.name) / "group-homophones.json"
        path.write_text(
            json.dumps(
                {"version": 1, "replacements": {"敏感词": "米感词"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sanitizer = GroupHomophoneFilter(path)
        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": 889}})
        delivery = GroupDeliveryService(
            self.repo,
            rpc,
            now_provider=lambda: NOW,
            text_filter=sanitizer.replace,
        )
        reply = GroupReply("回答里有敏感词", "profile", 2)

        self.assertTrue(await delivery.send(object(), self.request, reply))
        self.assertEqual(
            rpc.calls[0][2],
            {"group_id": 77, "message": "回答里有米感词"},
        )
        self.assertEqual(reply.text, "回答里有敏感词")

    async def test_filter_failure_blocks_group_send_and_records_safe_error(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        def fail_filter(_text):
            raise ValueError("sensitive mapping details must not leak")

        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": 890}})
        delivery = GroupDeliveryService(
            self.repo,
            rpc,
            now_provider=lambda: NOW,
            text_filter=fail_filter,
        )

        self.assertFalse(await delivery.send(object(), self.request, self.reply))
        self.assertEqual(rpc.calls, [])
        with self.repo._connection() as connection:
            stored = connection.execute(
                "SELECT safe_error_class FROM group_message_receipts "
                "WHERE group_id=77 ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(stored, "filter_error")

    async def test_malformed_or_failed_ack_is_safely_failed_without_text_leak(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        bad_responses = (
            {"status": "failed", "retcode": 0, "data": {"message_id": 888}},
            {"status": type("Status", (str,), {})("ok"), "retcode": 0, "data": {"message_id": 888}},
            {"status": "ok", "data": {"message_id": 888}},
            {"status": "ok", "retcode": "0", "data": {"message_id": 888}},
            {"status": "ok", "retcode": True, "data": {"message_id": 888}},
            {"status": "ok", "retcode": 1, "data": {"message_id": 888}},
            {"status": "ok", "retcode": 0, "data": {}},
            {"status": "ok", "retcode": 0, "data": {"message_id": True}},
            {"status": "ok", "retcode": 0, "data": {"message_id": " 888"}},
            {"status": "ok", "retcode": 0, "data": {"message_id": 0}},
        )
        for index, response in enumerate(bad_responses, 1):
            with self.subTest(response=response):
                rpc = _Rpc(response)
                request = type(self.request)(77, 88, str(200 + index), "问题", 1.0)
                delivery = GroupDeliveryService(
                    self.repo, rpc, now_provider=lambda: NOW
                )
                self.assertFalse(await delivery.send(object(), request, self.reply))

        secret = "https://secret.invalid/?token=leak"
        request = type(self.request)(77, 88, "300", "问题", 1.0)
        delivery = GroupDeliveryService(
            self.repo, _Rpc(error=RuntimeError(secret)), now_provider=lambda: NOW
        )
        self.assertFalse(await delivery.send(object(), request, self.reply))
        with self.repo._connection() as connection:
            stored = connection.execute(
                "SELECT safe_error_class FROM group_message_receipts "
                "WHERE group_id=77 ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(stored, "rpc_error")
        self.assertNotIn("secret", stored)

    async def test_duplicate_delivery_claim_sends_only_once(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": "900"}})
        delivery = GroupDeliveryService(
            self.repo, rpc, now_provider=lambda: NOW
        )
        results = await asyncio.gather(
            delivery.send(object(), self.request, self.reply),
            delivery.send(object(), self.request, self.reply),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(rpc.calls), 1)

    async def test_preclaimed_delivery_does_not_claim_a_second_time(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": "901"}})
        delivery = GroupDeliveryService(
            self.repo, rpc, now_provider=lambda: NOW
        )
        token = await delivery.claim(self.request)

        self.assertIs(type(token), str)
        self.assertTrue(await delivery.send(
            object(), self.request, self.reply, claim_token=token
        ))
        self.assertTrue(self.repo.is_bot_message(77, "901"))

    async def test_cancelled_rpc_leaves_claim_leased_and_propagates(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        started = asyncio.Event()

        class Rpc:
            async def request(self, *args, **kwargs):
                started.set()
                await asyncio.Event().wait()

        delivery = GroupDeliveryService(
            self.repo, Rpc(), now_provider=lambda: NOW
        )
        task = asyncio.create_task(delivery.send(object(), self.request, self.reply))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertIsNone(self.repo.claim_group_message(
            77, "123", NOW, lease_seconds=300
        ))

    async def test_cancellation_during_complete_waits_for_atomic_write(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        entered = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Repository:
            def claim_group_message(self, *args):
                return NOW.isoformat(timespec="microseconds")

            def complete_group_message(self, *args):
                loop.call_soon_threadsafe(entered.set)
                future = asyncio.run_coroutine_threadsafe(release.wait(), loop)
                future.result(1)
                return True

            def fail_group_message(self, *args):
                raise AssertionError("successful ack must not fail receipt")

        delivery = GroupDeliveryService(
            Repository(),
            _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": 902}}),
            now_provider=lambda: NOW,
        )
        task = asyncio.create_task(delivery.send(
            object(), self.request, self.reply
        ))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_repository_failures_are_bounded_and_never_send_twice(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        secret = "sqlite secret body"

        class ClaimFailure:
            def claim_group_message(self, *args):
                raise RuntimeError(secret)

            def complete_group_message(self, *args):
                raise AssertionError

            def fail_group_message(self, *args):
                raise AssertionError

        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": 903}})
        delivery = GroupDeliveryService(
            ClaimFailure(), rpc, now_provider=lambda: NOW
        )
        self.assertFalse(await delivery.send(object(), self.request, self.reply))
        self.assertEqual(rpc.calls, [])

        class CompleteFailure(ClaimFailure):
            def claim_group_message(self, *args):
                return NOW.isoformat(timespec="microseconds")

            def complete_group_message(self, *args):
                raise RuntimeError(secret)

        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": 904}})
        delivery = GroupDeliveryService(
            CompleteFailure(), rpc, now_provider=lambda: NOW
        )
        self.assertFalse(await delivery.send(object(), self.request, self.reply))
        self.assertEqual(len(rpc.calls), 1)

    async def test_acknowledged_send_retries_only_local_complete(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        repository = self.repo

        class FlakyComplete:
            def __init__(self):
                self.complete_calls = 0

            def claim_group_message(self, *args):
                return repository.claim_group_message(*args)

            def complete_group_message(self, *args):
                self.complete_calls += 1
                if self.complete_calls == 1:
                    raise RuntimeError("sqlite secret")
                return repository.complete_group_message(*args)

            def fail_group_message(self, *args):
                return repository.fail_group_message(*args)

        wrapped = FlakyComplete()
        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": 905}})
        delivery = GroupDeliveryService(
            wrapped, rpc, now_provider=lambda: NOW
        )

        self.assertTrue(await delivery.send(object(), self.request, self.reply))
        self.assertFalse(await delivery.send(object(), self.request, self.reply))
        self.assertEqual(len(rpc.calls), 1)
        self.assertEqual(wrapped.complete_calls, 2)
        self.assertTrue(repository.is_bot_message(77, "905"))

    async def test_unknown_ack_state_never_replays_rpc_after_complete_exhausted(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService

        repository = self.repo

        class BrokenComplete:
            def claim_group_message(self, *args):
                return repository.claim_group_message(*args)

            def complete_group_message(self, *args):
                raise RuntimeError("sqlite secret")

            def fail_group_message(self, *args):
                return repository.fail_group_message(*args)

        rpc = _Rpc({"status": "ok", "retcode": 0, "data": {"message_id": 906}})
        delivery = GroupDeliveryService(
            BrokenComplete(), rpc, now_provider=lambda: NOW
        )

        self.assertFalse(await delivery.send(object(), self.request, self.reply))
        self.assertFalse(await delivery.send(object(), self.request, self.reply))
        self.assertEqual(len(rpc.calls), 1)


if __name__ == "__main__":
    unittest.main()
