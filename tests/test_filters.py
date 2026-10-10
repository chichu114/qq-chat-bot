from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def private_event(message, message_id=1, timestamp=10):
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "message_id": message_id,
        "message": message,
        "time": timestamp,
    }


class InboundFilteringTests(unittest.TestCase):
    def test_group_event_is_rejected_before_an_envelope_is_returned(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        event = private_event("private text")
        event["message_type"] = "group"

        decision = evaluate_event(event, {10001}, RateLimiter(1, window_seconds=60))

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ignored_non_private")
        self.assertIsNone(decision.envelope)

    def test_non_whitelisted_event_is_rejected_before_an_envelope_is_returned(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        decision = evaluate_event(
            private_event("private text"), {20002}, RateLimiter(1, window_seconds=60)
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ignored_not_allowed")
        self.assertIsNone(decision.envelope)

    def test_pure_text_keeps_legacy_request_values_and_exposes_envelope(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        decision = evaluate_event(
            private_event("  你好  ", message_id=7, timestamp=20),
            {10001},
            RateLimiter(1, window_seconds=60),
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.request.user_id, 10001)
        self.assertEqual(decision.request.message_id, "7")
        self.assertEqual(decision.request.text, "你好")
        self.assertEqual(decision.request.timestamp, 20.0)
        self.assertEqual(decision.envelope.text, "你好")

    def test_image_only_message_uses_existing_notice_path_when_media_is_disabled(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        decision = evaluate_event(
            private_event([{"type": "image", "data": {"file": "a.png"}}]),
            {10001},
            RateLimiter(1, window_seconds=60),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "unsupported_message")
        self.assertIsNotNone(decision.envelope)

    def test_image_only_message_is_accepted_without_agent_request_when_media_is_enabled(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        decision = evaluate_event(
            private_event([{"type": "image", "data": {"file": "a.png"}}]),
            {10001},
            RateLimiter(1, window_seconds=60),
            enable_media_pipeline=True,
        )

        self.assertTrue(decision.accepted)
        self.assertIsNone(decision.request)
        self.assertEqual(decision.envelope.attachments[0].kind, "image")

    def test_media_messages_are_rate_limited_when_media_is_enabled(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        limiter = RateLimiter(1, window_seconds=60)
        message = [{"type": "image", "data": {"file": "a.png"}}]

        first = evaluate_event(
            private_event(message, message_id=1, timestamp=10),
            {10001},
            limiter,
            enable_media_pipeline=True,
        )
        second = evaluate_event(
            private_event(message, message_id=2, timestamp=11),
            {10001},
            limiter,
            enable_media_pipeline=True,
        )

        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "rate_limited")

    def test_filtering_uses_the_same_strict_user_id_rules_as_the_parser(self):
        from qq_openclaw_bridge.filters import evaluate_event
        from qq_openclaw_bridge.rate_limit import RateLimiter

        self.assertTrue(
            evaluate_event(
                private_event("hello") | {"user_id": "10001"},
                {10001},
                RateLimiter(1, window_seconds=60),
            ).accepted
        )

        for value in (True, 10001.0, 10001.9, -10001, "10001.0", " 10001 "):
            with self.subTest(value=value):
                decision = evaluate_event(
                    private_event("hello") | {"user_id": value},
                    {10001},
                    RateLimiter(1, window_seconds=60),
                )
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, "ignored_missing_user")

    def test_quote_only_message_is_accepted_with_empty_request_when_quote_path_enabled(self):
        from qq_openclaw_bridge.filters import evaluate_event

        limiter = _AllowSpy()
        decision = evaluate_event(
            private_event([{"type": "reply", "data": {"id": "900"}}]),
            {10001},
            limiter,
            enable_quote_resolution=True,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, "accepted")
        self.assertIsNotNone(decision.envelope.quote)
        self.assertIsNotNone(decision.request)
        self.assertEqual(decision.request.text, "")
        self.assertEqual(limiter.calls, ["10001"])

    def test_quote_only_message_stays_unsupported_when_quote_path_disabled(self):
        from qq_openclaw_bridge.filters import evaluate_event

        limiter = _AllowSpy()
        decision = evaluate_event(
            private_event([{"type": "reply", "data": {"id": "900"}}]),
            {10001},
            limiter,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "unsupported_message")
        self.assertEqual(limiter.calls, [])

    def test_quote_path_does_not_accept_image_only_without_media_pipeline(self):
        from qq_openclaw_bridge.filters import evaluate_event

        decision = evaluate_event(
            private_event([{"type": "image", "data": {"file": "a.png"}}]),
            {10001},
            _AllowSpy(),
            enable_quote_resolution=True,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "unsupported_message")

    def test_second_valid_reply_segment_is_unsupported_without_consuming_rate_limit(self):
        from qq_openclaw_bridge.filters import evaluate_event

        limiter = _AllowSpy()
        decision = evaluate_event(
            private_event(
                [
                    {"type": "reply", "data": {"id": "900"}},
                    {"type": "text", "data": {"text": "do not drop this"}},
                    {"type": "reply", "data": {"message_id": "901"}},
                ]
            ),
            {10001},
            limiter,
            enable_media_pipeline=True,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "unsupported_message")
        self.assertIsNone(decision.envelope)
        self.assertEqual(limiter.calls, [])

    def test_unknown_only_message_is_unsupported_without_consuming_rate_limit(self):
        from qq_openclaw_bridge.filters import evaluate_event

        limiter = _AllowSpy()
        decision = evaluate_event(
            private_event([{"type": "poke"}]),
            {10001},
            limiter,
            enable_media_pipeline=True,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "unsupported_message")
        self.assertEqual(decision.envelope.attachments[0].kind, "unsupported")
        self.assertEqual(limiter.calls, [])

    def test_only_supported_media_kinds_accept_attachment_only_messages(self):
        from qq_openclaw_bridge.filters import evaluate_event

        for segment_type in ("image", "record", "file"):
            with self.subTest(segment_type=segment_type):
                decision = evaluate_event(
                    private_event([{"type": segment_type, "data": {"file": "item"}}]),
                    {10001},
                    _AllowSpy(),
                    enable_media_pipeline=True,
                )
                self.assertTrue(decision.accepted)
                self.assertIsNone(decision.request)

    def test_rate_limiter_uses_local_time_even_for_future_event_timestamps(self):
        from qq_openclaw_bridge.filters import evaluate_event

        limiter = _AllowSpy()
        decision = evaluate_event(
            private_event("hello", timestamp=9876543210.0),
            {10001},
            limiter,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(limiter.calls, ["10001"])

    def test_read_only_duplicate_check_runs_before_rate_mutation(self):
        from qq_openclaw_bridge.filters import evaluate_event

        limiter = _AllowSpy()
        checked = []
        decision = evaluate_event(
            private_event("hello", message_id=7),
            {10001},
            limiter,
            duplicate_checker=lambda message_id: checked.append(message_id) or True,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "duplicate_message")
        self.assertEqual(checked, ["7"])
        self.assertEqual(limiter.calls, [])


class _AllowSpy:
    def __init__(self):
        self.calls = []

    def allow(self, key):
        self.calls.append(key)
        return True


if __name__ == "__main__":
    unittest.main()
