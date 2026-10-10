import tempfile
import unittest
from pathlib import Path


class E3PersonaTests(unittest.TestCase):
    def test_rule_provider_keeps_anchors_and_deduplicates_style_feedback(self):
        from qq_openclaw_bridge.persona import PersonaRuleProvider

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "STYLE.md").write_text(
                "# STYLE\n\n## 用户反馈沉淀\n"
                "- 2026-07-17: 用户反馈「少说客服套话」；处理方式：少说客服套话。\n"
                "- 2026-07-17: 用户反馈「少说客服套话」；处理方式：少说客服套话。\n",
                encoding="utf-8",
            )

            rules = PersonaRuleProvider(workspace, max_rules=20)()

            self.assertTrue(any("chichu" in rule for rule in rules))
            self.assertTrue(any("本地 QQ 私聊管家" in rule for rule in rules))
            self.assertEqual(sum("少说客服套话" in rule for rule in rules), 1)

    def test_e3_install_compacts_agents_and_soul_but_preserves_style(self):
        from qq_openclaw_bridge.persona import install_e3_persona_core

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            backup = Path(temp_dir) / "backup"
            workspace.mkdir()
            old_agents = "# AGENTS\n" + ("重复操作规则\n" * 500)
            old_soul = "# SOUL\n" + ("重复人格规则\n" * 300)
            old_style = "# STYLE\n\n## 用户反馈沉淀\n- 少说客服话\n"
            (workspace / "AGENTS.md").write_text(old_agents, encoding="utf-8")
            (workspace / "SOUL.md").write_text(old_soul, encoding="utf-8")
            (workspace / "STYLE.md").write_text(old_style, encoding="utf-8")

            install_e3_persona_core(workspace, backup_dir=backup)

            agents = (workspace / "AGENTS.md").read_text(encoding="utf-8")
            soul = (workspace / "SOUL.md").read_text(encoding="utf-8")
            self.assertLess(len(agents), len(old_agents))
            self.assertLess(len(soul), len(old_soul))
            self.assertIn("SQLite", agents)
            self.assertIn("chichu", soul)
            self.assertIn("先办事", soul)
            self.assertEqual((workspace / "STYLE.md").read_text(encoding="utf-8"), old_style)
            self.assertEqual((backup / "AGENTS.md.bak").read_text(encoding="utf-8"), old_agents)
            self.assertEqual((backup / "SOUL.md.bak").read_text(encoding="utf-8"), old_soul)

    def test_persona_contract_rejects_oily_or_customer_service_drift(self):
        from qq_openclaw_bridge.persona import evaluate_persona_reply

        healthy = evaluate_persona_reply(
            "我是 chichu，你本地的 QQ 私聊管家。少废话，事情交给我收拾。"
        )
        customer_service = evaluate_persona_reply(
            "您好，我是 AI 助手，很高兴为您服务，请问还有什么需要？"
        )
        oily = evaluate_persona_reply("宝贝主人，人家会永远宠着你哦。")

        self.assertTrue(healthy["accepted"])
        self.assertFalse(customer_service["accepted"])
        self.assertIn("客服腔", customer_service["violations"])
        self.assertFalse(oily["accepted"])
        self.assertIn("油腻暧昧", oily["violations"])


if __name__ == "__main__":
    unittest.main()

