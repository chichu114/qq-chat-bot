from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class PersonaTests(unittest.TestCase):
    def test_install_persona_core_creates_managed_sections(self):
        from qq_openclaw_bridge.persona import install_persona_core

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)

            install_persona_core(workspace)

            soul = (workspace / "SOUL.md").read_text(encoding="utf-8")
            style = (workspace / "STYLE.md").read_text(encoding="utf-8")
            self.assertIn("## B1 人设内核", soul)
            self.assertIn("chichu", soul)
            self.assertIn("毒舌御姐", soul)
            self.assertIn("不要回复成普通 AI 助手介绍", soul)
            self.assertIn("## B1 风格内核", style)
            self.assertIn("先办事，再发挥风格", style)

    def test_install_persona_core_is_idempotent(self):
        from qq_openclaw_bridge.persona import install_persona_core

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)

            install_persona_core(workspace)
            install_persona_core(workspace)

            soul = (workspace / "SOUL.md").read_text(encoding="utf-8")
            style = (workspace / "STYLE.md").read_text(encoding="utf-8")
            self.assertEqual(soul.count("## B1 人设内核"), 1)
            self.assertEqual(style.count("## B1 风格内核"), 1)

    def test_install_persona_core_preserves_style_feedback(self):
        from qq_openclaw_bridge.persona import install_persona_core

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "STYLE.md").write_text(
                "# STYLE\n\n## 用户反馈沉淀\n- 2026-07-11: 用户反馈「太客服」；处理方式：少模板。\n",
                encoding="utf-8",
            )

            install_persona_core(workspace)

            style = (workspace / "STYLE.md").read_text(encoding="utf-8")
            self.assertIn("## B1 风格内核", style)
            self.assertIn("## 用户反馈沉淀", style)
            self.assertIn("用户反馈「太客服」", style)

    def test_apply_managed_section_replaces_existing_section(self):
        from qq_openclaw_bridge.persona import apply_managed_section

        content = "# SOUL\n\n## B1 人设内核\n旧内容\n\n## 其他\n保留\n"

        updated = apply_managed_section(content, "## B1 人设内核", "新内容")

        self.assertIn("## B1 人设内核\n新内容", updated)
        self.assertNotIn("旧内容", updated)
        self.assertIn("## 其他\n保留", updated)


if __name__ == "__main__":
    unittest.main()
