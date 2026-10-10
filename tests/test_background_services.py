from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _Reminder:
    def __init__(self):
        self.todo_id = 7
        self.notification_id = "n7"
        self.kind = "todo"
        self.user_id = 10001
        self.message = "该做事了"
        self.due_at = datetime(2026, 7, 16, 8, tzinfo=ZoneInfo("Asia/Shanghai"))


class _ReminderStore:
    def __init__(self, reminder):
        self.reminder = reminder
        self.sent = []

    def collect_due(self, now):
        return [self.reminder]

    def mark_sent(self, reminder, now):
        self.sent.append((reminder, now))


class BackgroundServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reminder_is_marked_only_after_successful_delivery(self):
        from qq_openclaw_bridge.application.background import ReminderPushService

        reminder = _Reminder()
        store = _ReminderStore(reminder)
        calls = []

        async def sender(websocket, user_id, message):
            calls.append((user_id, message))

        now = datetime(2026, 7, 16, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        service = ReminderPushService(
            store,
            state_store=None,
            sender=sender,
            deliver_once=None,
            now_provider=lambda: now,
        )
        await service.run_once("ws")

        self.assertEqual(calls, [(10001, "该做事了")])
        self.assertEqual(store.sent, [(reminder, now)])

    def test_backup_service_reports_created_backup(self):
        from qq_openclaw_bridge.application.background import BackupService

        class Manager:
            def __init__(self):
                self.calls = 0

            def run_if_due(self):
                self.calls += 1
                return {"name": "backup.zip"}

        manager = Manager()
        service = BackupService(manager, enabled=True, interval_hours=24)
        self.assertEqual(service.run_once(), {"name": "backup.zip"})
        self.assertEqual(manager.calls, 1)


if __name__ == "__main__":
    unittest.main()
