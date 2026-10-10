import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 26, tzinfo=timezone.utc)

    def now(self):
        return self.value

    async def sleep(self, seconds):
        await asyncio.Event().wait()


class _ManualClock:
    def __init__(self):
        self.value = datetime(2026, 7, 26, tzinfo=timezone.utc)
        self._sleepers = []

    def now(self):
        return self.value

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.value + timedelta(seconds=seconds), future))
        await future

    async def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
        for deadline, future in tuple(self._sleepers):
            if deadline <= self.value and not future.done():
                future.set_result(None)
        self._sleepers = [
            (deadline, future)
            for deadline, future in self._sleepers
            if not future.done()
        ]
        for _ in range(4):
            await asyncio.sleep(0)


class LongTaskServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, root, clock=None, **kwargs):
        from qq_openclaw_bridge.application.long_tasks import LongTaskService
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        repository = E4Repository(SqliteDatabase(Path(root) / "state.sqlite3"))
        clock = clock or _Clock()
        return repository, LongTaskService(
            repository, clock=clock.now, sleep=clock.sleep, **kwargs
        )

    def _envelope(self):
        from qq_openclaw_bridge.domain.inbound import InboundEnvelope

        return InboundEnvelope(10001, "message-1", 1.0, ("处理附件",))

    async def test_creates_then_transitions_and_records_append_only_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            seen = []

            async def operation(task_id, cancellation):
                task = repository.get_task(task_id)
                seen.append((task["status"], cancellation.cancelled))
                service.report(task_id, "working", "整理中")
                return "完成"

            self.assertEqual(
                await service.run(self._envelope(), operation, lambda notice: None),
                "完成",
            )
            task = repository.list_tasks()[0]
            self.assertEqual(seen, [("running", False)])
            self.assertEqual(task["status"], "succeeded")
            self.assertEqual(
                [item["event_type"] for item in repository.task_events(task["id"])],
                ["transition", "progress", "transition"],
            )

    async def test_ack_and_progress_throttle_coalesce_and_persist_real_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = _Clock()
            repository, service = self._service(temp_dir, clock)
            notices = []
            release = asyncio.Event()
            started = asyncio.Event()

            async def operation(task_id, cancellation):
                started.set()
                service.report(task_id, "downloading", "下载附件")
                await release.wait()
                return "完成"

            running = asyncio.create_task(service.run(
                self._envelope(), operation, notices.append
            ))
            await started.wait()
            task_id = repository.list_tasks()[0]["id"]
            self.assertEqual(notices, [])
            service.report(task_id, "extracting", "提取正文")
            service.report(task_id, "reasoning", "整理答案")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(notices, [])
            release.set()
            await running
            events = repository.task_events(task_id)
            self.assertEqual(
                [item["stage"] for item in events if item["event_type"] == "progress"],
                ["downloading", "extracting", "reasoning"],
            )
            self.assertIsNone(repository.get_task(task_id)["last_notified_at"])

    async def test_fast_task_has_no_ack_and_timers_are_cleaned_up(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            notices = []

            async def operation(task_id, cancellation):
                return "快完成"

            self.assertEqual(
                await service.run(self._envelope(), operation, notices.append), "快完成"
            )
            self.assertEqual(notices, [])
            self.assertEqual(service.active_task_ids, ())
            self.assertEqual(repository.list_tasks()[0]["status"], "succeeded")

    async def test_deferred_terminal_result_leaves_task_running_for_outbox(self):
        from qq_openclaw_bridge.application.long_tasks import DeferredTaskResult

        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)

            async def operation(task_id, cancellation):
                repository.record_task_progress(task_id, "delivery_committed")
                return DeferredTaskResult(False)

            result = await service.run(
                self._envelope(), operation, lambda notice: None
            )

            self.assertFalse(result)
            task = repository.list_tasks()[0]
            self.assertEqual((task["status"], task["stage"]), ("running", "delivery_committed"))
            self.assertEqual(service.active_task_ids, ())

    async def test_cancel_after_final_commit_is_rejected_and_gated_reply_delivers(self):
        from dataclasses import replace
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.application.long_tasks import DeferredTaskResult, LongTaskService
        from qq_openclaw_bridge.models import AgentRequest
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            service = LongTaskService(store.e4_repository)
            sender_entered = asyncio.Event()
            sender_release = asyncio.Event()
            sent = []

            async def sender(websocket, user_id, message, echo=None):
                sender_entered.set()
                await sender_release.wait()
                sent.append((message, echo))

            async def observe(request, reply):
                return None

            delivery = ReliableDeliveryService(
                store, sender, 0, 3, observation_handler=observe
            )

            async def operation(task_id, cancellation):
                request = replace(
                    AgentRequest(10001, "message-1", "处理附件", 1.0),
                    task_id=task_id,
                )
                result = await delivery.send_final(
                    "ws", request, "最终回复", task_id=task_id
                )
                return DeferredTaskResult(result)

            running = asyncio.create_task(
                service.run(self._envelope(), operation, lambda notice: None)
            )
            await sender_entered.wait()
            task_id = store.e4_repository.list_tasks()[0]["id"]

            self.assertFalse(service.cancel(task_id, 10001))
            self.assertEqual(
                store.e4_repository.get_task(task_id)["stage"],
                "delivery_committed",
            )
            sender_release.set()
            self.assertTrue(await running)

            self.assertEqual(sent, [("最终回复", "task:%s:final" % task_id)])
            self.assertEqual(store.e4_repository.get_task(task_id)["status"], "succeeded")

    async def test_false_notification_result_does_not_record_sent_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = _ManualClock()
            repository, service = self._service(temp_dir, clock)
            started = asyncio.Event()
            release = asyncio.Event()

            async def operation(task_id, cancellation):
                started.set()
                await release.wait()
                return "done"

            async def sink(notice):
                return False

            running = asyncio.create_task(
                service.run(self._envelope(), operation, sink)
            )
            try:
                await started.wait()
                task_id = repository.list_tasks()[0]["id"]
                await clock.advance(15)
                task = repository.get_task(task_id)
                self.assertIsNone(task["last_notified_at"])
                self.assertNotIn(
                    "notification_sent",
                    [event["event_type"] for event in repository.task_events(task_id)],
                )
            finally:
                release.set()
                await running

    async def test_durable_notification_outcome_defers_audit_to_outbox(self):
        from qq_openclaw_bridge.application.long_tasks import NotificationDeliveryOutcome

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = _ManualClock()
            repository, service = self._service(temp_dir, clock)
            started = asyncio.Event()
            release = asyncio.Event()

            async def operation(task_id, cancellation):
                started.set()
                await release.wait()
                return "done"

            async def sink(notice):
                return NotificationDeliveryOutcome(delivered=True, audit_deferred=True)

            running = asyncio.create_task(
                service.run(self._envelope(), operation, sink)
            )
            try:
                await started.wait()
                task_id = repository.list_tasks()[0]["id"]
                await clock.advance(15)
                self.assertIsNone(repository.get_task(task_id)["last_notified_at"])
            finally:
                release.set()
                await running

    async def test_notification_sink_can_bind_to_exact_persisted_task_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            bound = []

            class Sink:
                def bind_task(self, task_id):
                    bound.append(task_id)
                    return lambda notice: None

            async def operation(task_id, cancellation):
                return task_id

            task_id = await service.run(self._envelope(), operation, Sink())

            self.assertEqual(bound, [task_id])

    async def test_notification_sink_binding_failure_marks_task_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)

            class Sink:
                def bind_task(self, task_id):
                    raise RuntimeError("https://secret.invalid/?token=leak")

            async def operation(task_id, cancellation):
                raise AssertionError("operation must not start")

            with self.assertRaises(RuntimeError):
                await service.run(self._envelope(), operation, Sink())

            task = repository.list_tasks()[0]
            self.assertEqual(task["status"], "failed")
            self.assertEqual(service.active_task_ids, ())
            self.assertNotIn(
                "secret.invalid", repr(repository.task_events(task["id"]))
            )

    async def test_owner_cancel_discards_cancellable_operation_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            started = asyncio.Event()

            async def operation(task_id, cancellation):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return "不得返回"

            running = asyncio.create_task(service.run(
                self._envelope(), operation, lambda notice: None
            ))
            await started.wait()
            task_id = repository.list_tasks()[0]["id"]
            self.assertTrue(service.cancel(task_id, 10001))
            with self.assertRaises(asyncio.CancelledError):
                await running
            self.assertEqual(repository.get_task(task_id)["status"], "cancelled")
            self.assertNotIn(
                "succeeded",
                [item["stage"] for item in repository.task_events(task_id)],
            )

    async def test_cross_instance_cancel_is_observed_by_owner_monitor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(
                temp_dir, cancel_sleep=lambda seconds: asyncio.sleep(0)
            )
            from qq_openclaw_bridge.application.long_tasks import LongTaskService
            from qq_openclaw_bridge.persistence.database import SqliteDatabase
            from qq_openclaw_bridge.persistence.e4 import E4Repository

            other = LongTaskService(
                E4Repository(SqliteDatabase(Path(temp_dir) / "state.sqlite3"))
            )
            started = asyncio.Event()

            async def operation(task_id, cancellation):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return "discarded"

            running = asyncio.create_task(service.run(
                self._envelope(), operation, lambda notice: None
            ))
            await started.wait()
            task_id = repository.list_tasks()[0]["id"]
            self.assertTrue(other.cancel(task_id, 10001))
            with self.assertRaises(asyncio.CancelledError):
                await running
            self.assertEqual(repository.get_task(task_id)["status"], "cancelled")

    async def test_owner_cancel_in_queued_window_never_starts_operation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            ran = []

            async def operation(task_id, cancellation):
                ran.append(task_id)
                return "unexpected"

            running = asyncio.create_task(service.run(
                self._envelope(), operation, lambda notice: None
            ))
            await asyncio.sleep(0)
            task_id = repository.list_tasks()[0]["id"]
            self.assertEqual(repository.get_task(task_id)["status"], "queued")
            self.assertTrue(service.cancel(task_id, 10001))
            with self.assertRaises(asyncio.CancelledError):
                await running
            self.assertEqual(ran, [])
            self.assertEqual(repository.get_task(task_id)["status"], "cancelled")

    async def test_cancel_from_non_event_loop_thread_signals_owner_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            started = asyncio.Event()

            async def operation(task_id, cancellation):
                started.set()
                await asyncio.Event().wait()

            running = asyncio.create_task(service.run(
                self._envelope(), operation, lambda notice: None
            ))
            await started.wait()
            task_id = repository.list_tasks()[0]["id"]
            result = []
            caller = threading.Thread(
                target=lambda: result.append(service.cancel(task_id, 10001))
            )
            caller.start()
            caller.join()
            with self.assertRaises(asyncio.CancelledError):
                await running
            self.assertEqual(result, [True])
            self.assertEqual(repository.get_task(task_id)["status"], "cancelled")

    async def test_failure_and_late_reports_are_controlled_and_terminal_is_immutable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)

            async def operation(task_id, cancellation):
                raise RuntimeError("https://secret.example/?token=leak")

            with self.assertRaises(RuntimeError):
                await service.run(self._envelope(), operation, lambda notice: None)
            task = repository.list_tasks()[0]
            self.assertEqual(task["status"], "failed")
            self.assertNotIn(
                "secret.example",
                " ".join(str(item["detail"]) for item in repository.task_events(task["id"])),
            )
            before = len(repository.task_events(task["id"]))
            service.report(task["id"], "late", "不应记录")
            self.assertFalse(service.cancel(task["id"], 10001))
            self.assertEqual(len(repository.task_events(task["id"])), before)

    async def test_restart_can_inspect_persisted_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)

            async def operation(task_id, cancellation):
                return "完成"

            await service.run(self._envelope(), operation, lambda notice: None)
            from qq_openclaw_bridge.persistence.database import SqliteDatabase
            from qq_openclaw_bridge.persistence.e4 import E4Repository

            restarted = E4Repository(SqliteDatabase(Path(temp_dir) / "state.sqlite3"))
            task = restarted.list_tasks(user_id=10001)[0]
            self.assertEqual(task["status"], "succeeded")
            self.assertTrue(restarted.task_events(task["id"]))

    async def test_external_cancel_during_setup_cleans_operation_and_marks_interrupted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            operation_started = asyncio.Event()
            operation_finished = asyncio.Event()

            async def operation(task_id, cancellation):
                operation_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    operation_finished.set()

            running = asyncio.create_task(service.run(
                self._envelope(), operation, lambda notice: None
            ))
            await asyncio.sleep(0)
            task_id = repository.list_tasks()[0]["id"]
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            await asyncio.sleep(0)

            self.assertFalse(operation_started.is_set())
            self.assertFalse(operation_finished.is_set())
            self.assertEqual(service.active_task_ids, ())
            self.assertEqual(repository.get_task(task_id)["status"], "cancelled")
            self.assertEqual(repository.get_task(task_id)["stage"], "interrupted")
            self.assertEqual(repository.get_task(task_id)["cancel_requested"], 0)

    async def test_ack_precedes_coalesced_progress_at_exact_manual_clock_times(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = _ManualClock()
            repository, service = self._service(temp_dir, clock)
            notices = []
            started = asyncio.Event()
            release = asyncio.Event()

            async def operation(task_id, cancellation):
                started.set()
                service.report(task_id, "downloading", "attachment=1 kind=image")
                await release.wait()
                return "完成"

            async def sink(notice):
                notices.append((clock.now(), notice))

            running = asyncio.create_task(service.run(
                self._envelope(), operation, sink,
            ))
            try:
                await started.wait()
                for _ in range(3):
                    await asyncio.sleep(0)
                task_id = repository.list_tasks()[0]["id"]
                self.assertEqual(notices, [])
                await clock.advance(14)
                self.assertEqual(notices, [])
                await clock.advance(1)
                self.assertEqual(
                    notices,
                    [(clock.now(), "任务 #%s 已接单，正在处理。" % task_id)],
                )

                service.report(task_id, "extracting", "attachment=1 kind=file")
                service.report(task_id, "reasoning", "attachment=2 kind=audio")
                await clock.advance(59)
                self.assertEqual(len(notices), 1)
                await clock.advance(1)
                self.assertIn("reasoning", notices[-1][1])
                self.assertEqual((notices[-1][0] - notices[0][0]).total_seconds(), 60)
                service.report(task_id, "writing", "attachment=3 kind=file")
                service.report(task_id, "finalizing", "attachment=4 kind=image")
                await clock.advance(60)
                self.assertIn("finalizing", notices[-1][1])
                self.assertEqual((notices[-1][0] - notices[-2][0]).total_seconds(), 60)
            finally:
                release.set()
                await running

    def test_untrusted_stage_and_detail_are_not_persisted_or_sent(self):
        from qq_openclaw_bridge.application.long_tasks import LongTaskService

        stage = LongTaskService._safe_stage("Traceback Authorization: Bearer leak")
        detail = LongTaskService._safe_detail(
            "raw media\napi_key: leak host.example/path Authorization: Bearer leak"
        )

        self.assertEqual(stage, "working")
        self.assertEqual(detail, "")

    async def test_report_filters_sensitive_content_in_events_and_notices(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = _ManualClock()
            repository, service = self._service(temp_dir, clock)
            notices = []
            release = asyncio.Event()
            started = asyncio.Event()
            sensitive = "media Traceback Bearer api_key: secret host.example/path"

            async def operation(task_id, cancellation):
                started.set()
                service.report(task_id, sensitive, sensitive)
                await release.wait()
                return "done"

            async def sink(notice):
                notices.append(notice)

            running = asyncio.create_task(service.run(self._envelope(), operation, sink))
            try:
                await started.wait()
                task_id = repository.list_tasks()[0]["id"]
                await clock.advance(15)
                service.report(task_id, "downloading", "attachment=1 kind=image")
                await clock.advance(60)
                events = repository.task_events(task_id)
                stored = " ".join("%s %s" % (event["stage"], event["detail"]) for event in events)
                self.assertNotIn("secret", stored + " ".join(notices))
                self.assertNotIn("host.example", stored + " ".join(notices))
                self.assertIn("attachment=1 kind=image", stored)
            finally:
                release.set()
                await running

    async def test_sync_callable_returning_coroutine_is_awaited_before_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = _ManualClock()
            repository, service = self._service(temp_dir, clock)
            delivered = asyncio.Event()
            release = asyncio.Event()

            def sink(notice):
                async def deliver():
                    delivered.set()
                return deliver()

            async def operation(task_id, cancellation):
                await release.wait()
                return "done"

            running = asyncio.create_task(service.run(self._envelope(), operation, sink))
            try:
                for _ in range(3):
                    await asyncio.sleep(0)
                await clock.advance(15)
                await asyncio.wait_for(delivered.wait(), 1)
                task = repository.list_tasks()[0]
                for _ in range(100):
                    events = repository.task_events(task["id"])
                    if events[-1]["event_type"] == "notification_sent":
                        break
                    await asyncio.sleep(0)
                else:
                    self.fail("notification audit was not observable")
                self.assertEqual(repository.task_events(task["id"])[-1]["event_type"], "notification_sent")
            finally:
                release.set()
                await running

    def test_non_positive_cancel_poll_interval_is_rejected(self):
        from qq_openclaw_bridge.application.long_tasks import LongTaskService

        for value in (0, -1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                LongTaskService(object(), cancel_poll_seconds=value)
        LongTaskService(object(), cancel_poll_seconds=0.1)

    async def test_cancelled_returned_awaitable_is_audited_after_side_effect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = _ManualClock()
            repository, service = self._service(temp_dir, clock)
            entered = asyncio.Event()
            release = asyncio.Event()
            side_effects = []
            operation_started = asyncio.Event()

            def sink(notice):
                async def deliver():
                    entered.set()
                    await release.wait()
                    side_effects.append(notice)
                return deliver()

            async def operation(task_id, cancellation):
                operation_started.set()
                await asyncio.Event().wait()

            running = asyncio.create_task(service.run(self._envelope(), operation, sink))
            await operation_started.wait()
            await clock.advance(15)
            await entered.wait()
            running.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
            task = repository.list_tasks()[0]
            self.assertEqual(len(side_effects), 1)
            self.assertIsNotNone(task["last_notified_at"])
            self.assertEqual(repository.task_events(task["id"])[-1]["event_type"], "notification_sent")

    async def test_blocking_sync_sink_is_audited_before_cancelled_run_exits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = _ManualClock()
            repository, service = self._service(temp_dir, clock)
            entered = threading.Event()
            release_sink = threading.Event()
            side_effects = []
            operation_started = asyncio.Event()

            def sink(notice):
                entered.set()
                release_sink.wait()
                side_effects.append(notice)

            async def operation(task_id, cancellation):
                operation_started.set()
                await asyncio.Event().wait()

            running = asyncio.create_task(service.run(self._envelope(), operation, sink))
            await operation_started.wait()
            await clock.advance(15)
            await asyncio.to_thread(entered.wait)
            running.cancel()
            await asyncio.sleep(0)
            release_sink.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
            task = repository.list_tasks()[0]
            events = repository.task_events(task["id"])
            self.assertEqual(len(side_effects), 1)
            self.assertIsNotNone(task["last_notified_at"])
            self.assertEqual(events[-1]["event_type"], "notification_sent")
            await asyncio.sleep(0)
            self.assertEqual(len(side_effects), 1)
            service_tasks = [
                task for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and "LongTaskService" in repr(task.get_coro())
            ]
            self.assertEqual(service_tasks, [])

    async def test_sync_and_async_sink_failures_are_controlled_without_success_audit(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory() as temp_dir:
                clock = _ManualClock()
                repository, service = self._service(temp_dir, clock)
                release = asyncio.Event()
                started = asyncio.Event()

                if asynchronous:
                    async def sink(notice):
                        raise RuntimeError("Authorization: Bearer secret")
                else:
                    def sink(notice):
                        raise RuntimeError("api_key=secret")

                async def operation(task_id, cancellation):
                    started.set()
                    await release.wait()
                    return "done"

                running = asyncio.create_task(service.run(self._envelope(), operation, sink))
                try:
                    await started.wait()
                    await clock.advance(15)
                    for _ in range(3):
                        await asyncio.sleep(0)
                    task = repository.list_tasks()[0]
                    events = repository.task_events(task["id"])
                    self.assertEqual(events[-1]["event_type"], "notification_failed")
                    self.assertIsNone(task["last_notified_at"])
                    self.assertNotIn("secret", " ".join(str(event["detail"]) for event in events))
                finally:
                    release.set()
                    await running

    async def test_to_thread_worker_result_is_discarded_after_owner_cancel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            worker_started = threading.Event()
            worker_release = threading.Event()
            worker_done = threading.Event()

            def worker():
                worker_started.set()
                worker_release.wait()
                worker_done.set()
                return "late result"

            async def operation(task_id, cancellation):
                return await asyncio.to_thread(worker)

            running = asyncio.create_task(service.run(self._envelope(), operation, lambda notice: None))
            await asyncio.to_thread(worker_started.wait)
            task_id = repository.list_tasks()[0]["id"]
            self.assertTrue(service.cancel(task_id, 10001))
            with self.assertRaises(asyncio.CancelledError):
                await running
            worker_release.set()
            await asyncio.to_thread(worker_done.wait)
            self.assertEqual(repository.get_task(task_id)["status"], "cancelled")

    async def test_external_cancel_while_running_is_failed_interrupted_and_cleaned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service = self._service(temp_dir)
            started = asyncio.Event()

            async def operation(task_id, cancellation):
                started.set()
                await asyncio.Event().wait()

            running = asyncio.create_task(service.run(
                self._envelope(), operation, lambda notice: None
            ))
            await started.wait()
            task_id = repository.list_tasks()[0]["id"]
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            task = repository.get_task(task_id)
            self.assertEqual((task["status"], task["stage"], task["cancel_requested"]),
                             ("failed", "interrupted", 0))
            self.assertEqual(service.active_task_ids, ())


if __name__ == "__main__":
    unittest.main()
