from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class IdentityTests(unittest.TestCase):
    def test_replies_to_who_are_you(self):
        from qq_openclaw_bridge.identity import identity_reply

        reply = identity_reply("你是谁")

        self.assertIsNotNone(reply)
        self.assertIn("chichu", reply)
        self.assertIn("本地 QQ 私聊管家", reply)
        self.assertIn("收拾", reply)

    def test_replies_to_self_intro(self):
        from qq_openclaw_bridge.identity import identity_reply

        reply = identity_reply("介绍一下你自己")

        self.assertIsNotNone(reply)
        self.assertIn("chichu", reply)
        self.assertIn("管家", reply)

    def test_replies_to_chichu_question(self):
        from qq_openclaw_bridge.identity import identity_reply

        reply = identity_reply("chichu 是谁")

        self.assertIsNotNone(reply)
        self.assertIn("chichu", reply)
        self.assertIn("别太离谱", reply)

    def test_ignores_normal_messages(self):
        from qq_openclaw_bridge.identity import identity_reply

        self.assertIsNone(identity_reply("帮我写一个 Python 脚本"))


if __name__ == "__main__":
    unittest.main()
