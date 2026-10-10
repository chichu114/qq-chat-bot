from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class OneBotParsingTests(unittest.TestCase):
    def test_extracts_text_from_string_message(self):
        from qq_openclaw_bridge.onebot import extract_text

        self.assertEqual(extract_text("  你好 OpenClaw  "), "你好 OpenClaw")

    def test_extracts_text_from_segment_array(self):
        from qq_openclaw_bridge.onebot import extract_text

        message = [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": "  你好"}},
            {"type": "image", "data": {"file": "a.png"}},
            {"type": "text", "data": {"text": " 世界  "}},
        ]

        self.assertEqual(extract_text(message), "你好 世界")

    def test_returns_none_for_unsupported_message(self):
        from qq_openclaw_bridge.onebot import extract_text

        self.assertIsNone(extract_text([{"type": "image", "data": {"file": "a.png"}}]))

    def test_builds_send_private_msg_action(self):
        from qq_openclaw_bridge.onebot import build_send_private_msg

        action = build_send_private_msg(10001, "你好", echo="reply-1")

        self.assertEqual(action["action"], "send_private_msg")
        self.assertEqual(action["params"], {"user_id": 10001, "message": "你好"})
        self.assertEqual(action["echo"], "reply-1")

    def test_group_action_is_separate_from_private_action(self):
        from qq_openclaw_bridge.onebot import build_send_group_msg

        action = build_send_group_msg(77, "群回复", echo="group-1")

        self.assertEqual(action["action"], "send_group_msg")
        self.assertEqual(action["params"], {"group_id": 77, "message": "群回复"})
        self.assertEqual(action["echo"], "group-1")


class FilteringTests(unittest.TestCase):
    def test_accepts_whitelisted_private_text(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": 777,
            "message": "你好",
            "time": 123456,
        }

        decision = evaluate_event(event, {10001}, RateLimiter(10, window_seconds=60))

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, "accepted")
        self.assertEqual(decision.request.user_id, 10001)
        self.assertEqual(decision.request.message_id, "777")
        self.assertEqual(decision.request.text, "你好")
        self.assertEqual(decision.request.timestamp, 123456.0)

    def test_ignores_group_messages(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 10001,
            "message": "你好",
        }

        decision = evaluate_event(event, {10001}, RateLimiter(10, window_seconds=60))

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ignored_non_private")
        self.assertIsNone(decision.request)

    def test_ignores_non_whitelisted_users(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 20002,
            "message": "你好",
        }

        decision = evaluate_event(event, {10001}, RateLimiter(10, window_seconds=60))

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ignored_not_allowed")

    def test_rejects_rate_limited_user(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        limiter = RateLimiter(1, window_seconds=60)
        first = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": 1,
            "message": "第一条",
            "time": 10,
        }
        second = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": 2,
            "message": "第二条",
            "time": 11,
        }

        self.assertTrue(evaluate_event(first, {10001}, limiter).accepted)
        decision = evaluate_event(second, {10001}, limiter)

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "rate_limited")


if __name__ == "__main__":
    unittest.main()
