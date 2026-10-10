from dataclasses import FrozenInstanceError
from collections import UserDict
import json
import math
from pathlib import Path
import sys
from typing import Iterator, Mapping
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


BOT_QQ = 90001
GROUP_ID = 77
USER_ID = 10001
MAX_QQ_ID = 10**20 - 1


def group_event(message, **overrides):
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": GROUP_ID,
        "user_id": USER_ID,
        "message_id": 123,
        "message": message,
        "time": 456,
    }
    event.update(overrides)
    return event


def text(value="hello"):
    return {"type": "text", "data": {"text": value}}


def at(qq=BOT_QQ):
    return {"type": "at", "data": {"qq": str(qq)}}


def reply(message_id=42):
    return {"type": "reply", "data": {"id": str(message_id)}}


class PairSpy:
    def __init__(self, result="allowed"):
        self.calls = []
        self.result = result

    def allow_pair(self, key, other, other_key):
        self.calls.append((key, other, other_key))
        return self.result


class SecretError(BaseException):
    pass


class SecretKeyboardInterrupt(KeyboardInterrupt):
    pass


class EvilResult(str):
    def __new__(cls):
        value = super().__new__(cls, "allowed")
        value.touched = []
        return value

    def __eq__(self, other):
        self.touched.append("eq")
        raise SecretError("sensitive result payload")

    def __ne__(self, other):
        self.touched.append("ne")
        raise SecretError("sensitive result payload")

    def __hash__(self):
        self.touched.append("hash")
        raise SecretError("sensitive result payload")

    def __str__(self):
        self.touched.append("str")
        raise SecretError("sensitive result payload")


class EvilInt(int):
    pass


class EvilFloat(float):
    pass


class EvilStr(str):
    def strip(self, *args, **kwargs):
        raise SecretError("sensitive string payload")

    def isascii(self):
        raise SecretError("sensitive string payload")

    def isdecimal(self):
        raise SecretError("sensitive string payload")


class HostileMapping(Mapping):
    def __getitem__(self, key):
        raise SecretError("sensitive mapping payload")

    def __iter__(self) -> Iterator:
        raise SecretError("sensitive mapping payload")

    def __len__(self):
        raise SecretError("sensitive mapping payload")

    def get(self, key, default=None):
        raise SecretError("sensitive mapping payload")

    def __contains__(self, key):
        raise SecretError("sensitive mapping payload")


class ControlMapping(HostileMapping):
    def __init__(self, exception_type):
        self.exception_type = exception_type

    def _raise(self):
        raise self.exception_type("sensitive control payload")

    def __getitem__(self, key):
        self._raise()

    def __iter__(self):
        self._raise()

    def __len__(self):
        self._raise()

    def get(self, key, default=None):
        self._raise()

    def __contains__(self, key):
        self._raise()


class HostileLimiter:
    def allow_pair(self, key, other, other_key):
        raise SecretError("sensitive limiter payload")


class ControlLimiter:
    def __init__(self, exception_type):
        self.exception_type = exception_type

    def allow_pair(self, key, other, other_key):
        raise self.exception_type("sensitive limiter payload")


def assert_clean_control(testcase, exception_type, operation):
    try:
        operation()
    except BaseException as exc:
        testcase.assertIs(type(exc), exception_type)
        testcase.assertEqual(exc.args, ())
        testcase.assertIsNone(exc.__cause__)
        testcase.assertIsNone(exc.__context__)
        frames = []
        traceback = exc.__traceback__
        while traceback is not None:
            frames.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        testcase.assertNotIn("_raise", frames)
        testcase.assertNotIn("allow_pair", frames)
    else:
        testcase.fail(f"{exception_type.__name__} was swallowed")


class GroupParserTests(unittest.TestCase):
    def test_direct_bot_at_is_removed_from_normalized_text(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        envelope = parse_group_message(
            group_event([text("  前半句 "), at(), text(" 后半句  ")]), BOT_QQ
        )

        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.group_id, GROUP_ID)
        self.assertEqual(envelope.user_id, USER_ID)
        self.assertEqual(envelope.message_id, "123")
        self.assertEqual(envelope.timestamp, 456.0)
        self.assertEqual(envelope.text, "前半句 后半句")
        self.assertTrue(envelope.mentioned_bot)
        self.assertIsNone(envelope.quote)

    def test_other_at_is_parsed_but_does_not_claim_bot_was_mentioned(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        envelope = parse_group_message(group_event([at(81234), text("找他")]), BOT_QQ)

        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.text, "找他")
        self.assertFalse(envelope.mentioned_bot)

    def test_single_reply_is_preserved_without_trusting_embedded_text(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        message = [
            {"type": "reply", "data": {"message_id": "42", "text": "untrusted"}},
            text("继续"),
        ]
        envelope = parse_group_message(group_event(message), BOT_QQ)

        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.quote.message_id, "42")
        self.assertEqual(envelope.quote.embedded_text, "")

    def test_rejects_non_group_and_non_message_events(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        cases = (
            {"post_type": "notice", "message_type": "group"},
            {"post_type": "message", "message_type": "private"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.assertIsNone(
                    parse_group_message(group_event([at(), text() ], **overrides), BOT_QQ)
                )

    def test_rejects_boolean_fractional_nonpositive_and_oversized_envelope_ids(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        for field in ("group_id", "user_id", "message_id"):
            for value in (True, 1.5, 0, -1, MAX_QQ_ID + 1, str(MAX_QQ_ID + 1)):
                with self.subTest(field=field, value=value):
                    event = group_event([at(), text()], **{field: value})
                    self.assertIsNone(parse_group_message(event, BOT_QQ))

    def test_rejects_invalid_bot_id_and_malformed_at_ids(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        event = group_event([at(), text()])
        for value in (True, 1.5, 0, -1, MAX_QQ_ID + 1, str(BOT_QQ), EvilInt(BOT_QQ)):
            with self.subTest(bot_qq=value):
                self.assertIsNone(parse_group_message(event, value))

        for value in (True, 1.5, 0, -1, " 90001 ", "9e4", str(MAX_QQ_ID + 1)):
            with self.subTest(at_qq=value):
                malformed = {"type": "at", "data": {"qq": value}}
                self.assertIsNone(
                    parse_group_message(group_event([malformed, text()]), BOT_QQ)
                )

    def test_rejects_missing_duplicate_and_malformed_reply_ids(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        cases = (
            [reply(1), reply(2), text()],
            [{"type": "reply", "data": {}}, text()],
            [{"type": "reply", "data": {"id": True}}, text()],
            [{"type": "reply", "data": {"id": "-1"}}, text()],
            [{"type": "reply", "data": {"id": str(MAX_QQ_ID + 1)}}, text()],
            [{"type": "reply", "data": {"id": "42", "message_id": None}}, text()],
            [{"type": "reply", "data": {"id": None, "message_id": "42"}}, text()],
        )
        for message in cases:
            with self.subTest(message=message):
                self.assertIsNone(parse_group_message(group_event(message), BOT_QQ))

    def test_rejects_unknown_or_malformed_segments(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        cases = (
            [at(), text(), {"type": "image", "data": {"file": "x"}}],
            [at(), text(), {"type": "poke", "data": {}}],
            [at(), {"type": "text", "data": []}],
            [at(), {"data": {}}],
            [at(), "not-a-segment"],
        )
        for message in cases:
            with self.subTest(message=message):
                self.assertIsNone(parse_group_message(group_event(message), BOT_QQ))

    def test_rejects_empty_and_oversized_text(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        for value in ("", " \n\t ", "x" * 4097):
            with self.subTest(length=len(value)):
                self.assertIsNone(
                    parse_group_message(group_event([at(), text(value)]), BOT_QQ)
                )

    def test_rejects_raw_string_message_and_nonfinite_timestamp(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        self.assertIsNone(parse_group_message(group_event("@bot hello"), BOT_QQ))
        for value in (True, math.inf, math.nan, "not-time"):
            with self.subTest(time=value):
                self.assertIsNone(
                    parse_group_message(group_event([at(), text()], time=value), BOT_QQ)
                )

    def test_hostile_event_segment_and_data_mappings_fail_closed(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        events = (
            HostileMapping(),
            group_event([HostileMapping(), text()]),
            group_event([{"type": "at", "data": HostileMapping()}, text()]),
        )
        for event in events:
            with self.subTest(event_type=type(event).__name__):
                self.assertIsNone(parse_group_message(event, BOT_QQ))

    def test_exact_control_exceptions_are_sanitized_and_rethrown(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        for exception_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(exception_type=exception_type.__name__, boundary="event"):
                assert_clean_control(
                    self,
                    exception_type,
                    lambda: parse_group_message(ControlMapping(exception_type), BOT_QQ),
                )
            with self.subTest(exception_type=exception_type.__name__, boundary="segment"):
                assert_clean_control(
                    self,
                    exception_type,
                    lambda: parse_group_message(
                        group_event([ControlMapping(exception_type), text()]), BOT_QQ
                    ),
                )
            with self.subTest(exception_type=exception_type.__name__, boundary="data"):
                assert_clean_control(
                    self,
                    exception_type,
                    lambda: parse_group_message(
                        group_event(
                            [
                                {"type": "at", "data": ControlMapping(exception_type)},
                                text(),
                            ]
                        ),
                        BOT_QQ,
                    ),
                )

    def test_control_exception_subclass_remains_fail_closed(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        self.assertIsNone(
            parse_group_message(ControlMapping(SecretKeyboardInterrupt), BOT_QQ)
        )

    def test_benign_mapping_implementations_remain_supported(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        event = UserDict(group_event([
            UserDict({"type": "at", "data": UserDict({"qq": str(BOT_QQ)})}),
            UserDict({"type": "text", "data": UserDict({"text": "hello"})}),
        ]))

        envelope = parse_group_message(event, BOT_QQ)

        self.assertIsNotNone(envelope)
        self.assertTrue(envelope.mentioned_bot)
        self.assertEqual(envelope.text, "hello")


class GroupFilterTests(unittest.TestCase):
    def evaluate(
        self, event, *, reply_sender_id=None, groups=None, group=None, user=None,
        allow_untriggered=False,
    ):
        from qq_openclaw_bridge.filters import evaluate_group_event

        return evaluate_group_event(
            event,
            bot_qq=BOT_QQ,
            allowed_groups={GROUP_ID} if groups is None else groups,
            group_limiter=PairSpy() if group is None else group,
            user_limiter=object() if user is None else user,
            reply_sender_id=reply_sender_id,
            allow_untriggered=allow_untriggered,
        )

    def test_accepts_only_direct_at_and_builds_group_request(self):
        decision = self.evaluate(group_event([at(), text("  怎么看？ ")]))

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, "accepted")
        self.assertEqual(decision.request.group_id, GROUP_ID)
        self.assertEqual(decision.request.user_id, USER_ID)
        self.assertEqual(decision.request.message_id, "123")
        self.assertEqual(decision.request.text, "怎么看？")
        self.assertEqual(decision.request.timestamp, 456.0)
        self.assertEqual(decision.request.quote_text, "")

    def test_accepts_reply_only_when_resolved_sender_is_bot(self):
        event = group_event([reply(), text("继续说")])

        accepted = self.evaluate(event, reply_sender_id=BOT_QQ)
        other = self.evaluate(event, reply_sender_id=81234)
        unresolved = self.evaluate(event, reply_sender_id=None)

        self.assertTrue(accepted.accepted)
        self.assertEqual(other.reason, "ignored_not_triggered")
        self.assertEqual(unresolved.reason, "ignored_not_triggered")

        for hostile in (str(BOT_QQ), EvilInt(BOT_QQ), True):
            with self.subTest(reply_sender_id=hostile):
                decision = self.evaluate(event, reply_sender_id=hostile)
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, "ignored_not_triggered")

    def test_reply_sender_does_not_trigger_without_a_reply_segment(self):
        decision = self.evaluate(group_event([text("普通消息")]), reply_sender_id=BOT_QQ)

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ignored_not_triggered")

    def test_other_at_does_not_trigger(self):
        decision = self.evaluate(group_event([at(81234), text("普通消息")]))

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ignored_not_triggered")

    def test_explicit_interjection_approval_accepts_plain_message_only_for_true(self):
        event = group_event([text("普通消息")])

        self.assertTrue(self.evaluate(event, allow_untriggered=True).accepted)
        for value in (False, 1, "yes", object()):
            with self.subTest(value=value):
                decision = self.evaluate(event, allow_untriggered=value)
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, "ignored_not_triggered")

    def test_malformed_at_and_failed_reply_lookup_do_not_trigger(self):
        event = group_event(
            [
                {"type": "at", "data": {"qq": True}},
                reply(),
                text("hi"),
            ]
        )

        decision = self.evaluate(event, reply_sender_id=None)

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ignored_not_triggered")

    def test_allowlist_precedes_payload_and_reply_sender_handling(self):
        group_limiter = PairSpy()
        user_limiter = object()
        event = group_event(
            [{"type": "reply", "data": {"id": True}}, text("hi")], group_id=88
        )

        decision = self.evaluate(
            event,
            reply_sender_id=True,
            groups={GROUP_ID},
            group=group_limiter,
            user=user_limiter,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ignored_not_allowed")
        self.assertIsNone(decision.envelope)
        self.assertEqual(group_limiter.calls, [])

    def test_rejects_non_group_invalid_group_and_invalid_reply_sender(self):
        cases = (
            (group_event([at(), text()], post_type="notice"), None, "ignored_non_message"),
            (group_event([at(), text()], message_type="private"), None, "ignored_non_group"),
            (group_event([at(), text()], group_id=True), None, "ignored_missing_group"),
            (group_event([reply(), text()]), True, "ignored_not_triggered"),
            (group_event([reply(), text()]), -1, "ignored_not_triggered"),
        )
        for event, reply_sender_id, reason in cases:
            with self.subTest(reason=reason, reply_sender_id=reply_sender_id):
                decision = self.evaluate(event, reply_sender_id=reply_sender_id)
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, reason)

    def test_group_and_user_limiters_use_independent_namespaced_keys(self):
        group_limiter = PairSpy()
        user_limiter = object()

        decision = self.evaluate(
            group_event([at(), text()]), group=group_limiter, user=user_limiter
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(
            group_limiter.calls,
            [("group:77", user_limiter, "group:77:user:10001")],
        )

    def test_group_limit_rejects_before_user_limiter(self):
        group_limiter = PairSpy("first_limited")
        user_limiter = object()

        decision = self.evaluate(
            group_event([at(), text()]), group=group_limiter, user=user_limiter
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "group_rate_limited")
        self.assertEqual(
            group_limiter.calls,
            [("group:77", user_limiter, "group:77:user:10001")],
        )

    def test_user_limit_has_distinct_reason(self):
        decision = self.evaluate(
            group_event([at(), text()]),
            group=PairSpy("second_limited"),
            user=object(),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "user_rate_limited")

    def test_hostile_mapping_returns_stable_failure_decision(self):
        from qq_openclaw_bridge.filters import evaluate_group_event

        decision = evaluate_group_event(
            HostileMapping(),
            bot_qq=BOT_QQ,
            allowed_groups={GROUP_ID},
            group_limiter=PairSpy(),
            user_limiter=object(),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "invalid_group_event")
        self.assertNotIn("sensitive", repr(decision))

    def test_hostile_limiter_returns_stable_failure_without_sensitive_chain(self):
        decision = self.evaluate(
            group_event([at(), text()]),
            group=HostileLimiter(),
            user=object(),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "rate_limit_failed")
        self.assertNotIn("sensitive", repr(decision))

    def test_limiter_result_requires_an_exact_allowlisted_string(self):
        result = EvilResult()
        for value in (result, 1, object(), "unknown"):
            with self.subTest(result_type=type(value).__name__):
                decision = self.evaluate(
                    group_event([at(), text()]),
                    group=PairSpy(value),
                    user=object(),
                )
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, "rate_limit_failed")
        self.assertEqual(result.touched, [])

    def test_exact_limiter_control_exceptions_are_sanitized_and_rethrown(self):
        for exception_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(exception_type=exception_type.__name__):
                assert_clean_control(
                    self,
                    exception_type,
                    lambda: self.evaluate(
                        group_event([at(), text()]),
                        group=ControlLimiter(exception_type),
                        user=object(),
                    ),
                )

    def test_limiter_control_subclass_remains_stable_failure(self):
        decision = self.evaluate(
            group_event([at(), text()]),
            group=ControlLimiter(SecretKeyboardInterrupt),
            user=object(),
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "rate_limit_failed")

    def test_evaluate_rewraps_parser_control_exception_without_sensitive_chain(self):
        from qq_openclaw_bridge.filters import evaluate_group_event

        for exception_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            events = (
                ("event", ControlMapping(exception_type)),
                ("segment", group_event([ControlMapping(exception_type), text()])),
                (
                    "data",
                    group_event(
                        [
                            {"type": "at", "data": ControlMapping(exception_type)},
                            text(),
                        ]
                    ),
                ),
            )
            for boundary, event in events:
                with self.subTest(
                    exception_type=exception_type.__name__, boundary=boundary
                ):
                    assert_clean_control(
                        self,
                        exception_type,
                        lambda event=event: evaluate_group_event(
                            event,
                            bot_qq=BOT_QQ,
                            allowed_groups={GROUP_ID},
                            group_limiter=PairSpy(),
                            user_limiter=object(),
                        ),
                    )

    def test_user_limiter_failure_does_not_consume_group_capacity(self):
        from qq_openclaw_bridge.rate_limit import RateLimiter

        group_limiter = RateLimiter(1, window_seconds=60)
        failed = self.evaluate(
            group_event([at(), text()]),
            group=group_limiter,
            user=HostileLimiter(),
        )
        accepted = self.evaluate(
            group_event([at(), text()], message_id=124),
            group=group_limiter,
            user=RateLimiter(1, window_seconds=60),
        )

        self.assertEqual(failed.reason, "rate_limit_failed")
        self.assertTrue(accepted.accepted)


class GroupContractAndActionTests(unittest.TestCase):
    def test_group_contracts_are_immutable(self):
        from qq_openclaw_bridge.onebot import parse_group_message

        envelope = parse_group_message(group_event([at(), text()]), BOT_QQ)
        decision = GroupFilterTests().evaluate(group_event([at(), text()]))

        with self.assertRaises(FrozenInstanceError):
            envelope.text = "changed"
        with self.assertRaises(FrozenInstanceError):
            decision.request.text = "changed"
        with self.assertRaises(FrozenInstanceError):
            decision.reason = "changed"

    def test_group_contracts_reject_invalid_direct_construction(self):
        from qq_openclaw_bridge.domain.inbound import GroupInboundEnvelope, QuoteRef
        from qq_openclaw_bridge.models import GroupMessageRequest

        with self.assertRaises(ValueError):
            GroupInboundEnvelope(True, USER_ID, "1", 1.0, "hello", True)
        with self.assertRaises(ValueError):
            GroupMessageRequest(GROUP_ID, 0, "1", "hello", 1.0)
        with self.assertRaises(ValueError):
            GroupMessageRequest(GROUP_ID, USER_ID, "1", "", 1.0)
        with self.assertRaises(ValueError):
            GroupInboundEnvelope(EvilInt(GROUP_ID), USER_ID, "1", 1.0, "hello", True)
        with self.assertRaises(ValueError):
            GroupInboundEnvelope(GROUP_ID, USER_ID, EvilStr("1"), 1.0, "hello", True)
        with self.assertRaises(ValueError):
            GroupMessageRequest(GROUP_ID, USER_ID, "1", EvilStr("hello"), 1.0)
        with self.assertRaises(ValueError):
            GroupMessageRequest(EvilInt(GROUP_ID), USER_ID, "1", "hello", 1.0)
        with self.assertRaises(ValueError):
            GroupMessageRequest(GROUP_ID, USER_ID, EvilStr("1"), "hello", 1.0)
        with self.assertRaises(ValueError):
            GroupMessageRequest(GROUP_ID, USER_ID, "1", "hello", EvilFloat(1.0))
        with self.assertRaises(ValueError):
            GroupMessageRequest(GROUP_ID, USER_ID, "1", "hello", 1.0, EvilStr("quote"))
        with self.assertRaises(ValueError):
            GroupInboundEnvelope(
                GROUP_ID,
                USER_ID,
                "1",
                1.0,
                "hello",
                True,
                QuoteRef("2", []),
            )

    def test_group_envelope_rebuilds_quote_with_canonical_builtin_values(self):
        from qq_openclaw_bridge.domain.inbound import GroupInboundEnvelope, QuoteRef

        quote = QuoteRef("002", "quoted")
        envelope = GroupInboundEnvelope(
            GROUP_ID, USER_ID, "1", 1.0, "hello", False, quote
        )

        self.assertIsNot(envelope.quote, quote)
        self.assertIs(type(envelope.quote), QuoteRef)
        self.assertIs(type(envelope.quote.message_id), str)
        self.assertIs(type(envelope.quote.embedded_text), str)
        self.assertEqual(envelope.quote.message_id, "2")

    def test_group_request_canonicalizes_numeric_message_id(self):
        from qq_openclaw_bridge.models import GroupMessageRequest

        request = GroupMessageRequest(
            GROUP_ID, USER_ID, "001", "hello", 1, "quoted"
        )

        self.assertIs(type(request.message_id), str)
        self.assertEqual(request.message_id, "1")
        self.assertIs(type(request.timestamp), float)

    def test_builds_strict_send_group_message_action(self):
        from qq_openclaw_bridge.onebot import build_send_group_msg

        self.assertEqual(
            build_send_group_msg(GROUP_ID, "你好", echo="group-reply-1"),
            {
                "action": "send_group_msg",
                "params": {"group_id": GROUP_ID, "message": "你好"},
                "echo": "group-reply-1",
            },
        )
        self.assertEqual(
            build_send_group_msg(GROUP_ID, "你好"),
            {"action": "send_group_msg", "params": {"group_id": GROUP_ID, "message": "你好"}},
        )

        wire = json.loads(json.dumps(build_send_group_msg(GROUP_ID, "你好", "e-1")))
        self.assertEqual(
            wire,
            {
                "action": "send_group_msg",
                "params": {"group_id": GROUP_ID, "message": "你好"},
                "echo": "e-1",
            },
        )
        self.assertIs(type(wire["params"]["group_id"]), int)
        self.assertIs(type(wire["params"]["message"]), str)

    def test_send_group_message_rejects_invalid_values(self):
        from qq_openclaw_bridge.onebot import build_send_group_msg

        cases = (
            (True, "hello", None),
            (str(GROUP_ID), "hello", None),
            (EvilInt(GROUP_ID), "hello", None),
            (0, "hello", None),
            (MAX_QQ_ID + 1, "hello", None),
            (GROUP_ID, "", None),
            (GROUP_ID, EvilStr("hello"), None),
            (GROUP_ID, "x" * 4097, None),
            (GROUP_ID, "hello", True),
            (GROUP_ID, "hello", EvilStr("echo")),
        )
        for group_id, message, echo in cases:
            with self.subTest(group_id=group_id, message_length=len(message)):
                with self.assertRaises(ValueError):
                    build_send_group_msg(group_id, message, echo)


if __name__ == "__main__":
    unittest.main()
