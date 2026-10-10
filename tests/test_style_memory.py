from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class StyleMemoryTests(unittest.TestCase):
    def _memory(self, path):
        from qq_openclaw_bridge.style_memory import StyleMemory

        return StyleMemory(path, date_provider=lambda: date(2026, 7, 11))

    def _style_with_feedback(self):
        return (
            "# chichu STYLE\n\n"
            "## B1 风格内核\n"
            "保持管家感。\n\n"
            "## 用户反馈沉淀\n"
            "- 2026-07-11: 用户反馈「太客服了」；处理方式：避免客服腔。\n"
            "- 2026-07-12: 用户反馈「更毒舌一点」；处理方式：毒舌更锋利。\n\n"
            "## 其他规则\n"
            "这段不能被删。\n"
        )

    def assert_records_feedback(self, feedback, expected_guidance):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text("# STYLE\n", encoding="utf-8")

            reply = self._memory(style_path).record_if_feedback(feedback)

            content = style_path.read_text(encoding="utf-8")
            self.assertIsNotNone(reply)
            self.assertIn(feedback, content)
            self.assertIn(expected_guidance, content)

    def test_records_customer_service_feedback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text("# STYLE\n\n已有规则。\n", encoding="utf-8")

            reply = self._memory(style_path).record_if_feedback("刚才太客服了，少废话")

            content = style_path.read_text(encoding="utf-8")
            self.assertIsNotNone(reply)
            self.assertIn("客服", reply)
            self.assertIn("少废话", reply)
            self.assertIn("## 用户反馈沉淀", content)
            self.assertIn("2026-07-11", content)
            self.assertIn("刚才太客服了，少废话", content)
            self.assertIn("避免客服腔", content)

    def test_records_positive_style_feedback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text("# STYLE\n", encoding="utf-8")

            reply = self._memory(style_path).record_if_feedback("这个味对了，以后都这样")

            content = style_path.read_text(encoding="utf-8")
            self.assertIsNotNone(reply)
            self.assertIn("保留", reply)
            self.assertIn("这个味对了，以后都这样", content)
            self.assertIn("保留这类表达", content)

    def test_records_too_harsh_feedback(self):
        self.assert_records_feedback("刚才太凶了", "收敛毒舌")

    def test_records_too_oily_feedback(self):
        self.assert_records_feedback("太油了", "避免油腻")

    def test_records_too_cold_feedback(self):
        self.assert_records_feedback("太冷了", "增加一点温度")

    def test_records_sharper_toxic_feedback(self):
        self.assert_records_feedback("更毒舌一点", "毒舌更锋利")

    def test_records_less_self_intro_feedback(self):
        self.assert_records_feedback("少自我介绍", "减少自我介绍")

    def test_records_more_housekeeper_feedback(self):
        self.assert_records_feedback("更像管家", "强化管家感")

    def test_ignores_normal_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text("# STYLE\n", encoding="utf-8")

            reply = self._memory(style_path).record_if_feedback("帮我写一个 Python 脚本")

            self.assertIsNone(reply)
            self.assertEqual(style_path.read_text(encoding="utf-8"), "# STYLE\n")

    def test_does_not_duplicate_same_feedback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text("# STYLE\n", encoding="utf-8")
            memory = self._memory(style_path)

            memory.record_if_feedback("更御姐一点")
            memory.record_if_feedback("更御姐一点")

            content = style_path.read_text(encoding="utf-8")
            self.assertEqual(content.count("更御姐一点"), 1)

    def test_lists_style_feedback_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text(self._style_with_feedback(), encoding="utf-8")

            reply = self._memory(style_path).handle_command("查看风格")

            self.assertIn("1. 2026-07-11", reply)
            self.assertIn("太客服了", reply)
            self.assertIn("2. 2026-07-12", reply)
            self.assertIn("更毒舌一点", reply)

    def test_style_self_check_summarizes_feedback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text(self._style_with_feedback(), encoding="utf-8")

            reply = self._memory(style_path).handle_command("风格自检")

            self.assertIn("2 条", reply)
            self.assertIn("最近 2 条", reply)
            self.assertIn("更毒舌一点", reply)

    def test_deletes_numbered_style_feedback_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text(self._style_with_feedback(), encoding="utf-8")

            reply = self._memory(style_path).handle_command("删除风格 1")

            content = style_path.read_text(encoding="utf-8")
            self.assertIn("已删除", reply)
            self.assertNotIn("太客服了", content)
            self.assertIn("更毒舌一点", content)
            self.assertIn("## B1 风格内核", content)
            self.assertIn("## 其他规则", content)

    def test_rejects_invalid_style_feedback_delete_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            original = self._style_with_feedback()
            style_path.write_text(original, encoding="utf-8")

            reply = self._memory(style_path).handle_command("删除风格 99")

            self.assertIn("没有第 99 条", reply)
            self.assertEqual(style_path.read_text(encoding="utf-8"), original)

    def test_clears_style_feedback_entries_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text(self._style_with_feedback(), encoding="utf-8")

            reply = self._memory(style_path).handle_command("清空风格反馈")

            content = style_path.read_text(encoding="utf-8")
            self.assertIn("已清空", reply)
            self.assertIn("## 用户反馈沉淀", content)
            self.assertNotIn("太客服了", content)
            self.assertNotIn("更毒舌一点", content)
            self.assertIn("保持管家感", content)
            self.assertIn("这段不能被删", content)

    def test_style_command_returns_empty_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text("# STYLE\n", encoding="utf-8")

            reply = self._memory(style_path).handle_command("风格沉淀")

            self.assertIn("还没有", reply)

    def test_unknown_style_command_returns_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            style_path = Path(temp_dir) / "STYLE.md"
            style_path.write_text("# STYLE\n", encoding="utf-8")

            reply = self._memory(style_path).handle_command("帮我写代码")

            self.assertIsNone(reply)


if __name__ == "__main__":
    unittest.main()
