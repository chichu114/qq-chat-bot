import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional


class ReminderPushService:
    def __init__(
        self,
        reminder_store: Optional[object],
        state_store: Optional[object],
        sender: Callable[..., Awaitable[None]],
        deliver_once: Optional[Callable[[object], Awaitable[bool]]],
        now_provider: Callable[[], datetime],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._reminder_store = reminder_store
        self._state_store = state_store
        self._sender = sender
        self._deliver_once = deliver_once
        self._now_provider = now_provider
        self._logger = logger or logging.getLogger("qq_openclaw_bridge")

    async def run_once(
        self, websocket, now: Optional[datetime] = None
    ) -> None:
        if self._reminder_store is None:
            return

        current_time = self._now_provider() if now is None else now
        reminders = self._reminder_store.collect_due(current_time)
        for reminder in reminders:
            sent = await self._deliver_reminder(
                websocket, reminder, current_time
            )
            if not sent:
                continue
            self._reminder_store.mark_sent(reminder, current_time)
            self._logger.info(
                "reminder pushed: notification_id=%s user_id=%s",
                getattr(
                    reminder,
                    "notification_id",
                    getattr(reminder, "todo_id", "item"),
                ),
                reminder.user_id,
            )

    async def _deliver_reminder(
        self, websocket, reminder, current_time: datetime
    ) -> bool:
        if self._state_store is None:
            await self._sender(websocket, reminder.user_id, reminder.message)
            return True

        due_at = getattr(reminder, "due_at", current_time)
        due_key = (
            due_at.isoformat(timespec="seconds")
            if isinstance(due_at, datetime)
            else str(due_at)
        )
        notification_id = getattr(
            reminder,
            "notification_id",
            getattr(reminder, "todo_id", "item"),
        )
        dedupe_key = "reminder:%s:%s:%s:%s" % (
            reminder.user_id,
            getattr(reminder, "kind", "todo"),
            notification_id,
            due_key,
        )
        row = self._state_store.enqueue_outbox(
            kind="reminder",
            user_id=reminder.user_id,
            message=reminder.message,
            dedupe_key=dedupe_key,
        )
        if row["status"] != "sent":
            assert self._deliver_once is not None
            await self._deliver_once(websocket)
            row = self._state_store.get_outbox(dedupe_key)
        return row is not None and row["status"] == "sent"

    async def loop(self, websocket, interval_seconds: int) -> None:
        while True:
            try:
                await self.run_once(websocket)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("reminder push failed")
            await asyncio.sleep(interval_seconds)


class BackupService:
    def __init__(
        self,
        backup_manager: Optional[object],
        enabled: bool,
        interval_hours: int,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._backup_manager = backup_manager
        self._enabled = enabled
        self._interval_hours = interval_hours
        self._logger = logger or logging.getLogger("qq_openclaw_bridge")

    @property
    def enabled(self) -> bool:
        return self._backup_manager is not None and self._enabled

    def run_once(self):
        if not self.enabled:
            return None
        created = self._backup_manager.run_if_due()
        if created is not None:
            self._logger.info("automatic backup created: %s", created["name"])
        return created

    async def loop(self) -> None:
        check_interval = min(3600, max(60, self._interval_hours * 3600))
        while True:
            try:
                self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("automatic backup failed")
            await asyncio.sleep(check_interval)
