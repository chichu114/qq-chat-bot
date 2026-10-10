from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ReminderStoreTests(unittest.TestCase):
    def test_collects_due_reminders_and_marks_them_sent(self):
        from qq_openclaw_bridge.reminders import ReminderStore

        with tempfile.TemporaryDirectory() as temp_dir:
            todo_path = Path(temp_dir) / "TODO.md"
            todo_path.write_text(
                "\n".join(
                    [
                        "# chichu TODO",
                        "",
                        "## 待办",
                        "- [ ] 1 | 2026-07-11 | 提醒：今天晚上；事项：检查 API 额度 | user=10001 | due=2026-07-11T20:00:00+08:00 | reminded=",
                        "- [ ] 2 | 2026-07-11 | 提醒：明天晚上；事项：重启服务 | user=10001 | due=2026-07-12T20:00:00+08:00 | reminded=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            now = datetime(2026, 7, 11, 20, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
            store = ReminderStore(todo_path)

            reminders = store.collect_due(now)

            self.assertEqual(len(reminders), 1)
            self.assertEqual(reminders[0].todo_id, 1)
            self.assertEqual(reminders[0].user_id, 10001)
            self.assertIn("检查 API 额度", reminders[0].message)

            store.mark_sent(reminders[0], now)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("reminded=2026-07-11T20:01:00+08:00", content)
            self.assertIn("due=2026-07-12T20:00:00+08:00 | reminded=", content)

    def test_skips_already_reminded_items(self):
        from qq_openclaw_bridge.reminders import ReminderStore

        with tempfile.TemporaryDirectory() as temp_dir:
            todo_path = Path(temp_dir) / "TODO.md"
            todo_path.write_text(
                "- [ ] 1 | 2026-07-11 | 提醒：今天晚上；事项：检查 API 额度 | user=10001 | due=2026-07-11T20:00:00+08:00 | reminded=2026-07-11T20:01:00+08:00\n",
                encoding="utf-8",
            )
            now = datetime(2026, 7, 11, 20, 2, tzinfo=ZoneInfo("Asia/Shanghai"))

            reminders = ReminderStore(todo_path).collect_due(now)

            self.assertEqual(reminders, [])

    def test_skips_due_reminders_while_paused(self):
        from qq_openclaw_bridge.reminders import ReminderStore

        with tempfile.TemporaryDirectory() as temp_dir:
            todo_path = Path(temp_dir) / "TODO.md"
            state_path = Path(temp_dir) / "REMINDER_STATE.md"
            todo_path.write_text(
                "- [ ] 1 | 2026-07-11 | 提醒：今天晚上；事项：检查 API 额度 | user=10001 | due=2026-07-11T20:00:00+08:00 | reminded=\n",
                encoding="utf-8",
            )
            state_path.write_text("status=paused\n", encoding="utf-8")
            now = datetime(2026, 7, 11, 20, 2, tzinfo=ZoneInfo("Asia/Shanghai"))

            reminders = ReminderStore(todo_path, state_path=state_path).collect_due(now)

            self.assertEqual(reminders, [])

    def test_recurring_daily_reminder_advances_after_mark_sent(self):
        from qq_openclaw_bridge.reminders import ReminderStore

        with tempfile.TemporaryDirectory() as temp_dir:
            todo_path = Path(temp_dir) / "TODO.md"
            todo_path.write_text(
                "- [ ] 1 | 2026-07-11 | 提醒：每天9点；事项：喝水 | user=10001 | due=2026-07-11T09:00:00+08:00 | reminded= | repeat=daily | repeat_time=09:00\n",
                encoding="utf-8",
            )
            now = datetime(2026, 7, 11, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
            store = ReminderStore(todo_path)

            reminders = store.collect_due(now)
            store.mark_sent(reminders[0], now)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("due=2026-07-12T09:00:00+08:00", content)
            self.assertIn("reminded= | repeat=daily", content)

    def test_skips_due_reminders_during_quiet_hours_and_collects_afterward(self):
        from qq_openclaw_bridge.reminders import ReminderStore

        with tempfile.TemporaryDirectory() as temp_dir:
            todo_path = Path(temp_dir) / "TODO.md"
            todo_path.write_text(
                "- [ ] 1 | 2026-07-11 | 提醒：今天早上；事项：检查 API 额度 | user=10001 | due=2026-07-11T07:00:00+08:00 | reminded=\n",
                encoding="utf-8",
            )
            store = ReminderStore(
                todo_path,
                quiet_hours_start="00:00",
                quiet_hours_end="08:30",
            )
            quiet_now = datetime(2026, 7, 11, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            awake_now = datetime(2026, 7, 11, 8, 31, tzinfo=ZoneInfo("Asia/Shanghai"))

            self.assertEqual(store.collect_due(quiet_now), [])
            reminders = store.collect_due(awake_now)

            self.assertEqual(len(reminders), 1)
            self.assertEqual(reminders[0].todo_id, 1)

    def test_collects_daily_briefing_once_per_day(self):
        from qq_openclaw_bridge.reminders import ReminderStore

        with tempfile.TemporaryDirectory() as temp_dir:
            todo_path = Path(temp_dir) / "TODO.md"
            state_path = Path(temp_dir) / "REMINDER_STATE.md"
            todo_path.write_text(
                "\n".join(
                    [
                        "- [ ] 1 | 2026-07-11 | 写周报 | user=10001 | category=工作",
                        "- [ ] 2 | 2026-07-11 | 提醒：今天晚上；事项：检查 API 额度 | user=10001 | due=2026-07-11T20:00:00+08:00 | reminded=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            store = ReminderStore(
                todo_path,
                state_path=state_path,
                proactive_user_ids=[10001],
                daily_briefing_time="09:00",
            )
            now = datetime(2026, 7, 11, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

            reminders = store.collect_due(now)

            briefing = [item for item in reminders if item.kind == "daily_briefing"]
            self.assertEqual(len(briefing), 1)
            self.assertEqual(briefing[0].user_id, 10001)
            self.assertIn("chichu 早报", briefing[0].message)
            self.assertIn("写周报", briefing[0].message)
            self.assertIn("检查 API 额度", briefing[0].message)

            store.mark_sent(briefing[0], now)

            self.assertIn(
                "daily_briefing_sent=2026-07-11",
                state_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                [item for item in store.collect_due(now) if item.kind == "daily_briefing"],
                [],
            )

    def test_collects_evening_review_once_per_day(self):
        from qq_openclaw_bridge.reminders import ReminderStore

        with tempfile.TemporaryDirectory() as temp_dir:
            todo_path = Path(temp_dir) / "TODO.md"
            state_path = Path(temp_dir) / "REMINDER_STATE.md"
            todo_path.write_text(
                "- [ ] 1 | 2026-07-11 | 写周报 | user=10001 | category=工作\n",
                encoding="utf-8",
            )
            store = ReminderStore(
                todo_path,
                state_path=state_path,
                proactive_user_ids=[10001],
                evening_review_time="23:00",
            )
            now = datetime(2026, 7, 11, 23, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

            reminders = store.collect_due(now)

            review = [item for item in reminders if item.kind == "evening_review"]
            self.assertEqual(len(review), 1)
            self.assertIn("晚间复盘", review[0].message)
            self.assertIn("1 条待办", review[0].message)

            store.mark_sent(review[0], now)

            self.assertIn(
                "evening_review_sent=2026-07-11",
                state_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
