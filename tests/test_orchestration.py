from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def _request(self):
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(
            user_id=10001,
            message_id="m1",
            text="分析这个问题",
            timestamp=10.0,
        )

    async def test_direct_plan_preserves_request_and_calls_executor_once(self):
        from qq_openclaw_bridge.models import AgentResponse
        from qq_openclaw_bridge.orchestration.direct import (
            DirectPlanningStrategy,
            SingleAgentExecutionEngine,
        )
        from qq_openclaw_bridge.orchestration.models import TaskContract

        request = self._request()
        contract = TaskContract.from_request(request)
        plan = DirectPlanningStrategy().plan(contract)
        calls = []

        async def reply_call(actual):
            calls.append(actual)
            return AgentResponse(text="执行完成")

        result = await SingleAgentExecutionEngine(reply_call).execute(plan)

        self.assertEqual(contract.request, request)
        self.assertEqual(contract.goal, request.text)
        self.assertEqual(len(plan.items), 1)
        self.assertEqual(plan.items[0].item_id, "direct")
        self.assertEqual(plan.items[0].request, request)
        self.assertEqual(result.text, "执行完成")
        self.assertEqual(calls, [request])

    async def test_direct_executor_rejects_multi_item_plan(self):
        from qq_openclaw_bridge.orchestration.direct import (
            DirectPlanningStrategy,
            SingleAgentExecutionEngine,
        )
        from qq_openclaw_bridge.orchestration.models import ExecutionPlan, TaskContract

        contract = TaskContract.from_request(self._request())
        direct = DirectPlanningStrategy().plan(contract)
        invalid = ExecutionPlan(contract=contract, items=direct.items + direct.items)

        async def reply_call(request):
            self.fail("invalid plan must not call the agent")

        with self.assertRaisesRegex(ValueError, "exactly one work item"):
            await SingleAgentExecutionEngine(reply_call).execute(invalid)

    def test_compatibility_verifier_rejects_empty_reply(self):
        from qq_openclaw_bridge.orchestration.direct import (
            CompatibilityVerificationStrategy,
        )
        from qq_openclaw_bridge.orchestration.models import (
            ExecutionResult,
            TaskContract,
        )

        contract = TaskContract.from_request(self._request())

        empty = CompatibilityVerificationStrategy().verify(
            contract, ExecutionResult(text="")
        )
        valid = CompatibilityVerificationStrategy().verify(
            contract, ExecutionResult(text="正常回复")
        )

        self.assertFalse(empty.accepted)
        self.assertIn("empty", empty.reason)
        self.assertTrue(valid.accepted)
        self.assertEqual(valid.reason, "")

    def test_extension_ports_have_inert_compatibility_implementations(self):
        from qq_openclaw_bridge.orchestration.direct import (
            DisabledBrainstormGate,
            ExistingLimitBudgetPolicy,
            StaticCapabilityRegistry,
        )
        from qq_openclaw_bridge.orchestration.models import TaskContract

        contract = TaskContract.from_request(self._request())

        self.assertEqual(DisabledBrainstormGate().mode_for(contract), "disabled")
        self.assertTrue(ExistingLimitBudgetPolicy().allows(contract))
        self.assertEqual(
            StaticCapabilityRegistry("openclaw-agent").select(contract),
            "openclaw-agent",
        )


if __name__ == "__main__":
    unittest.main()
