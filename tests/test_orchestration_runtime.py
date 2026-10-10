from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class FakeBudget:
    def __init__(self, events):
        self.events = events
        self.allowed = True
        self.error = None

    def allows(self, contract):
        self.events.append("budget")
        if self.error is not None:
            raise self.error
        return self.allowed


class FakeRegistry:
    def __init__(self, events):
        self.events = events

    def select(self, contract):
        self.events.append("capability")
        return "openclaw-agent"


class FakePlanner:
    def __init__(self, events):
        self.events = events
        self.error = None

    def plan(self, contract):
        from qq_openclaw_bridge.orchestration.models import ExecutionPlan, WorkItem

        self.events.append("plan")
        if self.error is not None:
            raise self.error
        return ExecutionPlan(
            contract=contract,
            items=(WorkItem(item_id="direct", request=contract.request),),
        )


class FakeExecutor:
    def __init__(self, events):
        self.events = events
        self.calls = 0
        self.error = None

    async def execute(self, plan):
        from qq_openclaw_bridge.orchestration.models import ExecutionResult

        self.events.append("execute")
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ExecutionResult(text="reply")


class FakeVerifier:
    def __init__(self, events):
        self.events = events
        self.accepted = True
        self.error = None

    def verify(self, contract, result):
        from qq_openclaw_bridge.orchestration.models import VerificationReport

        self.events.append("verify")
        if self.error is not None:
            raise self.error
        return VerificationReport(self.accepted, "rejected by verifier")


class FakeTrace:
    def __init__(self):
        self.calls = []
        self.raise_on_start = False
        self.raise_on_transition = False
        self.raise_on_finish = False

    def start(self, contract, mode, config_version):
        if self.raise_on_start:
            raise RuntimeError("trace start down")
        self.calls.append(("start", mode, config_version))
        return "run-1"

    def transition(self, run_id, stage, detail=""):
        if self.raise_on_transition:
            raise RuntimeError("trace transition down")
        self.calls.append(("transition", run_id, stage, detail))

    def finish(self, run_id, status, detail=""):
        if self.raise_on_finish:
            raise RuntimeError("trace finish down")
        self.calls.append(("finish", run_id, status, detail))


class FakeLogger:
    def __init__(self):
        self.warnings = []
        self.raise_on_warning = False

    def warning(self, message, value):
        if self.raise_on_warning:
            raise RuntimeError("logger down")
        self.warnings.append((message, value))


class OrchestrationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from qq_openclaw_bridge.models import AgentRequest
        from qq_openclaw_bridge.orchestration.config import OrchestrationConfig
        from qq_openclaw_bridge.orchestration.runtime import OrchestrationRuntime

        self.request = AgentRequest(7, "m1", "你好", 10)
        self.events = []
        self.budget = FakeBudget(self.events)
        self.registry = FakeRegistry(self.events)
        self.planner = FakePlanner(self.events)
        self.executor = FakeExecutor(self.events)
        self.verifier = FakeVerifier(self.events)
        self.trace = FakeTrace()
        self.logger = FakeLogger()
        self.runtime = OrchestrationRuntime(
            planner=self.planner,
            executor=self.executor,
            verifier=self.verifier,
            budget_policy=self.budget,
            capability_registry=self.registry,
            trace_sink=self.trace,
            config=OrchestrationConfig("direct", 1),
            logger=self.logger,
        )

    def _contract(self):
        from qq_openclaw_bridge.orchestration.models import TaskContract

        return TaskContract.from_request(self.request)

    async def test_direct_runtime_calls_each_stage_once_and_returns_result(self):
        result = await self.runtime.execute(self._contract())
        self.assertEqual(result.text, "reply")
        self.assertEqual(
            self.events, ["budget", "capability", "plan", "execute", "verify"]
        )
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(
            [call[2] for call in self.trace.calls if call[0] == "transition"],
            ["planned", "executing", "verifying"],
        )
        self.assertEqual(self.trace.calls[-1], ("finish", "run-1", "accepted", ""))

    async def test_trace_failure_does_not_replace_a_valid_direct_result(self):
        self.trace.raise_on_transition = True
        result = await self.runtime.execute(self._contract())
        self.assertEqual(result.text, "reply")
        self.assertEqual(self.executor.calls, 1)
        self.assertTrue(self.logger.warnings)
        self.assertEqual({value for _, value in self.logger.warnings}, {"RuntimeError"})

    async def test_trace_start_failure_makes_later_trace_calls_noops(self):
        self.trace.raise_on_start = True
        result = await self.runtime.execute(self._contract())
        self.assertEqual(result.text, "reply")
        self.assertEqual(self.trace.calls, [])

    async def test_failing_logger_does_not_replace_valid_result(self):
        self.trace.raise_on_transition = True
        self.logger.raise_on_warning = True
        result = await self.runtime.execute(self._contract())
        self.assertEqual(result.text, "reply")

    async def test_trace_finish_and_logger_failures_do_not_replace_valid_result(self):
        self.trace.raise_on_finish = True
        self.logger.raise_on_warning = True
        result = await self.runtime.execute(self._contract())
        self.assertEqual(result.text, "reply")

    async def test_budget_rejection_never_calls_executor(self):
        from qq_openclaw_bridge.orchestration.runtime import BudgetRejected

        self.budget.allowed = False
        with self.assertRaises(BudgetRejected):
            await self.runtime.execute(self._contract())
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(self.trace.calls[-1][2:], ("rejected", "budget_rejected"))

    async def test_budget_exception_is_traced_failed_and_reraised_unchanged(self):
        error = LookupError("budget service failed")
        self.budget.error = error
        with self.assertRaises(LookupError) as raised:
            await self.runtime.execute(self._contract())
        self.assertIs(raised.exception, error)
        self.assertEqual(self.executor.calls, 0)
        self.assertEqual(
            self.trace.calls[-1], ("finish", "run-1", "failed", "LookupError")
        )

    async def test_verification_rejection_has_explicit_exception(self):
        from qq_openclaw_bridge.orchestration.runtime import VerificationRejected

        self.verifier.accepted = False
        with self.assertRaisesRegex(VerificationRejected, "rejected by verifier"):
            await self.runtime.execute(self._contract())
        self.assertEqual(self.trace.calls[-1][2], "rejected")

    async def test_verifier_raised_rejection_is_traced_as_failed_and_reraised(self):
        from qq_openclaw_bridge.orchestration.runtime import VerificationRejected

        error = VerificationRejected("verifier crashed")
        self.verifier.error = error
        with self.assertRaises(VerificationRejected) as raised:
            await self.runtime.execute(self._contract())
        self.assertIs(raised.exception, error)
        self.assertEqual(
            self.trace.calls[-1],
            ("finish", "run-1", "failed", "VerificationRejected"),
        )

    async def test_pipeline_exception_is_traced_and_reraised_unchanged(self):
        error = LookupError("planner failed")
        self.planner.error = error
        with self.assertRaises(LookupError) as raised:
            await self.runtime.execute(self._contract())
        self.assertIs(raised.exception, error)
        self.assertEqual(
            self.trace.calls[-1], ("finish", "run-1", "failed", "LookupError")
        )

    async def test_trace_and_logger_failures_do_not_replace_pipeline_exception(self):
        error = LookupError("planner failed")
        self.planner.error = error
        self.trace.raise_on_finish = True
        self.logger.raise_on_warning = True
        with self.assertRaises(LookupError) as raised:
            await self.runtime.execute(self._contract())
        self.assertIs(raised.exception, error)

    def test_config_accepts_only_direct_with_positive_version(self):
        from qq_openclaw_bridge.orchestration.config import OrchestrationConfig

        self.assertEqual(OrchestrationConfig("direct", 2).version, 2)
        for mode, version in (
            ("legacy_direct", 1),
            ("shadow", 1),
            ("direct", 0),
            ("direct", True),
        ):
            with self.subTest(mode=mode, version=version):
                with self.assertRaises(ValueError):
                    OrchestrationConfig(mode, version)


if __name__ == "__main__":
    unittest.main()
