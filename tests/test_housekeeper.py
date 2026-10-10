from datetime import date, datetime
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class HousekeeperTests(unittest.TestCase):
    def _housekeeper(self, memory_path, todo_path):
        from qq_openclaw_bridge.housekeeper import Housekeeper

        return Housekeeper(
            memory_path=memory_path,
            todo_path=todo_path,
            date_provider=lambda: date(2026, 7, 11),
        )

    def _housekeeper_at(self, memory_path, todo_path, now):
        from qq_openclaw_bridge.housekeeper import Housekeeper

        return Housekeeper(
            memory_path=memory_path,
            todo_path=todo_path,
            date_provider=lambda: now.date(),
            now_provider=lambda: now,
        )

    def test_records_long_term_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            reply = housekeeper.handle("记住：我不喜欢长篇解释")

            content = memory_path.read_text(encoding="utf-8")
            self.assertIn("记下了", reply)
            self.assertIn("## 长期记忆", content)
            self.assertIn("2026-07-11", content)
            self.assertIn("我不喜欢长篇解释", content)

    def test_lists_long_term_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("记住：我喜欢先给结论")
            reply = housekeeper.handle("我的记忆")

            self.assertIn("1.", reply)
            self.assertIn("我喜欢先给结论", reply)

    def test_does_not_duplicate_same_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("记住：我喜欢短回复")
            housekeeper.handle("记住：我喜欢短回复")

            content = memory_path.read_text(encoding="utf-8")
            self.assertEqual(content.count("我喜欢短回复"), 1)

    def test_lists_memories_with_view_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("记住：我不吃香菜")
            reply = housekeeper.handle("查看记忆")

            self.assertIn("1. 我不吃香菜", reply)

    def test_searches_memories_by_keyword(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("记住：我不吃香菜")
            housekeeper.handle("记住：项目使用 OpenAI API")
            reply = housekeeper.handle("搜索记忆 api")

            self.assertIn("项目使用 OpenAI API", reply)
            self.assertNotIn("我不吃香菜", reply)

    def test_reports_when_memory_search_has_no_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("记住：我不吃香菜")
            reply = housekeeper.handle("搜索记忆 榴莲")

            self.assertIn("没找到", reply)

    def test_deletes_numbered_memory_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            memory_path.write_text(
                "# chichu MEMORY\n\n"
                "## 长期记忆\n"
                "- 2026-07-10: 我不吃香菜\n"
                "- 2026-07-11: 我喜欢先给结论\n\n"
                "## 其他内容\n"
                "- 这行必须保留\n",
                encoding="utf-8",
            )
            housekeeper = self._housekeeper(memory_path, todo_path)

            reply = housekeeper.handle("删除记忆 1")
            content = memory_path.read_text(encoding="utf-8")

            self.assertIn("已删除", reply)
            self.assertNotIn("我不吃香菜", content)
            self.assertIn("我喜欢先给结论", content)
            self.assertIn("## 其他内容\n- 这行必须保留", content)

    def test_rejects_invalid_memory_delete_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("记住：我不吃香菜")
            reply = housekeeper.handle("删除记忆 9")

            self.assertIn("没有第 9 条", reply)
            self.assertIn("我不吃香菜", memory_path.read_text(encoding="utf-8"))

    def test_clears_long_term_memories_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            memory_path.write_text(
                "# chichu MEMORY\n\n"
                "## 长期记忆\n"
                "- 2026-07-10: 我不吃香菜\n"
                "- 2026-07-11: 我喜欢先给结论\n\n"
                "## 其他内容\n"
                "- 这行必须保留\n",
                encoding="utf-8",
            )
            housekeeper = self._housekeeper(memory_path, todo_path)

            reply = housekeeper.handle("清空记忆")
            content = memory_path.read_text(encoding="utf-8")

            self.assertIn("已清空 2 条", reply)
            self.assertNotIn("我不吃香菜", content)
            self.assertNotIn("我喜欢先给结论", content)
            self.assertIn("## 其他内容\n- 这行必须保留", content)

    def test_records_todo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            reply = housekeeper.handle("待办：检查 API 额度")

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("收下", reply)
            self.assertIn("- [ ] 1 | 2026-07-11 | 检查 API 额度", content)

    def test_records_categorized_todo_from_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            reply = housekeeper.handle("工作待办：写周报")

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("收下", reply)
            self.assertIn("写周报", content)
            self.assertIn("category=工作", content)

    def test_records_categorized_todo_from_inline_category(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("待办：项目：修启动按钮")

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("修启动按钮", content)
            self.assertIn("category=项目", content)

    def test_records_categorized_todo_from_hash_tag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("待办：背单词 #学习")

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("背单词", content)
            self.assertNotIn("#学习", content)
            self.assertIn("category=学习", content)

    def test_records_reminder_text_as_todo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            reply = housekeeper.handle("明天晚上提醒我检查 API 额度", user_id=10001)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("提醒", reply)
            self.assertIn("明天晚上", content)
            self.assertIn("检查 API 额度", content)
            self.assertIn("due=2026-07-12T20:00:00+08:00", content)
            self.assertIn("user=10001", content)
            self.assertIn("reminded=", content)

    def test_records_absolute_datetime_reminder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            reply = housekeeper.handle("2026-07-11 21:30提醒我检查 API 额度", user_id=10001)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("21:30", reply)
            self.assertIn("due=2026-07-11T21:30:00+08:00", content)
            self.assertIn("user=10001", content)

    def test_records_daily_recurring_reminder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            reply = housekeeper.handle("每天9点提醒我喝水", user_id=10001)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("提醒定好了", reply)
            self.assertIn("喝水", content)
            self.assertIn("due=2026-07-12T09:00:00+08:00", content)
            self.assertIn("repeat=daily", content)
            self.assertIn("repeat_time=09:00", content)

    def test_records_weekly_recurring_reminder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("每周一 9:30提醒我开周会", user_id=10001)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("开周会", content)
            self.assertIn("due=2026-07-13T09:30:00+08:00", content)
            self.assertIn("repeat=weekly", content)
            self.assertIn("repeat_weekday=1", content)
            self.assertIn("repeat_time=09:30", content)

    def test_records_monthly_recurring_reminder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("每月1号 9:00提醒我交房租", user_id=10001)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("交房租", content)
            self.assertIn("due=2026-08-01T09:00:00+08:00", content)
            self.assertIn("repeat=monthly", content)
            self.assertIn("repeat_month_day=1", content)
            self.assertIn("repeat_time=09:00", content)

    def test_lists_active_todos(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("待办：重启桥接服务")
            reply = housekeeper.handle("待办列表")

            self.assertIn("1.", reply)
            self.assertIn("重启桥接服务", reply)

    def test_lists_todos_with_categories_and_filters_by_category(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("工作待办：写周报")
            housekeeper.handle("生活待办：买牛奶")

            all_reply = housekeeper.handle("待办列表")
            filtered_reply = housekeeper.handle("待办列表 工作")

            self.assertIn("[工作]", all_reply)
            self.assertIn("[生活]", all_reply)
            self.assertIn("写周报", filtered_reply)
            self.assertNotIn("买牛奶", filtered_reply)

    def test_completes_todo_by_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("待办：重启桥接服务")
            reply = housekeeper.handle("完成待办 1")

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("处理掉", reply)
            self.assertIn("- [x] 1 | 2026-07-11 | 重启桥接服务", content)

    def test_deletes_todo_by_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("待办：检查 API 额度")
            housekeeper.handle("待办：重启桥接服务")
            reply = housekeeper.handle("删除待办 1")

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("删掉", reply)
            self.assertNotIn("检查 API 额度", content)
            self.assertIn("重启桥接服务", content)

    def test_modifies_todo_by_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("待办：检查 API 额度")
            reply = housekeeper.handle("修改待办 1：明天晚上提醒我重启服务", user_id=10001)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("改好了", reply)
            self.assertNotIn("检查 API 额度", content)
            self.assertIn("- [ ] 1 | 2026-07-11 | 提醒：明天晚上；事项：重启服务", content)
            self.assertIn("due=2026-07-12T20:00:00+08:00", content)
            self.assertIn("user=10001", content)
            self.assertIn("reminded=", content)

    def test_clears_completed_todos(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            housekeeper.handle("待办：检查 API 额度")
            housekeeper.handle("待办：重启桥接服务")
            housekeeper.handle("完成待办 1")
            reply = housekeeper.handle("清空已完成")

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("清掉", reply)
            self.assertNotIn("检查 API 额度", content)
            self.assertIn("重启桥接服务", content)

    def test_lists_today_reminders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("2026-07-11 21:30提醒我检查 API 额度", user_id=10001)
            housekeeper.handle("2026-07-12 21:30提醒我重启服务", user_id=10001)
            reply = housekeeper.handle("查看今天提醒")

            self.assertIn("今天的提醒", reply)
            self.assertIn("检查 API 额度", reply)
            self.assertNotIn("重启服务", reply)

    def test_lists_reminders_for_next_seven_days_in_time_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("2026-07-14 09:00提醒我交材料", user_id=10001)
            housekeeper.handle("2026-07-12 20:00提醒我检查 API", user_id=10001)
            housekeeper.handle("2026-07-20 09:00提醒我交月报", user_id=10001)
            reply = housekeeper.handle("未来七天提醒")

            self.assertIn("未来七天", reply)
            self.assertIn("检查 API", reply)
            self.assertIn("交材料", reply)
            self.assertNotIn("交月报", reply)
            self.assertLess(reply.index("检查 API"), reply.index("交材料"))

    def test_reports_when_next_seven_days_have_no_reminders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            reply = housekeeper.handle("未来7天提醒")

            self.assertIn("没有安排", reply)

    def test_reschedules_reminder_with_natural_language(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("工作待办：写周报")
            reply = housekeeper.handle("把提醒 1 改到明天晚上", user_id=10001)
            content = todo_path.read_text(encoding="utf-8")

            self.assertIn("改期完成", reply)
            self.assertIn("提醒：明天晚上；事项：写周报", content)
            self.assertIn("due=2026-07-12T20:00:00+08:00", content)
            self.assertIn("user=10001", content)
            self.assertIn("reminded=", content)
            self.assertIn("category=工作", content)

    def test_reschedules_reminder_as_recurring(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("明天晚上提醒我喝水", user_id=10001)
            reply = housekeeper.handle("改期提醒 1 到每周一 9:30", user_id=10001)
            content = todo_path.read_text(encoding="utf-8")

            self.assertIn("改期完成", reply)
            self.assertIn("due=2026-07-13T09:30:00+08:00", content)
            self.assertIn("repeat=weekly", content)
            self.assertIn("repeat_weekday=1", content)
            self.assertIn("repeat_time=09:30", content)

    def test_reschedules_reminder_with_chinese_period_and_hour(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("待办：开会")
            housekeeper.handle("把提醒 1 改到明天上午10点", user_id=10001)

            content = todo_path.read_text(encoding="utf-8")
            self.assertIn("due=2026-07-12T10:00:00+08:00", content)

    def test_rejects_invalid_calendar_time_without_changing_todo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("明天晚上提醒我喝水", user_id=10001)
            before = todo_path.read_text(encoding="utf-8")
            reply = housekeeper.handle("把提醒 1 改到2026-99-99", user_id=10001)

            self.assertIn("没看懂时间", reply)
            self.assertEqual(before, todo_path.read_text(encoding="utf-8"))

    def test_rejects_unrecognized_reschedule_time_without_changing_todo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("明天晚上提醒我喝水", user_id=10001)
            before = todo_path.read_text(encoding="utf-8")
            reply = housekeeper.handle("把提醒 1 改到下辈子", user_id=10001)

            self.assertIn("没看懂时间", reply)
            self.assertEqual(before, todo_path.read_text(encoding="utf-8"))

    def test_cancels_reminder_but_keeps_todo_and_category(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            now = datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            housekeeper = self._housekeeper_at(memory_path, todo_path, now)

            housekeeper.handle("明天晚上提醒我写周报 #工作", user_id=10001)
            reply = housekeeper.handle("取消提醒 1")
            content = todo_path.read_text(encoding="utf-8")

            self.assertIn("待办还保留", reply)
            self.assertIn("写周报", content)
            self.assertIn("category=工作", content)
            self.assertNotIn("due=", content)
            self.assertNotIn("reminded=", content)
            self.assertNotIn("user=", content)
            self.assertNotIn("提醒：明天晚上", content)

    def test_pauses_and_resumes_reminders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            state_path = Path(temp_dir) / "REMINDER_STATE.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            pause_reply = housekeeper.handle("暂停提醒")
            paused_content = state_path.read_text(encoding="utf-8")
            resume_reply = housekeeper.handle("恢复提醒")
            resumed_content = state_path.read_text(encoding="utf-8")

            self.assertIn("暂停", pause_reply)
            self.assertIn("status=paused", paused_content)
            self.assertIn("恢复", resume_reply)
            self.assertIn("status=active", resumed_content)

    def test_ignores_normal_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "MEMORY.md"
            todo_path = Path(temp_dir) / "TODO.md"
            housekeeper = self._housekeeper(memory_path, todo_path)

            reply = housekeeper.handle("帮我解释一下 Python 装饰器")

            self.assertIsNone(reply)
            self.assertFalse(memory_path.exists())
            self.assertFalse(todo_path.exists())


if __name__ == "__main__":
    unittest.main()
