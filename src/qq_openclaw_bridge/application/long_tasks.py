import asyncio
import inspect
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, Optional


_ACTIVE_STATUSES = {"queued", "running"}
_STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DETAIL_PATTERN = re.compile(
    r"^attachment=\d+ kind=[a-z][a-z0-9_-]{0,31}"
    r"(?: asset_id=[A-Za-z0-9_-]{1,80})?$"
)


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError()


@dataclass(frozen=True)
class DeferredTaskResult:
    value: object


@dataclass(frozen=True)
class NotificationDeliveryOutcome:
    delivered: bool
    audit_deferred: bool = False


@dataclass
class _ActiveTask:
    task_id: str
    user_id: int
    token: CancellationToken
    notification_sink: Callable[[str], object]
    loop: asyncio.AbstractEventLoop
    created_at: datetime
    notice_lock: asyncio.Lock
    ack_done: asyncio.Event
    operation: Optional[asyncio.Task] = None
    ack_timer: Optional[asyncio.Task] = None
    progress_timer: Optional[asyncio.Task] = None
    cancel_monitor: Optional[asyncio.Task] = None
    pending_progress: Optional[tuple] = None


class LongTaskService:
    def __init__(
        self,
        repository,
        clock: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        ack_seconds: int = 15,
        progress_interval_seconds: int = 60,
        kind: str = "long_task",
        cancel_poll_seconds: float = 0.1,
        cancel_sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._sleep = sleep or asyncio.sleep
        self._cancel_sleep = cancel_sleep or asyncio.sleep
        self._ack_seconds = int(ack_seconds)
        self._progress_interval_seconds = int(progress_interval_seconds)
        self._cancel_poll_seconds = float(cancel_poll_seconds)
        if not math.isfinite(self._cancel_poll_seconds) or self._cancel_poll_seconds <= 0:
            raise ValueError("cancel_poll_seconds must be positive")
        self._kind = kind
        self._active: Dict[str, _ActiveTask] = {}

    @property
    def active_task_ids(self):
        return tuple(self._active.keys())

    async def run(self, envelope, operation, notification_sink) -> str:
        created_at = self._clock()
        task = self._repository.create_task(
            envelope.user_id, envelope.message_id, self._kind, now=created_at
        )
        task_id = str(task["id"])
        loop = asyncio.get_running_loop()
        active = _ActiveTask(
            task_id, envelope.user_id, CancellationToken(), notification_sink, loop,
            created_at, asyncio.Lock(), asyncio.Event()
        )
        self._active[task_id] = active
        try:
            active.cancel_monitor = asyncio.create_task(self._monitor_cancel(active))
            # Gives the owner a real queued-state cancellation point before work starts.
            await asyncio.sleep(0)
            if active.token.cancelled or self._is_cancel_requested(task_id):
                self._transition_cancelled(task_id)
                raise asyncio.CancelledError()
            self._repository.transition_task(
                task_id, "queued", "running", "running", now=self._clock(),
                require_not_cancelled=True,
            )
            bind_task = getattr(notification_sink, "bind_task", None)
            if callable(bind_task):
                active.notification_sink = bind_task(task_id)
            active.ack_timer = asyncio.create_task(self._send_ack(active))
            active.operation = asyncio.create_task(operation(task_id, active.token))
            result = await active.operation
            active.token.raise_if_cancelled()
            if isinstance(result, DeferredTaskResult):
                return result.value
            self._repository.transition_task(
                task_id, "running", "succeeded", "succeeded", now=self._clock(),
                require_not_cancelled=True,
            )
            return result
        except asyncio.CancelledError:
            if active.token.cancelled or self._is_cancel_requested(task_id):
                self._transition_cancelled(task_id)
            else:
                self._transition_interrupted(task_id)
            raise
        except Exception:
            if active.token.cancelled or self._is_cancel_requested(task_id):
                self._transition_cancelled(task_id)
                raise asyncio.CancelledError()
            self._transition_failed(task_id)
            raise
        finally:
            active.ack_done.set()
            await self._stop_tasks(active)
            self._active.pop(task_id, None)

    def report(self, task_id: str, stage: str, detail: str = "") -> None:
        clean_stage = self._safe_stage(stage)
        clean_detail = self._safe_detail(detail)
        if self._repository.record_task_progress(
            str(task_id), clean_stage, clean_detail, now=self._clock()
        ) is None:
            return
        active = self._active.get(str(task_id))
        if active is None:
            return
        active.pending_progress = (clean_stage, clean_detail)
        active.loop.call_soon_threadsafe(self._ensure_progress_timer, active)

    def cancel(self, task_id: str, user_id: int) -> bool:
        task = self._repository.request_cancel(str(task_id), user_id, now=self._clock())
        if task is None:
            return False
        active = self._active.get(str(task_id))
        if active is not None:
            active.loop.call_soon_threadsafe(self._signal_cancel, active)
        return True

    def _signal_cancel(self, active: _ActiveTask) -> None:
        if active.task_id not in self._active:
            return
        active.token.cancel()
        if active.operation is not None and not active.operation.done():
            active.operation.cancel()

    async def _monitor_cancel(self, active: _ActiveTask) -> None:
        while active.task_id in self._active:
            if self._is_cancel_requested(active.task_id):
                self._signal_cancel(active)
                return
            await self._cancel_sleep(self._cancel_poll_seconds)

    def _ensure_progress_timer(self, active: _ActiveTask) -> None:
        if active.task_id in self._active and (
            active.progress_timer is None or active.progress_timer.done()
        ):
            active.progress_timer = asyncio.create_task(self._flush_progress(active))

    async def _send_ack(self, active: _ActiveTask) -> None:
        try:
            due = active.created_at + timedelta(seconds=self._ack_seconds)
            delay = max(0.0, (due - self._clock()).total_seconds())
            if delay:
                await self._sleep(delay)
            async with active.notice_lock:
                if self._is_active(active.task_id):
                    await self._emit_notice(
                        active, "ack", "", "任务 #%s 已接单，正在处理。" % active.task_id
                    )
        finally:
            active.ack_done.set()

    async def _flush_progress(self, active: _ActiveTask) -> None:
        await active.ack_done.wait()
        while active.pending_progress is not None and self._is_active(active.task_id):
            async with active.notice_lock:
                task = self._repository.get_task(active.task_id)
                if task is None or task["status"] not in _ACTIVE_STATUSES:
                    return
                delay = self._notification_delay(task)
                if not delay:
                    pending = active.pending_progress
                    active.pending_progress = None
                    if pending is not None:
                        stage, detail = pending
                        await self._emit_notice(
                            active, stage, detail,
                            self._progress_notice(active.task_id, stage, detail),
                        )
                    continue
            await self._sleep(delay)

    async def _emit_notice(self, active: _ActiveTask, stage: str, detail: str, notice: str) -> bool:
        cancelled = False
        result = None
        try:
            if self._is_async_sink(active.notification_sink):
                result = await active.notification_sink(notice)
            else:
                result, cancelled = await self._call_sync_sink(
                    active.notification_sink, notice
                )
                if inspect.isawaitable(result):
                    result, awaitable_cancelled = await self._await_returned_awaitable(result)
                    cancelled = awaitable_cancelled or cancelled
        except Exception:
            self._repository.record_task_notification_failure(active.task_id, now=self._clock())
            return False
        if isinstance(result, NotificationDeliveryOutcome):
            if cancelled:
                raise asyncio.CancelledError()
            if result.audit_deferred:
                return result.delivered
            result = result.delivered
        if result is False:
            if cancelled:
                raise asyncio.CancelledError()
            return False
        self._repository.record_task_notification_sent(
            active.task_id, stage, detail, now=self._clock()
        )
        if cancelled:
            raise asyncio.CancelledError()
        return True

    async def _call_sync_sink(self, sink, notice: str):
        worker = asyncio.create_task(asyncio.to_thread(sink, notice))
        cancelled = False
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
            result = await asyncio.shield(worker)
        return result, cancelled

    @staticmethod
    async def _await_returned_awaitable(result):
        task = asyncio.ensure_future(result)
        cancelled = False
        try:
            value = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            value = await asyncio.shield(task)
        return value, cancelled

    async def _stop_tasks(self, active: _ActiveTask) -> None:
        tasks = [
            task for task in (
                active.operation, active.ack_timer, active.progress_timer,
                active.cancel_monitor,
            ) if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _is_active(self, task_id: str) -> bool:
        task = self._repository.get_task(task_id)
        return task is not None and task["status"] in _ACTIVE_STATUSES

    def _is_cancel_requested(self, task_id: str) -> bool:
        task = self._repository.get_task(task_id)
        return task is not None and bool(task["cancel_requested"])

    def _notification_delay(self, task) -> float:
        timestamp = task.get("last_notified_at")
        if not timestamp:
            return 0.0
        due = datetime.fromisoformat(str(timestamp)) + timedelta(
            seconds=self._progress_interval_seconds
        )
        return max(0.0, (due - self._clock()).total_seconds())

    def _transition_cancelled(self, task_id: str) -> None:
        self._transition(task_id, "cancelled", "cancelled")

    def _transition_interrupted(self, task_id: str) -> None:
        task = self._repository.get_task(task_id)
        if task is not None and task["stage"] == "delivery_committed":
            return
        if task is not None and task["status"] == "queued":
            self._transition(task_id, "cancelled", "interrupted", "任务已中断")
            return
        self._transition(task_id, "failed", "interrupted", "任务已中断")

    def _transition_failed(self, task_id: str) -> None:
        task = self._repository.get_task(task_id)
        if task is not None and task["stage"] == "delivery_committed":
            return
        self._transition(task_id, "failed", "failed", "任务执行失败")

    def _transition(self, task_id: str, target: str, stage: str, detail: str = "") -> None:
        task = self._repository.get_task(task_id)
        if task is None or task["status"] not in _ACTIVE_STATUSES:
            return
        try:
            self._repository.transition_task(
                task_id, str(task["status"]), target, stage, detail, now=self._clock()
            )
        except ValueError:
            pass

    @staticmethod
    def _is_async_sink(sink) -> bool:
        return inspect.iscoroutinefunction(sink) or inspect.iscoroutinefunction(
            getattr(sink, "__call__", None)
        )

    @staticmethod
    def _safe_stage(stage: str) -> str:
        value = str(stage).strip()
        return value if _STAGE_PATTERN.match(value) else "working"

    @staticmethod
    def _safe_detail(detail: str) -> str:
        value = str(detail).strip()
        return value if _DETAIL_PATTERN.match(value) else ""

    @staticmethod
    def _progress_notice(task_id: str, stage: str, detail: str) -> str:
        return "任务 #%s：%s%s。" % (
            task_id, stage, "" if not detail else "，%s" % detail
        )


__all__ = [
    "CancellationToken",
    "DeferredTaskResult",
    "LongTaskService",
    "NotificationDeliveryOutcome",
]
