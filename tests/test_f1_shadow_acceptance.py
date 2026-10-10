import asyncio
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _CommandChain:
    def __init__(self, reply=None):
        self.reply = reply

    def handle(self, request):
        return self.reply


class _Executor:
    def __init__(self):
        self.calls = 0

    async def execute(self, plan):
        from qq_openclaw_bridge.orchestration.models import ExecutionResult

        self.calls += 1
        return ExecutionResult("Direct reply")


class _Planner:
    def plan(self, contract):
        from qq_openclaw_bridge.orchestration.direct import DirectPlanningStrategy

        return DirectPlanningStrategy().plan(contract)


class _Verifier:
    def verify(self, contract, result):
        from qq_openclaw_bridge.orchestration.models import VerificationReport

        return VerificationReport(True)


class _CountingClassifier:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def classify(self, contract, config):
        self.calls += 1
        if self.error is not None:
            raise self.error
        from qq_openclaw_bridge.application.shadow_routing import ClassifierCallResult
        from qq_openclaw_bridge.domain.routing import ClassifierDecision

        return ClassifierCallResult(
            ClassifierDecision("L1", 0.9, "question", ("short",), False),
            "openai", "gpt-5.4-nano", 1, 1, 1,
        )


class ShadowRoutingAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WorkspaceStateStore(Path(self.temp_dir.name) / "state.sqlite3")
        self.store.routing_repository.create_config({
            "enabled": True,
            "classifier_enabled": True,
            "classifier_model": "openai/gpt-5.4-nano",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 15,
            "max_daily_calls": 100,
            "failure_threshold": 3,
            "cooldown_seconds": 300,
            "rule_version": "f1-v1",
        }, activate=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _coordinator(self, command_reply=None, classifier=None, mode="legacy_direct"):
        from qq_openclaw_bridge.application.coordinator import MessageCoordinator
        from qq_openclaw_bridge.application.shadow_routing import ShadowRoutingObserver
        from qq_openclaw_bridge.application.coordinator_factory import create_direct_runtime

        self.executor = _Executor()
        observer = ShadowRoutingObserver(
            self.store.routing_repository, classifier=classifier,
        )
        common = dict(
            command_chain=_CommandChain(command_reply),
            fallback_mapper=lambda error: "fallback",
            truncator=lambda text: text,
            shadow_routing_observer=observer,
        )
        if mode == "direct":
            return MessageCoordinator(
                orchestration_runtime=create_direct_runtime(
                    lambda request: self._agent_result()
                ), **common
            ), observer
        return MessageCoordinator(
            planner=_Planner(), executor=self.executor, verifier=_Verifier(), **common
        ), observer

    async def _agent_result(self):
        from qq_openclaw_bridge.orchestration.models import ExecutionResult

        self.executor.calls += 1
        return ExecutionResult("Direct reply")

    @staticmethod
    def _request(message_id="m1", text="你好"):
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(10001, message_id, text, 10.0)

    async def test_local_command_records_l0_without_classifier_or_main_agent(self):
        classifier = _CountingClassifier()
        coordinator, observer = self._coordinator("local reply", classifier)

        resolution = await coordinator.resolve_prepared(
            coordinator.prepare(self._request())
        )
        await coordinator.wait_for_shadow_tasks()

        row = self.store.routing_repository.recent_decisions(1)[0]
        self.assertEqual(resolution.reply, "local reply")
        self.assertEqual(row["final_level"], "L0")
        self.assertEqual(classifier.calls, 0)
        self.assertEqual(self.executor.calls, 0)

    async def test_normal_message_observes_once_and_calls_main_agent_once(self):
        classifier = _CountingClassifier()
        coordinator, observer = self._coordinator(classifier=classifier)

        resolution = await coordinator.resolve_prepared(
            coordinator.prepare(self._request())
        )
        await coordinator.wait_for_shadow_tasks()

        self.assertEqual(resolution.reply, "Direct reply")
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(classifier.calls, 1)
        self.assertEqual(len(self.store.routing_repository.recent_decisions(10)), 1)

    async def test_classifier_error_does_not_change_direct_reply(self):
        coordinator, observer = self._coordinator(classifier=_CountingClassifier(ValueError("bad classifier")))

        resolution = await coordinator.resolve_prepared(
            coordinator.prepare(self._request())
        )
        await coordinator.wait_for_shadow_tasks()

        self.assertEqual(resolution.reply, "Direct reply")
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.store.routing_repository.recent_decisions(1)[0]["status"], "error")

    async def test_legacy_and_direct_modes_keep_direct_reply_while_observing(self):
        for mode in ("legacy_direct", "direct"):
            with self.subTest(mode=mode):
                coordinator, observer = self._coordinator(
                    classifier=_CountingClassifier(), mode=mode
                )
                resolution = await coordinator.resolve_prepared(
                    coordinator.prepare(self._request("m-%s" % mode))
                )
                await coordinator.wait_for_shadow_tasks()

                self.assertEqual(resolution.reply, "Direct reply")
                self.assertEqual(self.executor.calls, 1)

    async def test_wait_for_shadow_tasks_drains_observer(self):
        coordinator, observer = self._coordinator(classifier=_CountingClassifier())
        await coordinator.resolve_prepared(coordinator.prepare(self._request()))

        await coordinator.wait_for_shadow_tasks()

        self.assertEqual(observer._tasks, set())


if __name__ == "__main__":
    unittest.main()
