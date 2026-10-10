from pathlib import Path
import sys
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def private_event(message):
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "message_id": 42,
        "message": message,
        "time": 123456.5,
    }


class InboundMessageParsingTests(unittest.TestCase):
    def test_mixed_segments_preserve_text_media_and_quote_order(self):
        from qq_openclaw_bridge.domain.inbound import InboundSegment
        from qq_openclaw_bridge.onebot import parse_inbound_message

        envelope = parse_inbound_message(
            private_event(
                [
                    {
                        "type": "image",
                        "data": {
                            "file": "a.jpg",
                            "url": "https://cdn.example/a.jpg",
                            "name": "photo.jpg",
                            "file_size": "12",
                            "mime": "image/jpeg",
                        },
                    },
                    {"type": "text", "data": {"text": "看看这张"}},
                    {"type": "reply", "data": {"id": "900"}},
                    {
                        "type": "image",
                        "data": {
                            "file": "b.jpg",
                            "url": "https://cdn.example/b.jpg",
                        },
                    },
                ]
            )
        )

        self.assertEqual(envelope.text, "看看这张")
        self.assertEqual(envelope.text_parts, ("看看这张",))
        self.assertEqual([item.kind for item in envelope.attachments], ["image", "image"])
        self.assertEqual(envelope.attachments[0].file_id, "a.jpg")
        self.assertEqual(envelope.attachments[0].declared_size, 12)
        self.assertEqual(envelope.attachments[0].declared_mime, "image/jpeg")
        self.assertEqual(envelope.quote.message_id, "900")
        self.assertEqual(
            envelope.segments,
            (
                InboundSegment("attachment", 0),
                InboundSegment("text", 0),
                InboundSegment("quote", 0),
                InboundSegment("attachment", 1),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            envelope.segments[0].index = 1

    def test_plain_string_message_creates_frozen_text_envelope(self):
        from qq_openclaw_bridge.onebot import parse_inbound_message

        envelope = parse_inbound_message(private_event("  你好 OpenClaw  "))

        self.assertEqual(envelope.user_id, 10001)
        self.assertEqual(envelope.message_id, "42")
        self.assertEqual(envelope.timestamp, 123456.5)
        self.assertEqual(envelope.text_parts, ("你好 OpenClaw",))
        self.assertEqual(envelope.attachments, ())
        with self.assertRaises(FrozenInstanceError):
            envelope.message_id = "different"

    def test_image_only_and_file_segments_create_attachment_references(self):
        from qq_openclaw_bridge.onebot import parse_inbound_message

        envelope = parse_inbound_message(
            private_event(
                [
                    {"type": "image", "data": {"file": "cover.png"}},
                    {
                        "type": "file",
                        "data": {
                            "file_id": "doc-7",
                            "url": "https://cdn.example/notes.pdf",
                            "name": "notes.pdf",
                            "file_size": 4096,
                        },
                    },
                ]
            )
        )

        self.assertEqual(envelope.text, "")
        self.assertEqual([item.kind for item in envelope.attachments], ["image", "file"])
        self.assertEqual(envelope.attachments[1].file_id, "doc-7")
        self.assertEqual(envelope.attachments[1].declared_size, 4096)

    def test_unknown_segment_is_an_unsupported_attachment_summary(self):
        from qq_openclaw_bridge.onebot import parse_inbound_message

        envelope = parse_inbound_message(
            private_event(
                [
                    {"type": "poke"},
                    {"type": "gift", "data": None},
                    {"type": "json", "data": "not a mapping"},
                ]
            )
        )

        self.assertEqual([item.kind for item in envelope.attachments], ["unsupported"] * 3)
        self.assertEqual([item.name for item in envelope.attachments], ["poke", "gift", "json"])
        self.assertTrue(all(item.url is None for item in envelope.attachments))

    def test_quote_only_message_is_preserved_with_segment_metadata(self):
        from qq_openclaw_bridge.domain.inbound import InboundSegment
        from qq_openclaw_bridge.onebot import parse_inbound_message

        envelope = parse_inbound_message(private_event([{"type": "reply", "data": {"id": "900"}}]))

        self.assertEqual(envelope.text, "")
        self.assertEqual(envelope.quote.message_id, "900")
        self.assertEqual(envelope.segments, (InboundSegment("quote", 0),))

    def test_user_id_accepts_only_positive_integers_or_decimal_strings(self):
        from qq_openclaw_bridge.onebot import parse_inbound_message

        for value in (10001, "10001", "0010001"):
            with self.subTest(value=value):
                envelope = parse_inbound_message(
                    {**private_event("hello"), "user_id": value}
                )
                self.assertIsNotNone(envelope)
                self.assertEqual(envelope.user_id, 10001)

        for value in (
            True,
            False,
            0,
            -1,
            10001.0,
            10001.9,
            float("nan"),
            float("inf"),
            "0",
            "-1",
            "+10001",
            " 10001 ",
            "10001.0",
            "NaN",
            "Inf",
        ):
            with self.subTest(value=value):
                self.assertIsNone(
                    parse_inbound_message({**private_event("hello"), "user_id": value})
                )

    def test_user_id_rejects_unicode_and_overflowing_decimal_values(self):
        from qq_openclaw_bridge.onebot import parse_qq_user_id

        maximum_user_id = 10**20 - 1
        self.assertEqual(parse_qq_user_id(str(maximum_user_id)), maximum_user_id)
        self.assertEqual(parse_qq_user_id(maximum_user_id), maximum_user_id)

        for value in (
            "\u0661\u0660\u0660\u0660\u0661",
            "\uff11\uff10\uff10\uff10\uff11",
            "1" * 1000,
            str(10**20),
            10**20,
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_qq_user_id(value))

    def test_second_valid_reply_segment_rejects_the_entire_message(self):
        from qq_openclaw_bridge.onebot import parse_inbound_message

        envelope = parse_inbound_message(
            private_event(
                [
                    {"type": "reply", "data": {"id": "900"}},
                    {"type": "text", "data": {"text": "do not drop this"}},
                    {"type": "reply", "data": {"message_id": "901"}},
                ]
            )
        )

        self.assertIsNone(envelope)

    def test_non_finite_or_invalid_timestamps_fall_back_to_finite_local_time(self):
        from qq_openclaw_bridge.onebot import parse_inbound_message

        with patch("qq_openclaw_bridge.onebot.time.time", return_value=9876.5):
            for value in (float("nan"), float("inf"), float("-inf"), "not a time"):
                with self.subTest(value=value):
                    envelope = parse_inbound_message(private_event("hello") | {"time": value})
                    self.assertEqual(envelope.timestamp, 9876.5)

        future = 9876543210.0
        self.assertEqual(
            parse_inbound_message(private_event("hello") | {"time": future}).timestamp,
            future,
        )

    def test_malformed_events_are_not_parsed(self):
        from qq_openclaw_bridge.onebot import parse_inbound_message

        self.assertIsNone(parse_inbound_message({"message": "missing identity"}))
        self.assertIsNone(parse_inbound_message(private_event({"not": "a segment list"})))
        self.assertIsNone(parse_inbound_message(private_event([{"type": "text"}])))


if __name__ == "__main__":
    unittest.main()
