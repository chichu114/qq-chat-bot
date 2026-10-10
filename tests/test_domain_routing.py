import json
import math
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class RoutingDomainTests(unittest.TestCase):
    def setUp(self):
        from qq_openclaw_bridge.models import AgentRequest

        self.AgentRequest = AgentRequest

    def contract(self, text, local_handled=False):
        from qq_openclaw_bridge.domain.routing import RoutingContract

        return RoutingContract.from_request(
            self.AgentRequest(7, "message-1", text, 1.0),
            local_handled=local_handled,
            config_version=3,
        )

    def test_contract_maps_agent_request_and_is_immutable(self):
        from qq_openclaw_bridge.domain.routing import RoutingContract

        contract = self.contract("你好")
        self.assertEqual(contract.user_id, 7)
        self.assertEqual(contract.source_message_id, "message-1")
        self.assertEqual(contract.config_version, 3)
        with self.assertRaises(FrozenInstanceError):
            contract.text = "changed"

    def test_local_command_is_l0_without_classifier(self):
        from qq_openclaw_bridge.domain.routing import DeterministicRiskRouter

        contract = self.contract("查看任务", local_handled=True)
        decision = DeterministicRiskRouter().evaluate(contract)
        self.assertEqual(decision.level, "L0")
        self.assertFalse(decision.needs_classifier)

    def test_external_send_and_secret_operations_are_forced_l3(self):
        from qq_openclaw_bridge.domain.routing import DeterministicRiskRouter

        router = DeterministicRiskRouter()
        for text in ("替我把这段发给客户", "删除这个文件", "把 API Key 写进配置"):
            decision = router.evaluate(self.contract(text))
            with self.subTest(text=text):
                self.assertEqual(decision.level, "L3")
                self.assertTrue(decision.forced)

    def test_truncated_input_forces_l3_and_blocks_classifier_downgrade(self):
        from qq_openclaw_bridge.domain.routing import (
            ClassifierDecision,
            ConservativeRouteResolver,
            DeterministicRiskRouter,
        )

        contract = self.contract("x" * 4096 + "删除这个文件")
        rule = DeterministicRiskRouter().evaluate(contract)
        self.assertEqual(rule.level, "L3")
        self.assertTrue(rule.forced)
        self.assertIn("input_truncated", rule.risk_flags)

        model = ClassifierDecision("L1", 0.99, "smalltalk", (), False)
        result = ConservativeRouteResolver().resolve(rule, model, 0.75)
        self.assertEqual(result.final_level, "L3")
        self.assertIn("model_downgrade_blocked", result.reasons)

    def test_ordinary_message_requires_classifier(self):
        from qq_openclaw_bridge.domain.routing import DeterministicRiskRouter

        decision = DeterministicRiskRouter().evaluate(
            self.contract("分析这个方案的优缺点")
        )
        self.assertIsNone(decision.level)
        self.assertTrue(decision.needs_classifier)

    def test_each_bounded_risk_group_has_a_stable_flag(self):
        from qq_openclaw_bridge.domain.routing import DeterministicRiskRouter

        cases = {
            "删除这条记录": "deletion",
            "发给客户": "external_send",
            "发布公告": "publishing",
            "支付这笔账单": "finance",
            "保存密码": "credentials",
            "重启服务": "system_control",
        }
        router = DeterministicRiskRouter()
        for text, flag in cases.items():
            with self.subTest(text=text):
                decision = router.evaluate(self.contract(text))
                self.assertEqual(decision.level, "L3")
                self.assertIn(flag, decision.risk_flags)

    def test_redacted_preview_masks_tokens_queries_and_long_numbers(self):
        from qq_openclaw_bridge.domain.routing import redacted_preview

        preview = redacted_preview(
            "Bearer abcdefghijklmnop https://x.test?a=secret 123456789012345678"
        )
        self.assertNotIn("abcdefghijklmnop", preview)
        self.assertNotIn("secret", preview)
        self.assertNotIn("123456789012345678", preview)
        self.assertLessEqual(len(preview), 160)

    def test_redacted_preview_masks_chinese_and_whitespace_separated_credentials(self):
        from qq_openclaw_bridge.domain.routing import redacted_preview

        preview = redacted_preview(
            "密码：shortpass 密钥 shortkey 凭证 shortcred "
            "token shorttoken secret shortsecret"
        )
        for value in ("shortpass", "shortkey", "shortcred", "shorttoken", "shortsecret"):
            with self.subTest(value=value):
                self.assertNotIn(value, preview)

    def test_redacted_preview_is_bounded_and_normalizes_whitespace(self):
        from qq_openclaw_bridge.domain.routing import redacted_preview

        preview = redacted_preview("  first\n\tsecond  " + "x" * 300, limit=20)
        self.assertEqual(preview, "first second xxxxxxx")
        self.assertLessEqual(len(preview), 20)

    def test_classifier_parser_accepts_the_bounded_schema(self):
        from qq_openclaw_bridge.domain.routing import parse_classifier_response

        decision = parse_classifier_response(
            json.dumps(
                {
                    "level": "L1",
                    "confidence": 0.9,
                    "task_kind": "smalltalk",
                    "reasons": ["ordinary conversation"],
                    "brainstorm_required": False,
                }
            )
        )
        self.assertEqual(decision.level, "L1")
        self.assertEqual(decision.confidence, 0.9)
        self.assertEqual(decision.task_kind, "smalltalk")
        self.assertEqual(decision.reasons, ("ordinary conversation",))
        self.assertFalse(decision.brainstorm_required)

    def test_classifier_task_kinds_are_canonical_ordered_and_match_the_prompt(self):
        from qq_openclaw_bridge.application.shadow_routing import (
            build_routing_classifier_prompt,
        )
        from qq_openclaw_bridge.domain.routing import TASK_KINDS, parse_classifier_response

        self.assertIsInstance(TASK_KINDS, tuple)
        self.assertEqual(
            TASK_KINDS,
            (
                "smalltalk", "question", "analysis", "creative", "coding",
                "planning", "summarization", "translation", "task",
                "external_action", "brainstorm", "other",
            ),
        )
        prompt = build_routing_classifier_prompt("harmless greeting")
        self.assertNotIn("local_command", prompt)
        self.assertNotIn("forced_risk", prompt)
        for task_kind in TASK_KINDS:
            with self.subTest(task_kind=task_kind):
                self.assertIn(task_kind, prompt)
                decision = parse_classifier_response(
                    json.dumps(
                        {
                            "level": "L1",
                            "confidence": 0.9,
                            "task_kind": task_kind,
                            "reasons": [],
                            "brainstorm_required": False,
                        }
                    )
                )
                self.assertEqual(decision.task_kind, task_kind)

    def test_classifier_parser_rejects_unknown_keys_and_invalid_enums(self):
        from qq_openclaw_bridge.domain.routing import parse_classifier_response

        valid = {
            "level": "L1",
            "confidence": 0.9,
            "task_kind": "smalltalk",
            "reasons": [],
            "brainstorm_required": False,
        }
        for key, value in (("unexpected", True), ("level", "L0"), ("task_kind", "unknown")):
            payload = dict(valid)
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_classifier_response(json.dumps(payload))

    def test_classifier_parser_rejects_unsafe_confidence_and_reasons(self):
        from qq_openclaw_bridge.domain.routing import parse_classifier_response

        valid = {
            "level": "L1",
            "confidence": 0.9,
            "task_kind": "smalltalk",
            "reasons": [],
            "brainstorm_required": False,
        }
        for confidence in (True, math.nan, math.inf, -0.1, 1.1):
            payload = dict(valid)
            payload["confidence"] = confidence
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                parse_classifier_response(json.dumps(payload, allow_nan=True))

        too_long = dict(valid)
        too_long["reasons"] = ["x" * 65]
        too_many = dict(valid)
        too_many["reasons"] = ["reason"] * 9
        for payload in (too_long, too_many):
            with self.assertRaises(ValueError):
                parse_classifier_response(json.dumps(payload))

    def test_low_confidence_falls_back_to_l2(self):
        from qq_openclaw_bridge.domain.routing import (
            ClassifierDecision,
            ConservativeRouteResolver,
            RuleDecision,
        )

        rule = RuleDecision(None, True)
        model = ClassifierDecision("L1", 0.60, "smalltalk", (), False)
        result = ConservativeRouteResolver().resolve(rule, model, 0.75)
        self.assertEqual(result.final_level, "L2")

    def test_forced_l3_cannot_be_downgraded(self):
        from qq_openclaw_bridge.domain.routing import (
            ClassifierDecision,
            ConservativeRouteResolver,
            RuleDecision,
        )

        rule = RuleDecision("L3", False, True, ("external_send",), ("forced_risk",))
        model = ClassifierDecision("L1", 0.99, "smalltalk", (), False)
        result = ConservativeRouteResolver().resolve(rule, model, 0.75)
        self.assertEqual(result.final_level, "L3")
        self.assertIn("model_downgrade_blocked", result.reasons)

    def test_classifier_parser_rejects_invalid_json(self):
        from qq_openclaw_bridge.domain.routing import parse_classifier_response

        with self.assertRaises(ValueError):
            parse_classifier_response("not json")


if __name__ == "__main__":
    unittest.main()
