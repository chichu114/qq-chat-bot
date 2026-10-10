import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ReliableDeliveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_delivery_rejects_reserved_kinds_before_sender(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService

        sent = []

        async def sender(*args, **kwargs):
            sent.append(args)

        service = ReliableDeliveryService(None, sender, 0, 3)
        for kind in ("task_final", "task_notice", "e3_observation"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "reserved"):
                    await service.send(
                        "ws", 10001, "internal", echo="bad",
                        message_id="m1", kind=kind,
                        dedupe_key="%s:bad" % kind,
                    )
        self.assertEqual(sent, [])

    async def test_corrupted_reserved_rows_are_quarantined_before_side_effects(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        cases = (
            ("task_final", "reply:m1", "malformed", "final text"),
            ("task_notice", "task:missing:notice:1", "task:missing:notice:1", "notice"),
            ("e3_observation", "observation:m1", "reply:m1", "{}"),
        )
        for kind, key, echo, message in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
                now = datetime(2026, 7, 27, tzinfo=timezone.utc).isoformat()
                store.claim_inbound("m1", 10001)
                with store._connect() as connection:
                    if kind == "e3_observation":
                        connection.execute(
                            """
                            INSERT INTO outbox(
                              kind, user_id, message, echo, dedupe_key, status,
                              attempts, next_attempt_at, created_at, updated_at, sent_at
                            ) VALUES ('reply', 10001, 'sent final', 'reply:m1',
                                      'reply:m1', 'sent', 1, ?, ?, ?, ?)
                            """,
                            (now, now, now, now),
                        )
                    connection.execute(
                        """
                        INSERT INTO outbox(
                          kind, user_id, message, echo, dedupe_key, status,
                          attempts, next_attempt_at, created_at, updated_at
                        ) VALUES (?, 10001, ?, ?, ?, 'pending', 0, ?, ?, ?)
                        """,
                        (kind, message, echo, key, now, now, now),
                    )
                qq = []
                observed = []

                async def sender(*args, **kwargs):
                    qq.append(args)

                async def observer(*args, **kwargs):
                    observed.append(args)

                delivery = ReliableDeliveryService(
                    store, sender, 0, 3, observation_handler=observer
                )
                self.assertFalse(await delivery.deliver_once("ws"))

                row = store.get_outbox(key)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["last_error"], "invalid reserved outbox row")
                self.assertEqual(qq, [])
                self.assertEqual(observed, [])
                with store._connect() as connection:
                    inbound = connection.execute(
                        "SELECT status FROM inbound_messages WHERE message_id='m1'"
                    ).fetchone()[0]
                self.assertEqual(inbound, "processing")

    def _request(self):
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(10001, "m1", "你好", 1.0)

    async def test_without_store_sends_directly(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService

        sent = []

        async def sender(websocket, user_id, message, echo=None):
            sent.append((websocket, user_id, message, echo))

        service = ReliableDeliveryService(None, sender, 2, 8)
        await service.send_reply("ws", self._request(), "回复")

        self.assertEqual(sent, [("ws", 10001, "回复", "reply:m1")])

    async def test_reply_is_enqueued_delivered_and_completes_inbound(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.claim_inbound("m1", 10001)
            sent = []

            async def sender(websocket, user_id, message, echo=None):
                sent.append((user_id, message, echo))

            service = ReliableDeliveryService(store, sender, 2, 8)
            await service.send_reply("ws", self._request(), "可靠回复")

            row = store.get_outbox("reply:m1")
            self.assertEqual(row["status"], "sent")
            self.assertEqual(sent, [(10001, "可靠回复", "reply:m1")])
            self.assertFalse(store.claim_inbound("m1", 10001))

    async def test_final_delivery_handler_runs_only_after_retry_is_acknowledged(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.claim_inbound("m1", 10001)
            attempts = 0
            completed = []

            async def sender(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionError("offline")

            async def complete(user_id, message_id):
                completed.append((user_id, message_id))

            service = ReliableDeliveryService(
                store,
                sender,
                0,
                3,
                final_delivery_handler=complete,
            )
            await service.send_reply("ws", self._request(), "可靠回复")
            self.assertEqual(completed, [])
            self.assertEqual(store.get_outbox("reply:m1")["status"], "pending")

            self.assertTrue(await service.deliver_once("ws"))
            self.assertEqual(completed, [(10001, "m1")])
            self.assertEqual(store.get_outbox("reply:m1")["status"], "sent")

    async def test_final_delivery_handler_failure_does_not_undo_sent_reply(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.claim_inbound("m1", 10001)

            async def sender(*args, **kwargs):
                return None

            async def fail_completion(*args, **kwargs):
                raise OSError("controlled completion failure")

            service = ReliableDeliveryService(
                store,
                sender,
                0,
                3,
                final_delivery_handler=fail_completion,
            )
            await service.send_reply("ws", self._request(), "可靠回复")

            self.assertEqual(store.get_outbox("reply:m1")["status"], "sent")
            self.assertFalse(store.claim_inbound("m1", 10001))

    async def test_direct_task_final_retry_completes_orchestration_run(self):
        from qq_openclaw_bridge.application.coordinator_factory import create_direct_runtime
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.models import AgentResponse
        from qq_openclaw_bridge.orchestration.models import TaskContract
        from qq_openclaw_bridge.persistence.orchestration import SqliteOrchestrationTraceSink
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            request = self._request()

            async def model_reply(_request):
                return AgentResponse("最终回复")

            runtime = create_direct_runtime(
                model_reply,
                trace_sink=SqliteOrchestrationTraceSink(
                    store.orchestration_repository
                ),
            )
            await runtime.execute(TaskContract.from_request(request))
            self.assertEqual(
                store.orchestration_repository.latest_run(10001)["status"],
                "accepted",
            )

            store.claim_inbound("m1", 10001)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1"
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running"
            )
            attempts = 0

            async def sender(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionError("offline")

            async def complete(user_id, message_id):
                store.orchestration_repository.complete_delivery(
                    user_id, message_id
                )

            async def observe(*args, **kwargs):
                return None

            service = ReliableDeliveryService(
                store,
                sender,
                0,
                3,
                observation_handler=observe,
                final_delivery_handler=complete,
            )
            self.assertFalse(
                await service.send_final("ws", request, "最终回复", task_id="t1")
            )
            self.assertEqual(
                store.orchestration_repository.latest_run(10001)["status"],
                "accepted",
            )

            self.assertTrue(await service.deliver_once("ws"))
            self.assertEqual(
                store.orchestration_repository.latest_run(10001)["status"],
                "delivered",
            )
            self.assertEqual(store.e4_repository.get_task("t1")["status"], "succeeded")

    async def test_existing_sent_reply_fast_path_is_owned_and_never_completes_inbound(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        cases = (
            ("exact_owner", "reply", 20002, "reply:m1", "reply:m1", True),
            ("cross_user", "reply", 10001, "reply:m1", "reply:m1", False),
            (
                "malformed_user", "reply", "not-an-integer",
                "reply:m1", "reply:m1", False,
            ),
            ("wrong_kind", "command", 20002, "reply:m1", "reply:m1", False),
            ("wrong_echo", "reply", 20002, "reply:m1", "broken-echo", False),
            ("wrong_key", "reply", 20002, "wrong:m1", "reply:m1", False),
        )
        for (
            label, stored_kind, stored_user, stored_key, stored_echo, expected
        ) in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
                store.claim_inbound("m1", 20002)
                row = store.enqueue_outbox(
                    kind="reply", user_id=20002, message="existing",
                    dedupe_key=stored_key, echo="reply:m1",
                )
                with store._connect() as connection:
                    connection.execute(
                        """
                        UPDATE outbox
                        SET kind=?, user_id=?, echo=?, status='sent', attempts=1,
                            sent_at=updated_at
                        WHERE id=?
                        """,
                        (stored_kind, stored_user, stored_echo, row["id"]),
                    )
                legacy_calls = []
                sends = []

                def fail_legacy_complete(*args, **kwargs):
                    legacy_calls.append(args)
                    raise RuntimeError("legacy completion boundary reached")

                async def sender(*args, **kwargs):
                    sends.append(args)

                store.complete_inbound = fail_legacy_complete
                service = ReliableDeliveryService(store, sender, 0, 3)
                escaped = None
                try:
                    result = await service.send(
                        "ws", 20002, "new reply", echo="reply:m1",
                        message_id="m1", kind="reply",
                        dedupe_key=stored_key,
                    )
                except RuntimeError as exc:
                    escaped = exc
                    result = None

                self.assertIsNone(escaped)
                self.assertEqual(result, expected)
                self.assertEqual(legacy_calls, [])
                self.assertEqual(sends, [])
                with store._connect() as connection:
                    inbound = connection.execute(
                        """
                        SELECT user_id, status, attempts FROM inbound_messages
                        WHERE message_id='m1'
                        """
                    ).fetchone()
                self.assertEqual(inbound, (20002, "processing", 1))

    def _prepare_ordinary_final(self, store, now):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService

        store.claim_inbound("m1", 10001, now=now)
        store.enqueue_final_with_observation(
            kind="reply",
            user_id=10001,
            message="最终回复",
            dedupe_key="reply:m1",
            echo="reply:m1",
            observation_message=ReliableDeliveryService._encode_observation(
                self._request(), "最终回复"
            ),
            observation_key="observation:m1",
            now=now,
        )

    @staticmethod
    def _ordinary_final_state(store):
        with store._connect() as connection:
            return connection.execute(
                """
                SELECT outbox.status, inbound_messages.status
                FROM outbox JOIN inbound_messages
                  ON inbound_messages.message_id='m1'
                WHERE outbox.dedupe_key='reply:m1'
                """
            ).fetchone()

    async def test_ordinary_final_ack_rolls_back_on_outbox_or_inbound_write_fault(self):
        import sqlite3

        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        now = datetime(2026, 7, 30, tzinfo=timezone.utc)
        triggers = (
            (
                "outbox_cas",
                """
                CREATE TRIGGER fail_final_outbox_cas
                BEFORE UPDATE OF status ON outbox
                WHEN OLD.dedupe_key='reply:m1' AND NEW.status='sent'
                BEGIN SELECT RAISE(ABORT, 'controlled outbox fault'); END
                """,
            ),
            (
                "inbound_completion",
                """
                CREATE TRIGGER fail_final_inbound_completion
                BEFORE UPDATE OF status ON inbound_messages
                WHEN OLD.message_id='m1' AND NEW.status='completed'
                BEGIN SELECT RAISE(ABORT, 'controlled inbound fault'); END
                """,
            ),
        )
        for label, trigger in triggers:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
                self._prepare_ordinary_final(store, now)
                with store._connect() as connection:
                    connection.execute(trigger)

                async def sender(*args, **kwargs):
                    return None

                service = ReliableDeliveryService(
                    store, sender, 0, 3, now_provider=lambda: now
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    await service.deliver_once("ws")

                self.assertEqual(
                    self._ordinary_final_state(store),
                    ("sending", "processing"),
                )

    async def test_ordinary_final_metric_failure_is_contained_after_atomic_commit(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 7, 30, tzinfo=timezone.utc)
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            self._prepare_ordinary_final(store, now)

            def fail_metric(*args, **kwargs):
                raise RuntimeError("controlled metric fault")

            store.set_runtime_metric = fail_metric

            async def sender(*args, **kwargs):
                return None

            service = ReliableDeliveryService(
                store, sender, 0, 3, now_provider=lambda: now
            )
            escaped = None
            try:
                delivered = await service.deliver_once("ws")
            except RuntimeError as exc:
                escaped = exc
                delivered = False

            self.assertIsNone(escaped)
            self.assertTrue(delivered)
            self.assertEqual(
                self._ordinary_final_state(store), ("sent", "completed")
            )

    async def test_ordinary_final_never_uses_former_inbound_completion_boundary(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 7, 30, tzinfo=timezone.utc)
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            self._prepare_ordinary_final(store, now)
            calls = []

            def fail_old_boundary(*args, **kwargs):
                calls.append(args)
                raise RuntimeError("former inbound boundary reached")

            store.complete_inbound = fail_old_boundary

            async def sender(*args, **kwargs):
                return None

            service = ReliableDeliveryService(
                store, sender, 0, 3, now_provider=lambda: now
            )
            escaped = None
            try:
                delivered = await service.deliver_once("ws")
            except RuntimeError as exc:
                escaped = exc
                delivered = False

            self.assertIsNone(escaped)
            self.assertEqual(calls, [])
            self.assertTrue(delivered)
            self.assertEqual(
                self._ordinary_final_state(store), ("sent", "completed")
            )

    async def test_ordinary_final_observation_failure_cannot_reopen_inbound(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 7, 30, tzinfo=timezone.utc)
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            self._prepare_ordinary_final(store, now)

            async def sender(*args, **kwargs):
                return None

            async def fail_observation(*args, **kwargs):
                raise RuntimeError("controlled observation fault")

            service = ReliableDeliveryService(
                store,
                sender,
                0,
                3,
                observation_handler=fail_observation,
                now_provider=lambda: now,
            )

            self.assertTrue(await service.deliver_once("ws"))
            self.assertEqual(
                self._ordinary_final_state(store), ("sent", "completed")
            )
            observation = store.get_outbox("observation:m1")
            self.assertEqual(observation["status"], "pending")

    async def test_failure_schedules_retry_and_sent_row_is_not_resent(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            calls = 0

            async def sender(websocket, user_id, message, echo=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ConnectionError("socket closed")

            service = ReliableDeliveryService(store, sender, 1, 8)
            await service.send_reply("ws", self._request(), "可靠回复")
            row = store.get_outbox("reply:m1")
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 1)

            await asyncio.sleep(1.05)
            self.assertTrue(await service.deliver_once("ws"))
            self.assertEqual(store.get_outbox("reply:m1")["status"], "sent")
            await service.send_reply("ws", self._request(), "可靠回复")
            self.assertEqual(calls, 2)

    async def test_task_notice_and_final_reply_have_distinct_durable_semantics(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.claim_inbound("m1", 10001)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="44"
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running"
            )
            sent = []

            async def sender(websocket, user_id, message, echo=None):
                sent.append((user_id, message, echo))

            service = ReliableDeliveryService(store, sender, 2, 8)
            await service.send_task_notice(
                "ws", "44", 10001, "处理中", role="ack"
            )
            with store._connect() as connection:
                status_after_notice = connection.execute(
                    "SELECT status FROM inbound_messages WHERE message_id='m1'"
                ).fetchone()[0]
            await service.send_reply("ws", self._request(), "最终回复")
            await service.send_task_notice(
                "ws", "44", 10001, "处理中", role="ack"
            )
            await service.send_reply("ws", self._request(), "最终回复")

            notice = store.get_outbox("task:44:notice:1")
            reply = store.get_outbox("reply:m1")
            self.assertEqual((notice["kind"], notice["status"]),
                             ("task_notice", "sent"))
            self.assertEqual((reply["kind"], reply["status"]), ("reply", "sent"))
            self.assertEqual(status_after_notice, "processing")
            with store._connect() as connection:
                final_status = connection.execute(
                    "SELECT status FROM inbound_messages WHERE message_id='m1'"
                ).fetchone()[0]
                outbox_count = connection.execute(
                    "SELECT COUNT(*) FROM outbox"
                ).fetchone()[0]
            self.assertEqual(final_status, "completed")
            self.assertEqual(outbox_count, 2)
            self.assertEqual(len(sent), 2)

    async def test_task_final_commit_pending_then_restart_delivers_and_observes(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            store = WorkspaceStateStore(path)
            store.claim_inbound("m1", 10001)
            task = store.e4_repository.create_task(10001, "m1", "long_task", task_id="t1")
            store.e4_repository.transition_task(task["id"], "queued", "running", "running")
            sender_calls = []

            async def failing_sender(websocket, user_id, message, echo=None):
                sender_calls.append(message)
                raise ConnectionError("offline")

            service = ReliableDeliveryService(store, failing_sender, 0, 3)
            delivered = await service.send_final(
                "ws", self._request(), "最终回复", task_id="t1"
            )

            self.assertFalse(delivered)
            self.assertEqual(store.e4_repository.get_task("t1")["status"], "running")
            self.assertEqual(
                store.e4_repository.get_task("t1")["stage"], "delivery_committed"
            )
            self.assertFalse(store.e4_repository.request_cancel("t1", 10001))
            self.assertEqual(store.get_outbox("reply:m1")["kind"], "task_final")
            self.assertEqual(store.get_outbox("reply:m1")["echo"], "task:t1:final")
            self.assertEqual(store.get_outbox("observation:m1")["status"], "pending")

            observed = []

            async def sender(websocket, user_id, message, echo=None):
                sender_calls.append(message)

            async def observe(request, reply):
                observed.append((request, reply))

            restarted = ReliableDeliveryService(
                WorkspaceStateStore(path), sender, 0, 3,
                observation_handler=observe,
            )
            self.assertTrue(await restarted.deliver_once("ws"))
            self.assertFalse(await restarted.deliver_once("ws"))

            restarted_store = restarted._state_store
            self.assertEqual(restarted_store.e4_repository.get_task("t1")["status"], "succeeded")
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], self._request())
            self.assertEqual(observed[0][1], "最终回复")
            self.assertEqual(sender_calls, ["最终回复", "最终回复"])

    async def test_cancel_before_task_final_commit_creates_no_outbox_rows(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            task = store.e4_repository.create_task(10001, "m1", "long_task", task_id="t1")
            store.e4_repository.transition_task(task["id"], "queued", "running", "running")
            self.assertIsNotNone(store.e4_repository.request_cancel("t1", 10001))

            service = ReliableDeliveryService(
                store,
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("cancelled final must not send")
                ),
                0,
                3,
            )
            with self.assertRaises(asyncio.CancelledError):
                await service.send_final("ws", self._request(), "不得入队", task_id="t1")

            self.assertIsNone(store.get_outbox("reply:m1"))
            self.assertIsNone(store.get_outbox("observation:m1"))

    async def test_exhausted_task_final_marks_task_failed_once(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            task = store.e4_repository.create_task(10001, "m1", "long_task", task_id="t1")
            store.e4_repository.transition_task(task["id"], "queued", "running", "running")

            async def sender(*args, **kwargs):
                raise ConnectionError("offline")

            service = ReliableDeliveryService(store, sender, 0, 1)
            self.assertFalse(
                await service.send_final("ws", self._request(), "最终回复", task_id="t1")
            )

            task = store.e4_repository.get_task("t1")
            self.assertEqual((task["status"], task["stage"]), ("failed", "delivery_failed"))
            events = store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["stage"] for event in events].count("delivery_failed"), 1
            )

    async def test_exhausted_task_final_terminalizes_pair_and_inbound(self):
        from datetime import datetime, timedelta, timezone
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 27, tzinfo=timezone.utc)
            store.claim_inbound("m1", 10001, now=now)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", now=now, task_id="t1"
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=now
            )

            async def sender(*args, **kwargs):
                raise ConnectionError("offline")

            service = ReliableDeliveryService(store, sender, 0, 1)
            self.assertFalse(
                await service.send_final("ws", self._request(), "最终回复", task_id="t1")
            )

            self.assertEqual(store.get_outbox("reply:m1")["status"], "failed")
            observation = store.get_outbox("observation:m1")
            self.assertEqual(observation["status"], "failed")
            self.assertEqual(observation["last_error"], "dependency final failed")
            self.assertFalse(
                store.claim_inbound("m1", 10001, now=now + timedelta(seconds=301))
            )

    async def test_exhausted_normal_final_terminalizes_pair_and_inbound(self):
        from datetime import datetime, timedelta, timezone
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 27, tzinfo=timezone.utc)
            store.claim_inbound("m1", 10001, now=now)

            async def sender(*args, **kwargs):
                raise ConnectionError("offline")

            service = ReliableDeliveryService(store, sender, 0, 1)
            self.assertFalse(await service.send_final("ws", self._request(), "最终回复"))

            self.assertEqual(store.get_outbox("reply:m1")["status"], "failed")
            self.assertEqual(store.get_outbox("observation:m1")["status"], "failed")
            self.assertFalse(
                store.claim_inbound("m1", 10001, now=now + timedelta(seconds=301))
            )

    async def test_observation_retries_durably_and_internal_json_never_reaches_sender(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            store = WorkspaceStateStore(path)
            sent = []
            attempts = []

            async def sender(websocket, user_id, message, echo=None):
                sent.append(message)

            async def failing_observer(request, reply):
                attempts.append(reply)
                raise RuntimeError("observer unavailable")

            service = ReliableDeliveryService(
                store, sender, 0, 3, observation_handler=failing_observer
            )
            self.assertTrue(await service.send_final("ws", self._request(), "最终回复"))
            self.assertEqual(store.get_outbox("observation:m1")["status"], "pending")

            observed = []

            async def observer(request, reply):
                observed.append((request.message_id, reply))

            restarted = ReliableDeliveryService(
                WorkspaceStateStore(path), sender, 0, 3,
                observation_handler=observer,
            )
            self.assertTrue(await restarted.deliver_once("ws"))
            self.assertFalse(await restarted.deliver_once("ws"))

            self.assertEqual(sent, ["最终回复"])
            self.assertEqual(attempts, ["最终回复"])
            self.assertEqual(observed, [("m1", "最终回复")])

    async def test_stored_observation_contains_only_minimal_learning_fields(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.models import AgentRequest
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            request = AgentRequest(
                10001,
                "m-sensitive",
                "用户问题",
                123.5,
                context="E3-SENTINEL",
                session_key="SESSION-SENTINEL",
                media_context=(
                    "QUOTE-SENTINEL https://media-secret.invalid/object"
                ),
                task_id="TASK-SENTINEL",
            )

            async def offline(*args, **kwargs):
                raise ConnectionError("offline")

            service = ReliableDeliveryService(store, offline, 1, 3)
            self.assertFalse(await service.send_final("ws", request, "最终回复"))

            raw = str(store.get_outbox("observation:m-sensitive")["message"])
            payload = json.loads(raw)
            self.assertEqual(
                payload,
                {
                    "user_id": 10001,
                    "message_id": "m-sensitive",
                    "text": "用户问题",
                    "timestamp": 123.5,
                    "reply": "最终回复",
                },
            )
            for sentinel in (
                "E3-SENTINEL", "SESSION-SENTINEL", "QUOTE-SENTINEL",
                "media-secret.invalid", "TASK-SENTINEL",
            ):
                self.assertNotIn(sentinel, raw)

            decoded, reply = ReliableDeliveryService._decode_observation(raw)
            self.assertEqual(reply, "最终回复")
            self.assertEqual((decoded.context, decoded.media_context), ("", ""))
            self.assertIsNone(decoded.session_key)
            self.assertIsNone(decoded.task_id)

    async def test_expired_last_attempt_task_final_lease_recovers_after_crash(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 27, tzinfo=timezone.utc)
            store.claim_inbound("m1", 10001, now=now)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=now
            )
            store.e4_repository.transition_task(
                "t1", "queued", "running", "delivering", now=now
            )
            payload = ReliableDeliveryService._encode_observation(
                self._request(), "最终回复"
            )
            store.commit_task_final(
                task_id="t1", user_id=10001, message="最终回复",
                final_key="reply:m1", final_echo="task:t1:final",
                observation_message=payload,
                observation_key="observation:m1", now=now,
            )
            abandoned = store.claim_due_outbox(now=now, max_attempts=1)
            self.assertEqual(abandoned["kind"], "task_final")

            qq_messages = []
            observations = []

            async def sender(websocket, user_id, message, echo=None):
                qq_messages.append(message)

            async def observer(request, reply):
                observations.append((request.message_id, reply))

            current = now + timedelta(seconds=300)
            restarted = ReliableDeliveryService(
                store, sender, 0, 1, observation_handler=observer,
                now_provider=lambda: current, outbox_lease_seconds=300,
            )
            self.assertTrue(await restarted.deliver_once("ws"))

            self.assertEqual(qq_messages, ["最终回复"])
            self.assertEqual(observations, [("m1", "最终回复")])
            self.assertEqual(store.e4_repository.get_task("t1")["status"], "succeeded")
            self.assertFalse(
                store.claim_inbound("m1", 10001, now=current + timedelta(seconds=301))
            )

    async def test_expired_last_attempt_observation_lease_never_sends_json_to_qq(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 27, tzinfo=timezone.utc)
            payload = ReliableDeliveryService._encode_observation(
                self._request(), "最终回复"
            )
            store.enqueue_final_with_observation(
                kind="reply", user_id=10001, message="最终回复",
                dedupe_key="reply:m1", echo="reply:m1",
                observation_message=payload,
                observation_key="observation:m1", now=now,
            )
            final = store.claim_due_outbox(now=now, max_attempts=1)
            store.mark_outbox_sent(
                final["id"], final["claim_token"], now=now
            )
            abandoned = store.claim_due_outbox(now=now, max_attempts=1)
            self.assertEqual(abandoned["kind"], "e3_observation")

            qq_messages = []
            observations = []

            async def sender(websocket, user_id, message, echo=None):
                qq_messages.append(message)

            async def observer(request, reply):
                observations.append((request.message_id, reply))

            current = now + timedelta(seconds=300)
            restarted = ReliableDeliveryService(
                store, sender, 0, 1, observation_handler=observer,
                now_provider=lambda: current, outbox_lease_seconds=300,
            )
            self.assertTrue(await restarted.deliver_once("ws"))
            self.assertEqual(observations, [("m1", "最终回复")])
            self.assertEqual(qq_messages, [])

    async def test_expired_last_attempt_notice_lease_recovers_and_audits_send(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 27, tzinfo=timezone.utc)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=now
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=now
            )
            store.enqueue_task_notice(
                task_id="t1", user_id=10001, message="任务已接单",
                role="ack", now=now,
            )
            abandoned = store.claim_due_outbox(now=now, max_attempts=1)
            self.assertEqual(abandoned["kind"], "task_notice")
            sent = []

            async def sender(websocket, user_id, message, echo=None):
                sent.append(message)

            current = now + timedelta(seconds=300)
            restarted = ReliableDeliveryService(
                store, sender, 0, 1, now_provider=lambda: current,
                outbox_lease_seconds=300,
            )
            self.assertTrue(await restarted.deliver_once("ws"))
            self.assertEqual(sent, ["任务已接单"])
            events = store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["event_type"] for event in events].count("notification_sent"),
                1,
            )

    async def test_new_store_after_long_outage_recovers_last_attempt_reserved_leases(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        for kind in ("task_final", "e3_observation", "task_notice"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                claimed_at = datetime.now(tz=timezone.utc) - timedelta(seconds=301)
                payload = ReliableDeliveryService._encode_observation(
                    self._request(), "最终回复"
                )
                if kind == "task_final":
                    store.claim_inbound("m1", 10001, now=claimed_at)
                    task = store.e4_repository.create_task(
                        10001, "m1", "long_task", task_id="t1", now=claimed_at
                    )
                    store.e4_repository.transition_task(
                        "t1", "queued", "running", "delivering", now=claimed_at
                    )
                    store.commit_task_final(
                        task_id="t1", user_id=10001, message="最终回复",
                        final_key="reply:m1", final_echo="task:t1:final",
                        observation_message=payload,
                        observation_key="observation:m1", now=claimed_at,
                    )
                elif kind == "e3_observation":
                    store.enqueue_final_with_observation(
                        kind="reply", user_id=10001, message="最终回复",
                        dedupe_key="reply:m1", echo="reply:m1",
                        observation_message=payload,
                        observation_key="observation:m1", now=claimed_at,
                    )
                    final = store.claim_due_outbox(
                        now=claimed_at, max_attempts=1
                    )
                    store.mark_outbox_sent(
                        final["id"], final["claim_token"], now=claimed_at
                    )
                else:
                    task = store.e4_repository.create_task(
                        10001, "m1", "long_task", task_id="t1", now=claimed_at
                    )
                    store.e4_repository.transition_task(
                        "t1", "queued", "running", "running", now=claimed_at
                    )
                    store.enqueue_task_notice(
                        task_id="t1", user_id=10001, message="任务已接单",
                        role="ack", now=claimed_at,
                    )
                abandoned = store.claim_due_outbox(
                    now=claimed_at, max_attempts=1
                )
                self.assertEqual(abandoned["kind"], kind)

                sent = []
                observed = []

                async def sender(websocket, user_id, message, echo=None):
                    sent.append(message)

                async def observer(request, reply):
                    observed.append((request.message_id, reply))

                current = claimed_at + timedelta(seconds=301)
                restarted_store = WorkspaceStateStore(path)
                restarted = ReliableDeliveryService(
                    restarted_store, sender, 0, 1,
                    observation_handler=observer,
                    now_provider=lambda: current,
                    outbox_lease_seconds=300,
                )

                self.assertTrue(await restarted.deliver_once("ws"))
                key = {
                    "task_final": "reply:m1",
                    "e3_observation": "observation:m1",
                    "task_notice": "task:t1:notice:1",
                }[kind]
                self.assertEqual(restarted_store.get_outbox(key)["status"], "sent")
                if kind == "task_final":
                    self.assertEqual(sent, ["最终回复"])
                    self.assertEqual(observed, [("m1", "最终回复")])
                    self.assertEqual(
                        restarted_store.e4_repository.get_task("t1")["status"],
                        "succeeded",
                    )
                elif kind == "e3_observation":
                    self.assertEqual(sent, [])
                    self.assertEqual(observed, [("m1", "最终回复")])
                else:
                    self.assertEqual(sent, ["任务已接单"])
                    events = restarted_store.e4_repository.task_events("t1")
                    self.assertEqual(
                        [event["event_type"] for event in events].count(
                            "notification_sent"
                        ),
                        1,
                    )

    async def test_reserved_leases_preserve_fractional_expiry_boundary(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        claimed_at = datetime(
            2026, 7, 29, 0, 0, 0, 900000, tzinfo=timezone.utc
        )
        for kind in ("task_final", "e3_observation", "task_notice"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
                payload = ReliableDeliveryService._encode_observation(
                    self._request(), "最终回复"
                )
                if kind == "task_final":
                    store.claim_inbound("m1", 10001, now=claimed_at)
                    task = store.e4_repository.create_task(
                        10001, "m1", "long_task", task_id="t1", now=claimed_at
                    )
                    store.e4_repository.transition_task(
                        "t1", "queued", "running", "delivering", now=claimed_at
                    )
                    store.commit_task_final(
                        task_id="t1", user_id=10001, message="最终回复",
                        final_key="reply:m1", final_echo="task:t1:final",
                        observation_message=payload,
                        observation_key="observation:m1", now=claimed_at,
                    )
                elif kind == "e3_observation":
                    store.enqueue_final_with_observation(
                        kind="reply", user_id=10001, message="最终回复",
                        dedupe_key="reply:m1", echo="reply:m1",
                        observation_message=payload,
                        observation_key="observation:m1", now=claimed_at,
                    )
                    final = store.claim_due_outbox(
                        now=claimed_at, max_attempts=2
                    )
                    store.mark_outbox_sent(
                        final["id"], final["claim_token"], now=claimed_at
                    )
                else:
                    task = store.e4_repository.create_task(
                        10001, "m1", "long_task", task_id="t1", now=claimed_at
                    )
                    store.e4_repository.transition_task(
                        "t1", "queued", "running", "running", now=claimed_at
                    )
                    store.enqueue_task_notice(
                        task_id="t1", user_id=10001, message="任务已接单",
                        role="ack", now=claimed_at,
                    )
                abandoned = store.claim_due_outbox(
                    now=claimed_at, max_attempts=2, lease_seconds=300
                )
                self.assertEqual(abandoned["kind"], kind)

                before_expiry = claimed_at + timedelta(
                    seconds=300, microseconds=-1
                )
                self.assertIsNone(
                    store.claim_due_outbox(
                        now=before_expiry,
                        max_attempts=2,
                        lease_seconds=300,
                    )
                )
                still_leased = store.get_outbox(str(abandoned["dedupe_key"]))
                self.assertEqual(
                    (still_leased["status"], still_leased["attempts"]),
                    ("sending", 1),
                )

                recovered = store.claim_due_outbox(
                    now=claimed_at + timedelta(seconds=300),
                    max_attempts=2,
                    lease_seconds=300,
                )
                self.assertEqual(recovered["id"], abandoned["id"])
                self.assertEqual(recovered["attempts"], 2)

    async def test_repeated_last_attempt_expiry_is_bounded_for_reserved_kinds(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        claimed_at = datetime(2026, 7, 29, tzinfo=timezone.utc)
        for kind in ("task_final", "e3_observation", "task_notice"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                payload = ReliableDeliveryService._encode_observation(
                    self._request(), "最终回复"
                )
                if kind == "task_final":
                    store.claim_inbound("m1", 10001, now=claimed_at)
                    task = store.e4_repository.create_task(
                        10001, "m1", "long_task", task_id="t1", now=claimed_at
                    )
                    store.e4_repository.transition_task(
                        "t1", "queued", "running", "delivering", now=claimed_at
                    )
                    store.commit_task_final(
                        task_id="t1", user_id=10001, message="最终回复",
                        final_key="reply:m1", final_echo="task:t1:final",
                        observation_message=payload,
                        observation_key="observation:m1", now=claimed_at,
                    )
                elif kind == "e3_observation":
                    store.enqueue_final_with_observation(
                        kind="reply", user_id=10001, message="最终回复",
                        dedupe_key="reply:m1", echo="reply:m1",
                        observation_message=payload,
                        observation_key="observation:m1", now=claimed_at,
                    )
                    final = store.claim_due_outbox(
                        now=claimed_at, max_attempts=1
                    )
                    store.mark_outbox_sent(
                        final["id"], final["claim_token"], now=claimed_at
                    )
                else:
                    task = store.e4_repository.create_task(
                        10001, "m1", "long_task", task_id="t1", now=claimed_at
                    )
                    store.e4_repository.transition_task(
                        "t1", "queued", "running", "running", now=claimed_at
                    )
                    store.enqueue_task_notice(
                        task_id="t1", user_id=10001, message="任务已接单",
                        role="ack", now=claimed_at,
                    )
                first = store.claim_due_outbox(
                    now=claimed_at, max_attempts=1, lease_seconds=300
                )
                self.assertEqual(first["kind"], kind)

                grace_at = claimed_at + timedelta(seconds=300)
                grace = WorkspaceStateStore(path).claim_due_outbox(
                    now=grace_at, max_attempts=1, lease_seconds=300
                )
                self.assertEqual(grace["id"], first["id"])
                self.assertEqual(grace["attempts"], 1)

                # The external QQ/observer side effect may have happened here;
                # the process crashes before SQLite can record completion.
                external_effects = [kind]
                terminal_at = grace_at + timedelta(seconds=300)
                terminal_store = WorkspaceStateStore(path)
                self.assertIsNone(
                    terminal_store.claim_due_outbox(
                        now=terminal_at,
                        max_attempts=1,
                        lease_seconds=300,
                    )
                )
                self.assertEqual(external_effects, [kind])
                key = {
                    "task_final": "reply:m1",
                    "e3_observation": "observation:m1",
                    "task_notice": "task:t1:notice:1",
                }[kind]
                row = terminal_store.get_outbox(key)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["last_error"], "delivery lease exhausted")
                with terminal_store._connect() as connection:
                    backlog = connection.execute(
                        """
                        SELECT COUNT(*) FROM outbox
                        WHERE status IN ('pending', 'sending')
                        """
                    ).fetchone()[0]
                self.assertEqual(backlog, 0)
                if kind == "task_final":
                    self.assertEqual(
                        terminal_store.get_outbox("observation:m1")["status"],
                        "failed",
                    )
                    task = terminal_store.e4_repository.get_task("t1")
                    self.assertEqual(
                        (task["status"], task["stage"]),
                        ("failed", "delivery_failed"),
                    )
                    self.assertFalse(
                        terminal_store.claim_inbound(
                            "m1", 10001, now=terminal_at + timedelta(seconds=301)
                        )
                    )
                elif kind == "task_notice":
                    events = terminal_store.e4_repository.task_events("t1")
                    self.assertEqual(
                        [event["event_type"] for event in events].count(
                            "notification_failed"
                        ),
                        1,
                    )

    def _prepare_reserved_lease_case(self, store, kind, claimed_at):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService

        payload = ReliableDeliveryService._encode_observation(
            self._request(), "最终回复"
        )
        if kind == "task_final":
            store.claim_inbound("m1", 10001, now=claimed_at)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=claimed_at
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "delivering", now=claimed_at
            )
            store.commit_task_final(
                task_id="t1", user_id=10001, message="最终回复",
                final_key="reply:m1", final_echo="task:t1:final",
                observation_message=payload,
                observation_key="observation:m1", now=claimed_at,
            )
            return "reply:m1"
        if kind == "e3_observation":
            store.enqueue_final_with_observation(
                kind="reply", user_id=10001, message="最终回复",
                dedupe_key="reply:m1", echo="reply:m1",
                observation_message=payload,
                observation_key="observation:m1", now=claimed_at,
            )
            final = store.claim_due_outbox(now=claimed_at, max_attempts=3)
            store.mark_outbox_sent(
                final["id"], final["claim_token"], now=claimed_at
            )
            return "observation:m1"
        task = store.e4_repository.create_task(
            10001, "m1", "long_task", task_id="t1", now=claimed_at
        )
        store.e4_repository.transition_task(
            task["id"], "queued", "running", "running", now=claimed_at
        )
        store.enqueue_task_notice(
            task_id="t1", user_id=10001, message="任务已接单",
            role="ack", now=claimed_at,
        )
        return "task:t1:notice:1"

    def _assert_reserved_lease_terminal_state(self, store, kind, key):
        row = store.get_outbox(key)
        self.assertEqual(
            (row["status"], row["last_error"]),
            ("failed", "delivery lease exhausted"),
        )
        with store._connect() as connection:
            backlog = connection.execute(
                """
                SELECT COUNT(*) FROM outbox
                WHERE status IN ('pending', 'sending')
                """
            ).fetchone()[0]
        self.assertEqual(backlog, 0)
        if kind == "task_final":
            self.assertEqual(
                store.get_outbox("observation:m1")["status"], "failed"
            )
            task = store.e4_repository.get_task("t1")
            self.assertEqual(
                (task["status"], task["stage"]),
                ("failed", "delivery_failed"),
            )
        elif kind == "task_notice":
            events = store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["event_type"] for event in events].count(
                    "notification_failed"
                ),
                1,
            )

    def _prepare_delivery_race_case(self, store, kind, claimed_at):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService

        if kind in ("task_final", "task_notice", "e3_observation"):
            return self._prepare_reserved_lease_case(store, kind, claimed_at)
        if kind == "reply":
            store.claim_inbound("m1", 10001, now=claimed_at)
            payload = ReliableDeliveryService._encode_observation(
                self._request(), "最终回复"
            )
            store.enqueue_final_with_observation(
                kind="reply", user_id=10001, message="最终回复",
                dedupe_key="reply:m1", echo="reply:m1",
                observation_message=payload,
                observation_key="observation:m1", now=claimed_at,
            )
            return "reply:m1"
        store.enqueue_outbox(
            kind="command", user_id=10001, message="generic",
            dedupe_key="command:c1", echo="command:c1", now=claimed_at,
        )
        return "command:c1"

    @staticmethod
    def _delivery_state_snapshot(store):
        with store._connect() as connection:
            return {
                "outbox": connection.execute(
                    """
                    SELECT id, kind, user_id, dedupe_key, status, attempts,
                           next_attempt_at, updated_at, sent_at, last_error
                    FROM outbox ORDER BY id
                    """
                ).fetchall(),
                "inbound": connection.execute(
                    """
                    SELECT message_id, user_id, status, attempts, updated_at,
                           last_error
                    FROM inbound_messages ORDER BY message_id
                    """
                ).fetchall(),
                "tasks": connection.execute(
                    """
                    SELECT id, user_id, source_message_id, status, stage,
                           finished_at, last_notified_at
                    FROM long_tasks ORDER BY id
                    """
                ).fetchall(),
                "events": connection.execute(
                    """
                    SELECT task_id, event_type, stage, detail, created_at
                    FROM task_events ORDER BY id
                    """
                ).fetchall(),
                "metrics": connection.execute(
                    """
                    SELECT name, value, updated_at FROM runtime_metrics
                    ORDER BY name
                    """
                ).fetchall(),
            }

    async def _assert_stale_callback_is_noop(self, kind, outcome, terminal):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            claimed_at = datetime(2026, 7, 30, tzinfo=timezone.utc)
            current = claimed_at
            store = WorkspaceStateStore(path)
            key = self._prepare_delivery_race_case(
                store, kind, claimed_at
            )
            entered = asyncio.Event()
            release = asyncio.Event()
            qq = []
            observed = []

            async def wait_for_callback():
                entered.set()
                await release.wait()
                if outcome == "retry":
                    raise RuntimeError("controlled sender failure")

            async def sender(websocket, user_id, message, echo=None):
                qq.append((user_id, message, echo))
                await wait_for_callback()

            async def observer(request, reply):
                observed.append((request.message_id, reply))
                if kind == "e3_observation":
                    await wait_for_callback()

            delivery = ReliableDeliveryService(
                store, sender, 0, 2,
                observation_handler=observer,
                now_provider=lambda: current,
                outbox_lease_seconds=300,
            )
            worker = asyncio.create_task(delivery.deliver_once("ws"))
            await asyncio.wait_for(entered.wait(), timeout=1)

            grace_at = claimed_at + timedelta(seconds=300)
            current = grace_at
            grace_store = WorkspaceStateStore(path)
            grace = grace_store.claim_due_outbox(
                now=grace_at, max_attempts=1, lease_seconds=300
            )
            self.assertIsNotNone(grace)
            self.assertEqual(grace["dedupe_key"], key)
            self.assertEqual(
                grace_store.get_outbox(key)["last_error"],
                "delivery lease grace used",
            )

            callback_store = grace_store
            if terminal:
                current = grace_at + timedelta(seconds=300)
                callback_store = WorkspaceStateStore(path)
                self.assertIsNone(
                    callback_store.claim_due_outbox(
                        now=current, max_attempts=1, lease_seconds=300
                    )
                )
                self.assertEqual(
                    callback_store.get_outbox(key)["status"], "failed"
                )
            before = self._delivery_state_snapshot(callback_store)

            release.set()
            result = await asyncio.wait_for(worker, timeout=1)

            self.assertFalse(result)
            after = self._delivery_state_snapshot(
                WorkspaceStateStore(path)
            )
            self.assertEqual(after, before)
            if kind == "e3_observation":
                self.assertEqual(qq, [])
                self.assertEqual(observed, [("m1", "最终回复")])
            else:
                self.assertEqual(len(qq), 1)
                self.assertEqual(observed, [])

    async def test_stale_sent_and_retry_callbacks_cannot_ack_reclaimed_lease(self):
        for outcome in ("sent", "retry"):
            for kind in (
                "reply", "command", "task_final", "task_notice",
                "e3_observation",
            ):
                with self.subTest(outcome=outcome, kind=kind):
                    await self._assert_stale_callback_is_noop(
                        kind, outcome, terminal=False
                    )

    async def test_stale_sent_and_retry_callbacks_cannot_reopen_terminal_lease(self):
        for outcome in ("sent", "retry"):
            for kind in (
                "reply", "command", "task_final", "task_notice",
                "e3_observation",
            ):
                with self.subTest(outcome=outcome, kind=kind):
                    await self._assert_stale_callback_is_noop(
                        kind, outcome, terminal=True
                    )

    def test_active_grace_retry_failure_terminalizes_after_limit_is_raised(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        claimed_at = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for kind in (
            "reply", "command", "task_final", "task_notice",
            "e3_observation",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                key = self._prepare_delivery_race_case(store, kind, claimed_at)
                store.claim_due_outbox(
                    now=claimed_at, max_attempts=1, lease_seconds=300
                )

                grace_at = claimed_at + timedelta(seconds=300)
                grace_store = WorkspaceStateStore(path)
                grace = grace_store.claim_due_outbox(
                    now=grace_at, max_attempts=1, lease_seconds=300
                )
                self.assertEqual(grace["dedupe_key"], key)
                self.assertTrue(
                    grace_store.mark_outbox_retry(
                        grace["id"],
                        grace["claim_token"],
                        "sender failed during grace",
                        retry_base_seconds=0,
                        max_attempts=8,
                        now=grace_at,
                    )
                )

                row = grace_store.get_outbox(key)
                self.assertEqual(
                    (row["status"], row["last_error"]),
                    ("failed", "delivery lease exhausted"),
                )
                self.assertIsNone(
                    WorkspaceStateStore(path).claim_due_outbox(
                        now=grace_at + timedelta(seconds=300),
                        max_attempts=8,
                        lease_seconds=300,
                    )
                )

    def test_first_lease_expiry_consumes_only_replay_grace_for_all_kinds(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        claimed_at = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for kind in (
            "reply", "command", "task_final", "task_notice",
            "e3_observation",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                key = self._prepare_delivery_race_case(store, kind, claimed_at)
                original = store.claim_due_outbox(
                    now=claimed_at, max_attempts=5, lease_seconds=300
                )
                self.assertEqual(original["attempts"], 1)

                replay_at = claimed_at + timedelta(seconds=300)
                replay_store = WorkspaceStateStore(path)
                replay = replay_store.claim_due_outbox(
                    now=replay_at, max_attempts=5, lease_seconds=300
                )
                self.assertEqual(replay["id"], original["id"])
                self.assertEqual(replay["attempts"], 2)
                self.assertEqual(
                    replay_store.get_outbox(key)["last_error"],
                    "delivery lease grace used",
                )

                terminal_store = WorkspaceStateStore(path)
                self.assertIsNone(
                    terminal_store.claim_due_outbox(
                        now=replay_at + timedelta(seconds=300),
                        max_attempts=8,
                        lease_seconds=300,
                    )
                )
                row = terminal_store.get_outbox(key)
                self.assertEqual(
                    (row["status"], row["last_error"]),
                    ("failed", "delivery lease exhausted"),
                )
                with terminal_store._connect() as connection:
                    backlog = connection.execute(
                        """
                        SELECT COUNT(*) FROM outbox
                        WHERE status IN ('pending', 'sending')
                        """
                    ).fetchone()[0]
                self.assertEqual(backlog, 0)
                if kind == "reply":
                    self.assertEqual(
                        terminal_store.get_outbox("observation:m1")["status"],
                        "failed",
                    )
                elif kind in ("task_final", "task_notice", "e3_observation"):
                    self._assert_reserved_lease_terminal_state(
                        terminal_store, kind, key
                    )

    def test_consumed_lease_grace_survives_raised_limit_across_restarts(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        claimed_at = datetime(2026, 7, 29, tzinfo=timezone.utc)
        for kind in ("task_final", "task_notice", "e3_observation"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                key = self._prepare_reserved_lease_case(store, kind, claimed_at)
                first = store.claim_due_outbox(
                    now=claimed_at, max_attempts=1, lease_seconds=300
                )
                self.assertEqual(first["kind"], kind)

                grace_at = claimed_at + timedelta(seconds=300)
                grace_store = WorkspaceStateStore(path)
                grace = grace_store.claim_due_outbox(
                    now=grace_at, max_attempts=1, lease_seconds=300
                )
                self.assertEqual(grace["id"], first["id"])
                self.assertEqual(
                    grace_store.get_outbox(key)["last_error"],
                    "delivery lease grace used",
                )

                before_expiry = grace_at + timedelta(
                    seconds=300, microseconds=-1
                )
                before_store = WorkspaceStateStore(path)
                self.assertIsNone(
                    before_store.claim_due_outbox(
                        now=before_expiry,
                        max_attempts=2,
                        lease_seconds=300,
                    )
                )
                self.assertEqual(
                    before_store.get_outbox(key)["last_error"],
                    "delivery lease grace used",
                )

                terminal_store = WorkspaceStateStore(path)
                self.assertIsNone(
                    terminal_store.claim_due_outbox(
                        now=grace_at + timedelta(seconds=300),
                        max_attempts=2,
                        lease_seconds=300,
                    )
                )
                self._assert_reserved_lease_terminal_state(
                    terminal_store, kind, key
                )

    def test_consumed_lease_grace_survives_lowered_limit_across_restarts(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        claimed_at = datetime(2026, 7, 29, tzinfo=timezone.utc)
        for kind in ("task_final", "task_notice", "e3_observation"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                key = self._prepare_reserved_lease_case(store, kind, claimed_at)
                first = store.claim_due_outbox(
                    now=claimed_at, max_attempts=2, lease_seconds=300
                )
                self.assertEqual(first["kind"], kind)
                second_at = claimed_at + timedelta(seconds=300)
                second = WorkspaceStateStore(path).claim_due_outbox(
                    now=second_at, max_attempts=2, lease_seconds=300
                )
                self.assertEqual(second["attempts"], 2)
                self.assertEqual(
                    WorkspaceStateStore(path).get_outbox(key)["last_error"],
                    "delivery lease grace used",
                )

                terminal_at = second_at + timedelta(seconds=300)
                terminal_store = WorkspaceStateStore(path)
                self.assertIsNone(
                    terminal_store.claim_due_outbox(
                        now=terminal_at,
                        max_attempts=1,
                        lease_seconds=300,
                    )
                )
                self._assert_reserved_lease_terminal_state(
                    terminal_store, kind, key
                )

    def test_pre_grace_limit_decrease_keeps_one_claimable_bounded_grace(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        claimed_at = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for kind in (
            "reply", "command", "task_final", "task_notice",
            "e3_observation",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                key = self._prepare_delivery_race_case(
                    store, kind, claimed_at
                )
                first = store.claim_due_outbox(
                    now=claimed_at, max_attempts=5, lease_seconds=300
                )
                self.assertEqual(first["attempts"], 1)
                self.assertTrue(
                    store.mark_outbox_retry(
                        first["id"], first["claim_token"], "sender failed",
                        retry_base_seconds=0, max_attempts=5, now=claimed_at,
                    )
                )
                self.assertNotEqual(
                    store.get_outbox(key)["last_error"],
                    "delivery lease grace used",
                )

                second_at = claimed_at + timedelta(seconds=1)
                second_store = WorkspaceStateStore(path)
                second = second_store.claim_due_outbox(
                    now=second_at, max_attempts=5, lease_seconds=300
                )
                self.assertEqual(second["attempts"], 2)
                self.assertTrue(
                    second_store.mark_outbox_retry(
                        second["id"], second["claim_token"], "sender failed",
                        retry_base_seconds=0, max_attempts=5, now=second_at,
                    )
                )
                self.assertNotEqual(
                    second_store.get_outbox(key)["last_error"],
                    "delivery lease grace used",
                )
                third_at = second_at + timedelta(seconds=1)
                third = WorkspaceStateStore(path).claim_due_outbox(
                    now=third_at, max_attempts=5, lease_seconds=300
                )
                self.assertEqual(third["attempts"], 3)

                grace_at = third_at + timedelta(seconds=300)
                grace_store = WorkspaceStateStore(path)
                grace = grace_store.claim_due_outbox(
                    now=grace_at, max_attempts=1, lease_seconds=300
                )
                self.assertIsNotNone(grace)
                self.assertEqual(grace["id"], first["id"])
                self.assertEqual(grace["attempts"], 1)
                self.assertEqual(
                    grace_store.get_outbox(key)["last_error"],
                    "delivery lease grace used",
                )

                terminal_at = grace_at + timedelta(seconds=300)
                terminal_store = WorkspaceStateStore(path)
                self.assertIsNone(
                    terminal_store.claim_due_outbox(
                        now=terminal_at, max_attempts=5, lease_seconds=300
                    )
                )
                row = terminal_store.get_outbox(key)
                self.assertEqual(
                    (row["status"], row["last_error"]),
                    ("failed", "delivery lease exhausted"),
                )
                with terminal_store._connect() as connection:
                    backlog = connection.execute(
                        """
                        SELECT COUNT(*) FROM outbox
                        WHERE status IN ('pending', 'sending')
                        """
                    ).fetchone()[0]
                self.assertEqual(backlog, 0)
                if kind == "reply":
                    self.assertEqual(
                        terminal_store.get_outbox("observation:m1")["status"],
                        "failed",
                    )
                    with terminal_store._connect() as connection:
                        inbound = connection.execute(
                            """
                            SELECT status FROM inbound_messages
                            WHERE message_id='m1' AND user_id=10001
                            """
                        ).fetchone()[0]
                    self.assertEqual(inbound, "completed")
                elif kind in (
                    "task_final", "task_notice", "e3_observation"
                ):
                    self._assert_reserved_lease_terminal_state(
                        terminal_store, kind, key
                    )

    async def test_repeated_normal_final_lease_recovery_terminalizes_pair(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            claimed_at = datetime(2026, 7, 29, tzinfo=timezone.utc)
            store = WorkspaceStateStore(path)
            store.claim_inbound("m1", 10001, now=claimed_at)
            payload = ReliableDeliveryService._encode_observation(
                self._request(), "最终回复"
            )
            store.enqueue_final_with_observation(
                kind="reply", user_id=10001, message="最终回复",
                dedupe_key="reply:m1", echo="reply:m1",
                observation_message=payload,
                observation_key="observation:m1", now=claimed_at,
            )
            first = store.claim_due_outbox(
                now=claimed_at, max_attempts=1, lease_seconds=300
            )
            self.assertEqual(first["kind"], "reply")

            grace_at = claimed_at + timedelta(seconds=300)
            grace_store = WorkspaceStateStore(path)
            grace = grace_store.claim_due_outbox(
                now=grace_at, max_attempts=1, lease_seconds=300
            )
            self.assertEqual(grace["id"], first["id"])
            self.assertEqual(
                grace_store.get_outbox("reply:m1")["last_error"],
                "delivery lease grace used",
            )

            terminal_at = grace_at + timedelta(seconds=300)
            terminal_store = WorkspaceStateStore(path)
            self.assertIsNone(
                terminal_store.claim_due_outbox(
                    now=terminal_at, max_attempts=1, lease_seconds=300
                )
            )
            reply = terminal_store.get_outbox("reply:m1")
            observation = terminal_store.get_outbox("observation:m1")
            self.assertEqual(
                (reply["status"], reply["last_error"]),
                ("failed", "delivery lease exhausted"),
            )
            self.assertEqual(
                (observation["status"], observation["last_error"]),
                ("failed", "dependency final failed"),
            )
            with terminal_store._connect() as connection:
                inbound = connection.execute(
                    """
                    SELECT user_id, status, last_error FROM inbound_messages
                    WHERE message_id='m1'
                    """
                ).fetchone()
                backlog = connection.execute(
                    """
                    SELECT COUNT(*) FROM outbox
                    WHERE status IN ('pending', 'sending')
                    """
                ).fetchone()[0]
            self.assertEqual(
                inbound, (10001, "completed", "final delivery failed")
            )
            self.assertEqual(backlog, 0)

            qq = []
            observed = []

            async def sender(*args, **kwargs):
                qq.append(args)

            async def observer(*args, **kwargs):
                observed.append(args)

            delivery = ReliableDeliveryService(
                terminal_store, sender, 0, 1,
                observation_handler=observer,
                now_provider=lambda: terminal_at + timedelta(seconds=301),
                outbox_lease_seconds=300,
            )
            self.assertFalse(await delivery.deliver_once("ws"))
            self.assertEqual(qq, [])
            self.assertEqual(observed, [])

    async def test_repeated_generic_lease_recovery_terminalizes_without_send(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            claimed_at = datetime(2026, 7, 29, tzinfo=timezone.utc)
            store = WorkspaceStateStore(path)
            store.enqueue_outbox(
                kind="command", user_id=10001, message="generic",
                dedupe_key="command:c1", echo="command:c1", now=claimed_at,
            )
            first = store.claim_due_outbox(
                now=claimed_at, max_attempts=1, lease_seconds=300
            )
            self.assertEqual(first["kind"], "command")

            grace_at = claimed_at + timedelta(seconds=300)
            grace_store = WorkspaceStateStore(path)
            grace = grace_store.claim_due_outbox(
                now=grace_at, max_attempts=1, lease_seconds=300
            )
            self.assertEqual(grace["id"], first["id"])
            self.assertEqual(
                grace_store.get_outbox("command:c1")["last_error"],
                "delivery lease grace used",
            )

            terminal_at = grace_at + timedelta(seconds=300)
            terminal_store = WorkspaceStateStore(path)
            self.assertIsNone(
                terminal_store.claim_due_outbox(
                    now=terminal_at, max_attempts=1, lease_seconds=300
                )
            )
            row = terminal_store.get_outbox("command:c1")
            self.assertEqual(
                (row["status"], row["last_error"]),
                ("failed", "delivery lease exhausted"),
            )
            with terminal_store._connect() as connection:
                backlog = connection.execute(
                    """
                    SELECT COUNT(*) FROM outbox
                    WHERE status IN ('pending', 'sending')
                    """
                ).fetchone()[0]
            self.assertEqual(backlog, 0)

            qq = []
            observed = []

            async def sender(*args, **kwargs):
                qq.append(args)

            async def observer(*args, **kwargs):
                observed.append(args)

            delivery = ReliableDeliveryService(
                terminal_store, sender, 0, 1,
                observation_handler=observer,
                now_provider=lambda: terminal_at + timedelta(seconds=301),
                outbox_lease_seconds=300,
            )
            self.assertFalse(await delivery.deliver_once("ws"))
            self.assertEqual(qq, [])
            self.assertEqual(observed, [])

    async def test_progress_slot_reuses_sent_row_for_new_stage_across_restart(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            start = datetime(2026, 7, 29, tzinfo=timezone.utc)
            current = start
            store = WorkspaceStateStore(path)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=current
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=current
            )
            sends = []

            async def sender(websocket, user_id, message, echo=None):
                sends.append((message, current))

            delivery = ReliableDeliveryService(
                store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertTrue(
                await delivery.send_task_notice(
                    "ws", "t1", 10001, "ack", role="ack"
                )
            )

            current = start + timedelta(seconds=60)
            restarted_store = WorkspaceStateStore(path)
            restarted = ReliableDeliveryService(
                restarted_store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertTrue(
                await restarted.send_task_notice(
                    "ws", "t1", 10001, "progress-1", role="progress"
                )
            )
            self.assertTrue(
                await restarted.send_task_notice(
                    "ws", "t1", 10001, "progress-1", role="progress"
                )
            )

            current = start + timedelta(seconds=120)
            final_store = WorkspaceStateStore(path)
            final_delivery = ReliableDeliveryService(
                final_store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertTrue(
                await final_delivery.send_task_notice(
                    "ws", "t1", 10001, "progress-2", role="progress"
                )
            )

            self.assertEqual(
                [message for message, _ in sends],
                ["ack", "progress-1", "progress-2"],
            )
            self.assertTrue(
                all(
                    later[1] - earlier[1] >= timedelta(seconds=60)
                    for earlier, later in zip(sends, sends[1:])
                )
            )
            events = final_store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["event_type"] for event in events].count(
                    "notification_sent"
                ),
                3,
            )
            with final_store._connect() as connection:
                notice_rows = connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE kind='task_notice'"
                ).fetchone()[0]
            self.assertEqual(notice_rows, 2)

    async def test_fractional_ack_to_progress_restart_waits_for_exact_interval(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            start = datetime(
                2026, 7, 29, 0, 0, 0, 900000, tzinfo=timezone.utc
            )
            current = start
            store = WorkspaceStateStore(path)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=current
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=current
            )
            sends = []

            async def sender(websocket, user_id, message, echo=None):
                sends.append((message, current))

            delivery = ReliableDeliveryService(
                store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertTrue(
                await delivery.send_task_notice(
                    "ws", "t1", 10001, "ack", role="ack"
                )
            )

            current = start + timedelta(seconds=60, microseconds=-1)
            before_store = WorkspaceStateStore(path)
            before = ReliableDeliveryService(
                before_store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertFalse(
                await before.send_task_notice(
                    "ws", "t1", 10001, "progress-1", role="progress"
                )
            )
            self.assertEqual([message for message, _ in sends], ["ack"])
            self.assertEqual(
                before_store.get_outbox("task:t1:notice:progress")["status"],
                "pending",
            )

            current = start + timedelta(seconds=60)
            exact_store = WorkspaceStateStore(path)
            exact = ReliableDeliveryService(
                exact_store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertTrue(await exact.deliver_once("ws"))
            self.assertEqual(
                sends, [("ack", start), ("progress-1", current)]
            )
            events = exact_store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["event_type"] for event in events].count(
                    "notification_sent"
                ),
                2,
            )
            with exact_store._connect() as connection:
                notices = connection.execute(
                    """
                    SELECT dedupe_key, sent_at, updated_at FROM outbox
                    WHERE kind='task_notice' ORDER BY id
                    """
                ).fetchall()
                notified_at = connection.execute(
                    "SELECT last_notified_at FROM long_tasks WHERE id='t1'"
                ).fetchone()[0]
            self.assertEqual(len(notices), 2)
            self.assertEqual(
                notices[0][1:],
                (
                    start.isoformat(timespec="microseconds"),
                    start.isoformat(timespec="microseconds"),
                ),
            )
            self.assertEqual(
                notices[1][1:],
                (
                    current.isoformat(timespec="microseconds"),
                    current.isoformat(timespec="microseconds"),
                ),
            )
            self.assertEqual(
                notified_at, current.isoformat(timespec="microseconds")
            )

    async def test_fractional_progress_restart_waits_for_exact_interval(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            start = datetime(
                2026, 7, 29, 0, 0, 0, 900000, tzinfo=timezone.utc
            )
            current = start
            store = WorkspaceStateStore(path)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=current
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=current
            )
            sends = []

            async def sender(websocket, user_id, message, echo=None):
                sends.append((message, current))

            delivery = ReliableDeliveryService(
                store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertTrue(
                await delivery.send_task_notice(
                    "ws", "t1", 10001, "ack", role="ack"
                )
            )
            current = start + timedelta(seconds=60)
            progress_store = WorkspaceStateStore(path)
            progress = ReliableDeliveryService(
                progress_store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertTrue(
                await progress.send_task_notice(
                    "ws", "t1", 10001, "progress-1", role="progress"
                )
            )

            current = start + timedelta(seconds=120, microseconds=-1)
            before_store = WorkspaceStateStore(path)
            before = ReliableDeliveryService(
                before_store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertFalse(
                await before.send_task_notice(
                    "ws", "t1", 10001, "progress-2", role="progress"
                )
            )
            self.assertEqual(
                [message for message, _ in sends], ["ack", "progress-1"]
            )

            current = start + timedelta(seconds=120)
            exact_store = WorkspaceStateStore(path)
            exact = ReliableDeliveryService(
                exact_store, sender, 0, 3, now_provider=lambda: current,
                task_notice_interval_seconds=60,
            )
            self.assertTrue(await exact.deliver_once("ws"))
            self.assertEqual(
                sends,
                [
                    ("ack", start),
                    ("progress-1", start + timedelta(seconds=60)),
                    ("progress-2", current),
                ],
            )
            events = exact_store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["event_type"] for event in events].count(
                    "notification_sent"
                ),
                3,
            )
            with exact_store._connect() as connection:
                notice_rows = connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE kind='task_notice'"
                ).fetchone()[0]
                notified_at = connection.execute(
                    "SELECT last_notified_at FROM long_tasks WHERE id='t1'"
                ).fetchone()[0]
            self.assertEqual(notice_rows, 2)
            self.assertEqual(
                notified_at, current.isoformat(timespec="microseconds")
            )

    def test_unreachable_pending_observations_are_terminalized_before_claim(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        cases = ("missing", "malformed", "wrong_kind", "failed")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
                now = datetime(2026, 7, 29, tzinfo=timezone.utc).isoformat()
                observation_key = (
                    "malformed-observation"
                    if case == "malformed"
                    else "observation:m1"
                )
                with store._connect() as connection:
                    if case in ("wrong_kind", "failed"):
                        connection.execute(
                            """
                            INSERT INTO outbox(
                              kind, user_id, message, echo, dedupe_key, status,
                              attempts, next_attempt_at, created_at, updated_at
                            ) VALUES (?, 10001, 'dependency', 'reply:m1',
                                      'reply:m1', ?, 0, ?, ?, ?)
                            """,
                            (
                                "command" if case == "wrong_kind" else "reply",
                                "sent" if case == "wrong_kind" else "failed",
                                now,
                                now,
                                now,
                            ),
                        )
                    connection.execute(
                        """
                        INSERT INTO outbox(
                          kind, user_id, message, echo, dedupe_key, status,
                          attempts, next_attempt_at, created_at, updated_at
                        ) VALUES ('e3_observation', 10001, '{}', 'reply:m1', ?,
                                  'pending', 0, ?, ?, ?)
                        """,
                        (observation_key, now, now, now),
                    )

                self.assertIsNone(store.claim_due_outbox())
                observation = store.get_outbox(observation_key)
                self.assertEqual(observation["status"], "failed")
                self.assertEqual(
                    observation["last_error"], "invalid reserved outbox row"
                )
                with store._connect() as connection:
                    backlog = connection.execute(
                        """
                        SELECT COUNT(*) FROM outbox
                        WHERE kind='e3_observation'
                          AND status IN ('pending', 'sending')
                        """
                    ).fetchone()[0]
                self.assertEqual(backlog, 0)

    async def test_task_final_single_ownership_field_corruption_closes_owner(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        cases = ("echo", "key", "user")
        for field in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                now = datetime(2026, 7, 29, tzinfo=timezone.utc)
                store = WorkspaceStateStore(path)
                store.claim_inbound("m1", 10001, now=now)
                task = store.e4_repository.create_task(
                    10001, "m1", "long_task", task_id="t1", now=now
                )
                store.e4_repository.transition_task(
                    task["id"], "queued", "running", "delivering", now=now
                )
                payload = ReliableDeliveryService._encode_observation(
                    self._request(), "最终回复"
                )
                store.commit_task_final(
                    task_id="t1", user_id=10001, message="最终回复",
                    final_key="reply:m1", final_echo="task:t1:final",
                    observation_message=payload,
                    observation_key="observation:m1", now=now,
                )
                final_id = store.get_outbox("reply:m1")["id"]
                with store._connect() as connection:
                    if field == "echo":
                        connection.execute(
                            "UPDATE outbox SET echo='broken-final-echo' WHERE id=?",
                            (final_id,),
                        )
                    elif field == "key":
                        connection.execute(
                            "UPDATE outbox SET dedupe_key='broken-final-key' WHERE id=?",
                            (final_id,),
                        )
                    else:
                        connection.execute(
                            "UPDATE outbox SET user_id=30003 WHERE id=?",
                            (final_id,),
                        )

                qq = []
                observed = []

                async def sender(*args, **kwargs):
                    qq.append(args)

                async def observer(*args, **kwargs):
                    observed.append(args)

                delivery = ReliableDeliveryService(
                    WorkspaceStateStore(path), sender, 0, 3,
                    observation_handler=observer,
                    now_provider=lambda: now,
                )
                self.assertFalse(await delivery.deliver_once("ws"))

                checked = WorkspaceStateStore(path)
                with checked._connect() as connection:
                    final = connection.execute(
                        "SELECT status, last_error FROM outbox WHERE id=?",
                        (final_id,),
                    ).fetchone()
                    inbound = connection.execute(
                        """
                        SELECT user_id, status, last_error FROM inbound_messages
                        WHERE message_id='m1'
                        """
                    ).fetchone()
                    backlog = connection.execute(
                        """
                        SELECT COUNT(*) FROM outbox
                        WHERE status IN ('pending', 'sending')
                        """
                    ).fetchone()[0]
                    sent_count = connection.execute(
                        "SELECT COUNT(*) FROM outbox WHERE status='sent'"
                    ).fetchone()[0]
                self.assertEqual(
                    final, ("failed", "invalid reserved outbox row")
                )
                self.assertEqual(
                    checked.get_outbox("observation:m1")["status"], "failed"
                )
                owner = checked.e4_repository.get_task("t1")
                self.assertEqual(
                    (owner["status"], owner["stage"]),
                    ("failed", "delivery_corrupt"),
                )
                self.assertEqual(
                    inbound,
                    (10001, "completed", "final delivery corrupted"),
                )
                self.assertEqual(backlog, 0)
                self.assertEqual(sent_count, 0)
                self.assertEqual(qq, [])
                self.assertEqual(observed, [])
                events = checked.e4_repository.task_events("t1")
                self.assertEqual(
                    [event["stage"] for event in events].count(
                        "delivery_corrupt"
                    ),
                    1,
                )

    async def test_conflicting_task_final_ownership_is_quarantined_only(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        for field in ("echo", "user"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                now = datetime(2026, 7, 29, tzinfo=timezone.utc)
                store = WorkspaceStateStore(path)
                store.claim_inbound("m1", 10001, now=now)
                store.claim_inbound("m2", 20002, now=now)
                owner = store.e4_repository.create_task(
                    10001, "m1", "long_task", task_id="t1", now=now
                )
                other = store.e4_repository.create_task(
                    20002, "m2", "long_task", task_id="t2", now=now
                )
                store.e4_repository.transition_task(
                    owner["id"], "queued", "running", "delivering", now=now
                )
                store.e4_repository.transition_task(
                    other["id"], "queued", "running", "delivery_committed",
                    now=now,
                )
                payload = ReliableDeliveryService._encode_observation(
                    self._request(), "最终回复"
                )
                store.commit_task_final(
                    task_id="t1", user_id=10001, message="最终回复",
                    final_key="reply:m1", final_echo="task:t1:final",
                    observation_message=payload,
                    observation_key="observation:m1", now=now,
                )
                final_id = store.get_outbox("reply:m1")["id"]
                original_events = {
                    task_id: list(store.e4_repository.task_events(task_id))
                    for task_id in ("t1", "t2")
                }
                with store._connect() as connection:
                    if field == "echo":
                        connection.execute(
                            "UPDATE outbox SET echo='task:t2:final' WHERE id=?",
                            (final_id,),
                        )
                    else:
                        connection.execute(
                            "UPDATE outbox SET user_id=20002 WHERE id=?",
                            (final_id,),
                        )

                qq = []
                observed = []

                async def sender(*args, **kwargs):
                    qq.append(args)

                async def observer(*args, **kwargs):
                    observed.append(args)

                delivery = ReliableDeliveryService(
                    WorkspaceStateStore(path), sender, 0, 3,
                    observation_handler=observer,
                    now_provider=lambda: now,
                )
                self.assertFalse(await delivery.deliver_once("ws"))
                self.assertFalse(await delivery.deliver_once("ws"))

                checked = WorkspaceStateStore(path)
                with checked._connect() as connection:
                    final = connection.execute(
                        "SELECT status, last_error FROM outbox WHERE id=?",
                        (final_id,),
                    ).fetchone()
                    inbounds = dict(
                        connection.execute(
                            "SELECT message_id, status FROM inbound_messages"
                        ).fetchall()
                    )
                    health = connection.execute(
                        """
                        SELECT component, status, detail FROM health_events
                        WHERE component='outbox_delivery'
                        ORDER BY id
                        """
                    ).fetchall()
                    sent_count = connection.execute(
                        "SELECT COUNT(*) FROM outbox WHERE status='sent'"
                    ).fetchone()[0]
                self.assertEqual(
                    final, ("failed", "invalid reserved outbox row")
                )
                self.assertEqual(inbounds, {"m1": "processing", "m2": "processing"})
                for task_id in ("t1", "t2"):
                    task = checked.e4_repository.get_task(task_id)
                    self.assertEqual(
                        (task["status"], task["stage"]),
                        ("running", "delivery_committed"),
                    )
                    self.assertEqual(
                        checked.e4_repository.task_events(task_id),
                        original_events[task_id],
                    )
                self.assertEqual(
                    health,
                    [("outbox_delivery", "degraded", "ambiguous task final ownership")],
                )
                self.assertEqual(sent_count, 0)
                self.assertEqual(qq, [])
                self.assertEqual(observed, [])

    def _prepare_owned_task_final_with_other_owner(self, store, now):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService

        store.claim_inbound("m1", 10001, now=now)
        store.claim_inbound("m2", 20002, now=now)
        owner = store.e4_repository.create_task(
            10001, "m1", "long_task", task_id="t1", now=now
        )
        other = store.e4_repository.create_task(
            20002, "m2", "long_task", task_id="t2", now=now
        )
        store.e4_repository.transition_task(
            owner["id"], "queued", "running", "delivering", now=now
        )
        store.e4_repository.transition_task(
            other["id"], "queued", "running", "running", now=now
        )
        payload = ReliableDeliveryService._encode_observation(
            self._request(), "最终回复"
        )
        store.commit_task_final(
            task_id="t1", user_id=10001, message="最终回复",
            final_key="reply:m1", final_echo="task:t1:final",
            observation_message=payload,
            observation_key="observation:m1", now=now,
        )
        return store.get_outbox("reply:m1")["id"]

    def test_malformed_task_final_user_types_close_unique_owner_and_advance_queue(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        cases = (
            ("text", "not-an-integer"),
            ("real", 10001.5),
            ("negative", -1),
            ("oversized", "100000000000000000000"),
        )
        now = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for label, value in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                final_id = self._prepare_owned_task_final_with_other_owner(
                    store, now
                )
                store.enqueue_outbox(
                    kind="command", user_id=30003, message="unrelated",
                    dedupe_key="command:c1", echo="command:c1", now=now,
                )
                other_before = list(store.e4_repository.task_events("t2"))
                with store._connect() as connection:
                    connection.execute(
                        "UPDATE outbox SET user_id=? WHERE id=?",
                        (value, final_id),
                    )

                try:
                    claimed = WorkspaceStateStore(path).claim_due_outbox(now=now)
                except (TypeError, ValueError, OverflowError) as exc:
                    self.fail("malformed durable user escaped: %s" % type(exc).__name__)

                self.assertIsNotNone(claimed)
                self.assertEqual(claimed["dedupe_key"], "command:c1")
                checked = WorkspaceStateStore(path)
                with checked._connect() as connection:
                    final = connection.execute(
                        "SELECT status FROM outbox WHERE id=?", (final_id,)
                    ).fetchone()[0]
                    inbounds = dict(
                        connection.execute(
                            "SELECT message_id, status FROM inbound_messages"
                        ).fetchall()
                    )
                    health_count = connection.execute(
                        """
                        SELECT COUNT(*) FROM health_events
                        WHERE component='outbox_delivery'
                        """
                    ).fetchone()[0]
                self.assertEqual(final, "failed")
                self.assertEqual(
                    checked.get_outbox("observation:m1")["status"], "failed"
                )
                task = checked.e4_repository.get_task("t1")
                self.assertEqual(
                    (task["status"], task["stage"]),
                    ("failed", "delivery_corrupt"),
                )
                self.assertEqual(
                    inbounds, {"m1": "completed", "m2": "processing"}
                )
                self.assertEqual(health_count, 0)
                self.assertEqual(
                    checked.e4_repository.get_task("t2")["status"], "running"
                )
                self.assertEqual(
                    checked.e4_repository.task_events("t2"), other_before
                )

    def test_ambiguous_malformed_task_final_records_one_health_and_advances_queue(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            now = datetime(2026, 7, 30, tzinfo=timezone.utc)
            store = WorkspaceStateStore(path)
            final_id = self._prepare_owned_task_final_with_other_owner(store, now)
            store.enqueue_outbox(
                kind="command", user_id=30003, message="unrelated",
                dedupe_key="command:c1", echo="command:c1", now=now,
            )
            owner_events = list(store.e4_repository.task_events("t1"))
            with store._connect() as connection:
                connection.execute(
                    """
                    UPDATE outbox SET user_id='not-an-integer',
                                      echo='broken-final-echo'
                    WHERE id=?
                    """,
                    (final_id,),
                )

            try:
                claimed = WorkspaceStateStore(path).claim_due_outbox(now=now)
            except (TypeError, ValueError, OverflowError) as exc:
                self.fail("malformed durable user escaped: %s" % type(exc).__name__)

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["dedupe_key"], "command:c1")
            checked = WorkspaceStateStore(path)
            task = checked.e4_repository.get_task("t1")
            self.assertEqual(
                (task["status"], task["stage"]),
                ("running", "delivery_committed"),
            )
            self.assertEqual(
                checked.e4_repository.task_events("t1"), owner_events
            )
            with checked._connect() as connection:
                inbound = connection.execute(
                    """
                    SELECT status FROM inbound_messages
                    WHERE message_id='m1'
                    """
                ).fetchone()[0]
                health = connection.execute(
                    """
                    SELECT component, status, detail FROM health_events
                    WHERE component='outbox_delivery' ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(inbound, "processing")
            self.assertEqual(
                health,
                [("outbox_delivery", "degraded", "ambiguous task final ownership")],
            )

    def test_malformed_generic_user_types_are_quarantined_and_queue_advances(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        cases = (
            ("text", "not-an-integer"),
            ("real", 10001.5),
            ("negative", -1),
            ("oversized", "100000000000000000000"),
        )
        now = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for label, value in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                malformed = store.enqueue_outbox(
                    kind="command", user_id=10001, message="bad",
                    dedupe_key="command:bad", echo="command:bad", now=now,
                )
                store.enqueue_outbox(
                    kind="command", user_id=20002, message="valid",
                    dedupe_key="command:valid", echo="command:valid", now=now,
                )
                with store._connect() as connection:
                    connection.execute(
                        "UPDATE outbox SET user_id=? WHERE id=?",
                        (value, malformed["id"]),
                    )

                claimed = WorkspaceStateStore(path).claim_due_outbox(now=now)

                self.assertIsNotNone(claimed)
                self.assertEqual(claimed["dedupe_key"], "command:valid")
                bad = WorkspaceStateStore(path).get_outbox("command:bad")
                self.assertEqual(
                    (bad["status"], bad["last_error"]),
                    ("failed", "invalid outbox ownership"),
                )

    def test_null_outbox_user_is_rejected_without_blocking_valid_queue(self):
        import sqlite3

        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            now = datetime(2026, 7, 30, tzinfo=timezone.utc)
            store = WorkspaceStateStore(path)
            row = store.enqueue_outbox(
                kind="command", user_id=30003, message="unrelated",
                dedupe_key="command:c1", echo="command:c1", now=now,
            )
            with store._connect() as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE outbox SET user_id=NULL WHERE id=?",
                        (row["id"],),
                    )

            claimed = WorkspaceStateStore(path).claim_due_outbox(now=now)

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["dedupe_key"], "command:c1")

    def test_malformed_reserved_user_during_grace_terminalizes_and_advances_queue(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        cases = (
            ("task_final", "not-an-integer"),
            ("task_notice", 10001.5),
            ("e3_observation", -1),
        )
        claimed_at = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for kind, value in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "state.sqlite3"
                store = WorkspaceStateStore(path)
                key = self._prepare_reserved_lease_case(
                    store, kind, claimed_at
                )
                first = store.claim_due_outbox(
                    now=claimed_at, max_attempts=1, lease_seconds=300
                )
                grace_at = claimed_at + timedelta(seconds=300)
                grace_store = WorkspaceStateStore(path)
                grace = grace_store.claim_due_outbox(
                    now=grace_at, max_attempts=1, lease_seconds=300
                )
                self.assertEqual(grace["id"], first["id"])
                grace_store.enqueue_outbox(
                    kind="command", user_id=30003, message="unrelated",
                    dedupe_key="command:c1", echo="command:c1", now=grace_at,
                )
                with grace_store._connect() as connection:
                    connection.execute(
                        "UPDATE outbox SET user_id=? WHERE id=?",
                        (value, grace["id"]),
                    )

                terminal_at = grace_at + timedelta(seconds=300)
                try:
                    claimed = WorkspaceStateStore(path).claim_due_outbox(
                        now=terminal_at, max_attempts=1, lease_seconds=300
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    self.fail(
                        "malformed durable user escaped: %s"
                        % type(exc).__name__
                    )

                self.assertIsNotNone(claimed)
                self.assertEqual(claimed["dedupe_key"], "command:c1")
                checked = WorkspaceStateStore(path)
                self.assertEqual(checked.get_outbox(key)["status"], "failed")
                if kind == "task_final":
                    task = checked.e4_repository.get_task("t1")
                    self.assertEqual(
                        (task["status"], task["stage"]),
                        ("failed", "delivery_failed"),
                    )
                    self.assertEqual(
                        checked.get_outbox("observation:m1")["status"],
                        "failed",
                    )

    def test_corrupted_owned_task_final_closes_exact_lifecycle_atomically(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 29, tzinfo=timezone.utc)
            store.claim_inbound("m1", 10001, now=now)
            store.claim_inbound("m2", 20002, now=now)
            owner = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=now
            )
            other = store.e4_repository.create_task(
                20002, "m2", "long_task", task_id="t2", now=now
            )
            store.e4_repository.transition_task(
                owner["id"], "queued", "running", "delivering", now=now
            )
            store.e4_repository.transition_task(
                other["id"], "queued", "running", "running", now=now
            )
            payload = ReliableDeliveryService._encode_observation(
                self._request(), "最终回复"
            )
            store.commit_task_final(
                task_id="t1", user_id=10001, message="最终回复",
                final_key="reply:m1", final_echo="task:t1:final",
                observation_message=payload,
                observation_key="observation:m1", now=now,
            )
            other_events = list(store.e4_repository.task_events("t2"))
            with store._connect() as connection:
                connection.execute(
                    "UPDATE outbox SET echo='corrupted' WHERE dedupe_key='reply:m1'"
                )

            self.assertIsNone(store.claim_due_outbox(now=now))
            self.assertEqual(store.get_outbox("reply:m1")["status"], "failed")
            self.assertEqual(
                store.get_outbox("observation:m1")["status"], "failed"
            )
            task = store.e4_repository.get_task("t1")
            self.assertEqual(
                (task["status"], task["stage"]), ("failed", "delivery_corrupt")
            )
            with store._connect() as connection:
                inbound = dict(
                    connection.execute(
                        "SELECT message_id, status FROM inbound_messages"
                    ).fetchall()
                )
                backlog = connection.execute(
                    """
                    SELECT COUNT(*) FROM outbox
                    WHERE status IN ('pending', 'sending')
                    """
                ).fetchone()[0]
            self.assertEqual(inbound, {"m1": "completed", "m2": "processing"})
            self.assertEqual(backlog, 0)
            self.assertEqual(
                (store.e4_repository.get_task("t2")["status"],
                 store.e4_repository.get_task("t2")["stage"]),
                ("running", "running"),
            )
            self.assertEqual(store.e4_repository.task_events("t2"), other_events)
            owner_events = store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["stage"] for event in owner_events].count(
                    "delivery_corrupt"
                ),
                1,
            )
            self.assertNotIn(
                "notification_sent",
                [event["event_type"] for event in owner_events],
            )

    def test_corrupted_notice_and_cross_owner_observation_close_safely(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 29, tzinfo=timezone.utc)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=now
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=now
            )
            store.enqueue_task_notice(
                task_id="t1", user_id=10001, message="ack", role="ack", now=now
            )
            timestamp = now.isoformat()
            with store._connect() as connection:
                connection.execute(
                    """
                    UPDATE outbox SET echo='corrupted'
                    WHERE dedupe_key='task:t1:notice:1'
                    """
                )
                connection.execute(
                    """
                    INSERT INTO outbox(
                      kind, user_id, message, echo, dedupe_key, status,
                      attempts, next_attempt_at, created_at, updated_at, sent_at
                    ) VALUES ('reply', 20002, 'other', 'reply:m2', 'reply:m2',
                              'sent', 1, ?, ?, ?, ?)
                    """,
                    (timestamp, timestamp, timestamp, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO outbox(
                      kind, user_id, message, echo, dedupe_key, status,
                      attempts, next_attempt_at, created_at, updated_at
                    ) VALUES ('e3_observation', 10001, '{}', 'reply:m2',
                              'observation:m2', 'pending', 0, ?, ?, ?)
                    """,
                    (timestamp, timestamp, timestamp),
                )

            self.assertIsNone(store.claim_due_outbox(now=now))
            notice = store.get_outbox("task:t1:notice:1")
            observation = store.get_outbox("observation:m2")
            dependency = store.get_outbox("reply:m2")
            self.assertEqual((notice["status"], observation["status"]),
                             ("failed", "failed"))
            self.assertEqual(dependency["status"], "sent")
            events = store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["event_type"] for event in events].count(
                    "notification_failed"
                ),
                1,
            )
            self.assertNotIn(
                "notification_sent", [event["event_type"] for event in events]
            )
            with store._connect() as connection:
                backlog = connection.execute(
                    """
                    SELECT COUNT(*) FROM outbox
                    WHERE status IN ('pending', 'sending')
                    """
                ).fetchone()[0]
            self.assertEqual(backlog, 0)

    async def test_duplicate_final_creates_one_pair_and_observer_permanent_failure_sends_no_json(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            sent = []

            async def sender(websocket, user_id, message, echo=None):
                sent.append(message)

            async def observer(request, reply):
                raise RuntimeError("observer unavailable")

            service = ReliableDeliveryService(
                store, sender, 0, 1, observation_handler=observer
            )
            self.assertTrue(await service.send_final("ws", self._request(), "最终回复"))
            self.assertTrue(await service.send_final("ws", self._request(), "最终回复"))

            with store._connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            self.assertEqual(count, 2)
            self.assertEqual(store.get_outbox("observation:m1")["status"], "failed")
            self.assertEqual(sent, ["最终回复"])

    async def test_normal_reply_cannot_disable_inbound_completion(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ReliableDeliveryService(
                WorkspaceStateStore(Path(temp_dir) / "state.sqlite3"),
                lambda *args, **kwargs: None,
                0,
                3,
            )
            for value in (False, 0, None, ""):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "complete_inbound"):
                        await service.send(
                            "ws", 10001, "reply", "reply:m1", "m1",
                            kind="reply", complete_inbound=value,
                        )

    async def test_already_sent_reply_never_completes_inbound_via_unscoped_api(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        cases = (
            ("exact_owner", 10001, 10001, "reply", "reply:m1", True),
            ("cross_user", 20002, 10001, "reply", "reply:m1", False),
            ("malformed_owner", 20002, "malformed", "reply", "reply:m1", False),
            ("wrong_kind", 10001, 10001, "task_notice", "reply:m1", False),
            ("wrong_echo", 10001, 10001, "reply", "wrong:m1", False),
        )
        for (
            label,
            inbound_user,
            final_user,
            final_kind,
            final_echo,
            expected_result,
        ) in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
                store.claim_inbound("m1", inbound_user)
                now = datetime(2026, 7, 30, tzinfo=timezone.utc).isoformat()
                with store._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO outbox(
                          kind, user_id, message, echo, dedupe_key, status,
                          attempts, next_attempt_at, created_at, updated_at, sent_at
                        ) VALUES (?, ?, 'persisted final', ?, 'reply:m1', 'sent',
                                  1, ?, ?, ?, ?)
                        """,
                        (final_kind, final_user, final_echo, now, now, now, now),
                    )

                sent = []

                async def sender(*args, **kwargs):
                    sent.append(args)

                service = ReliableDeliveryService(store, sender, 0, 3)
                self.assertEqual(
                    await service.send(
                        "ws", inbound_user, "new reply", "reply:m1", "m1"
                    ),
                    expected_result,
                )

                with store._connect() as connection:
                    inbound = connection.execute(
                        """
                        SELECT user_id, status FROM inbound_messages
                        WHERE message_id='m1'
                        """
                    ).fetchone()
                    persisted = connection.execute(
                        """
                        SELECT kind, user_id, echo, status FROM outbox
                        WHERE dedupe_key='reply:m1'
                        """
                    ).fetchone()
                self.assertEqual(inbound, (inbound_user, "processing"))
                self.assertEqual(
                    persisted, (final_kind, final_user, final_echo, "sent")
                )
                self.assertEqual(sent, [])

    async def test_task_notice_audits_only_after_actual_retry_delivery(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.claim_inbound("m1", 10001)
            task = store.e4_repository.create_task(10001, "m1", "long_task", task_id="t1")
            store.e4_repository.transition_task(task["id"], "queued", "running", "running")
            calls = 0

            async def sender(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ConnectionError("offline")

            service = ReliableDeliveryService(store, sender, 0, 2)
            self.assertFalse(await service.send_task_notice(
                "ws", "t1", 10001, "处理中", role="ack"
            ))
            self.assertIsNone(store.e4_repository.get_task("t1")["last_notified_at"])

            self.assertTrue(await service.deliver_once("ws"))

            task = store.e4_repository.get_task("t1")
            self.assertIsNotNone(task["last_notified_at"])
            self.assertEqual(
                [event["event_type"] for event in store.e4_repository.task_events("t1")].count("notification_sent"),
                1,
            )
            with store._connect() as connection:
                inbound = connection.execute(
                    "SELECT status FROM inbound_messages WHERE message_id='m1'"
                ).fetchone()[0]
            self.assertEqual(inbound, "processing")

    async def test_exhausted_task_notice_records_only_controlled_failure(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            task = store.e4_repository.create_task(10001, "m1", "long_task", task_id="t1")
            store.e4_repository.transition_task(task["id"], "queued", "running", "running")

            async def sender(*args, **kwargs):
                raise ConnectionError("secret endpoint")

            service = ReliableDeliveryService(store, sender, 0, 1)
            self.assertFalse(await service.send_task_notice(
                "ws", "t1", 10001, "处理中", role="ack"
            ))

            events = store.e4_repository.task_events("t1")
            self.assertEqual(
                [event["event_type"] for event in events].count("notification_failed"), 1
            )
            self.assertNotIn("notification_sent", [event["event_type"] for event in events])
            self.assertNotIn("secret endpoint", repr(events))

    async def test_cancel_suppresses_pending_notice_and_terminalizes_inbound(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 27, tzinfo=timezone.utc)
            store.claim_inbound("m1", 10001, now=now)
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", now=now, task_id="t1"
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=now
            )

            async def offline(*args, **kwargs):
                raise ConnectionError("offline")

            delivery = ReliableDeliveryService(store, offline, 0, 3)
            self.assertFalse(
                await delivery.send_task_notice(
                    "ws", "t1", 10001, "任务已接单", role="ack"
                )
            )
            self.assertIsNotNone(
                store.e4_repository.request_cancel("t1", 10001, now=now)
            )

            row = store.get_outbox("task:t1:notice:1")
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["last_error"], "task notice suppressed")
            self.assertFalse(
                store.claim_inbound("m1", 10001, now=now + timedelta(seconds=301))
            )

            sent = []

            async def sender(*args, **kwargs):
                sent.append(args)

            restarted = ReliableDeliveryService(store, sender, 0, 3)
            self.assertFalse(await restarted.deliver_once("ws"))
            self.assertEqual(sent, [])

    async def test_terminal_task_suppresses_pending_notice_before_retry(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1"
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running"
            )

            async def offline(*args, **kwargs):
                raise ConnectionError("offline")

            delivery = ReliableDeliveryService(store, offline, 0, 3)
            await delivery.send_task_notice(
                "ws", "t1", 10001, "任务已接单", role="ack"
            )
            store.e4_repository.transition_task(
                "t1", "running", "succeeded", "succeeded"
            )

            self.assertEqual(
                store.get_outbox("task:t1:notice:1")["status"], "failed"
            )

    async def test_restart_coalesces_progress_and_spaces_actual_notices(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            task = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1"
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running"
            )
            current = [datetime(2026, 7, 27, tzinfo=timezone.utc)]

            async def offline(*args, **kwargs):
                raise ConnectionError("offline")

            delivery = ReliableDeliveryService(
                store, offline, 0, 8, now_provider=lambda: current[0]
            )
            await delivery.send_task_notice(
                "ws", "t1", 10001, "任务已接单", role="ack"
            )
            await delivery.send_task_notice(
                "ws", "t1", 10001, "旧进度", role="progress"
            )
            await delivery.send_task_notice(
                "ws", "t1", 10001, "最新进度", role="progress"
            )

            with store._connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM outbox WHERE kind='task_notice'"
                    ).fetchone()[0],
                    2,
                )
            sent = []

            async def sender(websocket, user_id, message, echo=None):
                sent.append((current[0], message))

            restarted = ReliableDeliveryService(
                store, sender, 0, 8, now_provider=lambda: current[0],
                task_notice_interval_seconds=60,
            )
            self.assertTrue(await restarted.deliver_once("ws"))
            self.assertEqual([item[1] for item in sent], ["任务已接单"])
            self.assertFalse(await restarted.deliver_once("ws"))
            current[0] += timedelta(seconds=59)
            self.assertFalse(await restarted.deliver_once("ws"))
            current[0] += timedelta(seconds=1)
            self.assertTrue(await restarted.deliver_once("ws"))

            self.assertEqual([item[1] for item in sent], ["任务已接单", "最新进度"])
            self.assertEqual((sent[1][0] - sent[0][0]).total_seconds(), 60)


if __name__ == "__main__":
    unittest.main()
