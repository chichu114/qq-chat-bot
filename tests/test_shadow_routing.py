import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import time
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class RecordingAdapter:
    def __init__(self, text):
        self.text = text
        self.requests = []

    def reply(self, request):
        from qq_openclaw_bridge.models import AgentResponse

        self.requests.append(request)
        return AgentResponse(
            self.text,
            provider="openai",
            model="gpt-5.4-nano",
            input_tokens=12,
            output_tokens=8,
            duration_ms=7,
        )


class BlockingClassifier:
    def classify(self, contract, config):
        time.sleep(0.15)
        from qq_openclaw_bridge.domain.routing import ClassifierDecision
        from qq_openclaw_bridge.application.shadow_routing import ClassifierCallResult

        return ClassifierCallResult(
            decision=ClassifierDecision("L1", 0.9, "smalltalk", ("short",), False),
            provider="openai",
            model="gpt-5.4-nano",
            input_tokens=1,
            output_tokens=1,
            duration_ms=150,
        )


class FailingClassifier:
    def classify(self, contract, config):
        raise ValueError("classifier response is invalid")


class CountingClassifier:
    def __init__(self, reason="short"):
        self.calls = []
        self.reason = reason
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def classify(self, contract, config):
        with self._lock:
            self.calls.append((contract.source_message_id, dict(config)))
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            time.sleep(0.04)
            from qq_openclaw_bridge.application.shadow_routing import ClassifierCallResult
            from qq_openclaw_bridge.domain.routing import ClassifierDecision

            return ClassifierCallResult(
                ClassifierDecision("L1", 0.9, "smalltalk", (self.reason,), False),
                "openai", "gpt-5.4-nano", 1, 1, 1,
            )
        finally:
            with self._lock:
                self._active -= 1


class ExplodingResolver:
    def resolve(self, rule, classifier, threshold):
        raise RuntimeError("resolver failed")


class ExplodingRuleRouter:
    def evaluate(self, contract):
        raise ValueError("rule failed")


class BlockingCancellableClassifier:
    def __init__(self):
        self.started = threading.Event()
        self.finished = threading.Event()
        self._cancelled = threading.Event()

    def classify(self, contract, config):
        self.started.set()
        self._cancelled.wait(2)
        self.finished.set()
        raise RuntimeError("classifier cancelled")

    def cancel_active(self):
        self._cancelled.set()


class ShadowRoutingTests(unittest.TestCase):
    def test_application_exports_preserve_existing_coordinator_api(self):
        import qq_openclaw_bridge.application as application

        self.assertIn("MessageCoordinator", application.__all__)
        self.assertIn("ShadowRoutingObserver", application.__all__)

    def contract(self):
        from qq_openclaw_bridge.domain.routing import RoutingContract

        return RoutingContract(
            user_id=10001,
            source_message_id="source-1",
            text="你好，帮我分析这个方案",
            local_handled=False,
            config_version=1,
        )

    def routing_config(self, **overrides):
        config = {
            "enabled": True,
            "classifier_enabled": True,
            "classifier_model": "openai/gpt-5.4-nano",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 15,
            "max_daily_calls": 100,
            "failure_threshold": 3,
            "cooldown_seconds": 300,
            "rule_version": "f1-v1",
        }
        config.update(overrides)
        return config

    def test_classifier_uses_dedicated_agent_and_session(self):
        from qq_openclaw_bridge.application.shadow_routing import (
            LightweightRouteClassifier,
        )

        adapter = RecordingAdapter(
            '{"level":"L1","confidence":0.9,"task_kind":"smalltalk",'
            '"reasons":["short"],"brainstorm_required":false}'
        )
        classifier = LightweightRouteClassifier(adapter)

        result = classifier.classify(self.contract(), self.routing_config())

        self.assertEqual(result.decision.level, "L1")
        self.assertTrue(adapter.requests[0].session_key.startswith("agent:routing:qq-"))
        self.assertIn("只输出 JSON", adapter.requests[0].text)
        self.assertIn('"level"', adapter.requests[0].text)
        self.assertIn("帮我分析这个方案", adapter.requests[0].text)

    def test_classifier_invalid_model_json_does_not_chain_output_body(self):
        from qq_openclaw_bridge.application.shadow_routing import (
            LightweightRouteClassifier,
        )

        classifier = LightweightRouteClassifier(
            RecordingAdapter('{"OPENAI_API_KEY":"never-leak"')
        )

        with self.assertRaises(ValueError) as context:
            classifier.classify(self.contract(), self.routing_config())

        self.assertNotIn("never-leak", str(context.exception))
        self.assertIsNone(context.exception.__cause__)

    def test_public_classifier_prompt_declares_the_production_contract(self):
        from qq_openclaw_bridge.application.shadow_routing import (
            build_routing_classifier_prompt,
        )

        prompt = build_routing_classifier_prompt("harmless greeting")

        self.assertIn('"level":"L1"', prompt)
        self.assertIn("L1、L2 或 L3", prompt)
        self.assertIn("task_kind", prompt)
        self.assertIn("smalltalk", prompt)
        self.assertIn("只输出 JSON", prompt)
        self.assertIn("harmless greeting", prompt)

    def test_public_classifier_prompt_declares_level_and_brainstorm_rubric(self):
        from qq_openclaw_bridge.application.shadow_routing import (
            build_routing_classifier_prompt,
        )

        prompt = build_routing_classifier_prompt("请先头脑风暴，不要执行")

        self.assertIn("L1：闲聊、简单事实", prompt)
        self.assertIn("L2：中等分析、有界写作或编码", prompt)
        self.assertIn("L3：架构、重大决策、高影响或复杂多步", prompt)
        self.assertIn("先不执行", prompt)
        self.assertIn("brainstorm_required=true", prompt)
        self.assertIn("不得降低", prompt)

    def test_fuse_blocks_after_threshold_and_recovers_after_cooldown(self):
        from qq_openclaw_bridge.application.shadow_routing import RouteCallFuse

        fuse = RouteCallFuse(failure_threshold=3, cooldown_seconds=60)
        for _ in range(3):
            fuse.fail(now=100.0)

        self.assertFalse(fuse.allow(now=120.0, calls_today=0, max_daily_calls=100))
        self.assertTrue(fuse.allow(now=161.0, calls_today=0, max_daily_calls=100))

    def test_fuse_trips_with_production_allow_fail_sequence(self):
        from qq_openclaw_bridge.application.shadow_routing import RouteCallFuse

        fuse = RouteCallFuse(failure_threshold=3, cooldown_seconds=60)
        for now in (100.0, 101.0, 102.0):
            self.assertTrue(fuse.allow(now=now, calls_today=0, max_daily_calls=100))
            fuse.fail(now=now)

        self.assertFalse(fuse.allow(now=103.0, calls_today=0, max_daily_calls=100))

    def test_classifier_factory_uses_each_call_config_without_shared_adapter(self):
        from qq_openclaw_bridge.application.shadow_routing import LightweightRouteClassifier

        created = []

        def factory(config):
            adapter = RecordingAdapter(
                '{"level":"L1","confidence":0.9,"task_kind":"smalltalk",'
                '"reasons":["short"],"brainstorm_required":false}'
            )
            created.append((config["classifier_model"], config["classifier_timeout_seconds"], adapter))
            return adapter

        classifier = LightweightRouteClassifier(adapter_factory=factory)
        classifier.classify(self.contract(), self.routing_config())
        classifier.classify(
            self.contract(),
            self.routing_config(classifier_model="openai/other", classifier_timeout_seconds=30),
        )

        self.assertEqual([(item[0], item[1]) for item in created], [
            ("openai/gpt-5.4-nano", 15), ("openai/other", 30),
        ])
        self.assertIsNot(created[0][2], created[1][2])


class ShadowRoutingObserverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WorkspaceStateStore(Path(self.temp_dir.name) / "state.sqlite3")
        self.repository = self.store.routing_repository

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self):
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(10001, "source-1", "请分析这个方案", 100.0)

    def routing_config(self, **overrides):
        config = {
            "enabled": True,
            "classifier_enabled": True,
            "classifier_model": "openai/gpt-5.4-nano",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 15,
            "max_daily_calls": 100,
            "failure_threshold": 3,
            "cooldown_seconds": 300,
            "rule_version": "f1-v1",
        }
        config.update(overrides)
        return config

    def observer(
        self, classifier=None, resolver=None, rule_router=None,
        classifier_concurrency_limit=2, **config_overrides
    ):
        from qq_openclaw_bridge.application.shadow_routing import ShadowRoutingObserver

        self.repository.create_config(
            self.routing_config(**config_overrides), activate=True
        )
        return ShadowRoutingObserver(
            self.repository,
            classifier=classifier,
            resolver=resolver,
            rule_router=rule_router,
            classifier_concurrency_limit=classifier_concurrency_limit,
        )

    async def test_schedule_does_not_wait_for_classifier(self):
        observer = self.observer(classifier=BlockingClassifier())

        started = time.perf_counter()
        overhead = observer.schedule(self.request(), local_handled=False)

        self.assertLess(time.perf_counter() - started, 0.05)
        self.assertLess(overhead, 50)
        await observer.wait()

    async def test_shutdown_cancels_and_drains_owned_classifier_work(self):
        classifier = BlockingCancellableClassifier()
        observer = self.observer(classifier=classifier)

        observer.schedule(self.request(), local_handled=False)
        self.assertTrue(await asyncio.to_thread(classifier.started.wait, 0.5))

        drained = await observer.shutdown(timeout_seconds=0.5)

        self.assertTrue(drained)
        self.assertTrue(classifier.finished.is_set())
        self.assertEqual(observer._tasks, set())
        self.assertEqual(observer._classifier_calls, set())

    async def test_shutdown_cancels_queued_classifier_before_adapter_or_process_start(self):
        from qq_openclaw_bridge.agent import OpenClawCliAgentAdapter
        from qq_openclaw_bridge.application.shadow_routing import (
            LightweightRouteClassifier,
            ShadowRoutingObserver,
        )

        reservation_started = threading.Event()
        release_reservation = threading.Event()
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        created_adapters = []
        spawned_processes = []

        class Repository:
            def __init__(self, delegate):
                self._delegate = delegate

            def __getattr__(self, name):
                return getattr(self._delegate, name)

            def reserve_classifier_attempt(self, *args, **kwargs):
                reservation_started.set()
                release_reservation.wait(1)
                return self._delegate.reserve_classifier_attempt(*args, **kwargs)

        class Process:
            returncode = 0

            def communicate(self, timeout):
                return (
                    '{"level":"L1","confidence":0.9,"task_kind":"question",'
                    '"reasons":["short"],"brainstorm_required":false}',
                    "",
                )

            def poll(self):
                return self.returncode

        def adapter_factory(config):
            adapter = OpenClawCliAgentAdapter(
                "openclaw",
                cancellable=True,
                popen_factory=lambda *args, **kwargs: (
                    spawned_processes.append(Process()) or spawned_processes[-1]
                ),
            )
            created_adapters.append(adapter)
            return adapter

        self.repository.create_config(self.routing_config(), activate=True)
        observer = ShadowRoutingObserver(
            Repository(self.repository),
            classifier=LightweightRouteClassifier(adapter_factory=adapter_factory),
        )
        executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)
        try:
            observer.schedule(self.request(), local_handled=False)
            for _ in range(50):
                if reservation_started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(reservation_started.is_set())

            blocker = asyncio.create_task(asyncio.to_thread(
                lambda: blocker_started.set() or release_blocker.wait(1)
            ))
            await asyncio.sleep(0)
            release_reservation.set()
            for _ in range(50):
                if blocker_started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(blocker_started.is_set())
            for _ in range(20):
                if observer._classifier_calls:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(observer._classifier_calls)

            self.assertTrue(await observer.shutdown(timeout_seconds=0.2))
            release_blocker.set()
            await blocker
            await asyncio.sleep(0.05)

            self.assertEqual(created_adapters, [])
            self.assertEqual(spawned_processes, [])
            self.assertEqual(observer._tasks, set())
            self.assertEqual(observer._classifier_calls, set())
        finally:
            release_reservation.set()
            release_blocker.set()
            executor.shutdown(wait=True)

    async def test_budget_exhaustion_records_rule_only_l2(self):
        classifier = CountingClassifier()
        observer = self.observer(classifier=classifier, max_daily_calls=0)

        observer.schedule(self.request(), local_handled=False)
        await observer.wait()

        row = self.repository.recent_decisions(1)[0]
        self.assertEqual(row["status"], "rule_only")
        self.assertEqual(row["final_level"], "L2")
        self.assertIn("classifier_budget_exhausted", row["reasons"])
        self.assertFalse(row["classifier_attempted"])
        self.assertEqual(classifier.calls, [])

    async def test_classifier_failure_is_recorded_without_escaping_task(self):
        observer = self.observer(classifier=FailingClassifier())

        observer.schedule(self.request(), local_handled=False)
        await observer.wait()

        row = self.repository.recent_decisions(1)[0]
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["classifier_attempted"])
        self.assertEqual(row["safe_error_class"], "invalid_classifier_response")

    async def test_duplicate_schedules_claim_one_classifier_attempt(self):
        classifier = CountingClassifier()
        observer = self.observer(classifier=classifier)

        observer.schedule(self.request(), local_handled=False)
        observer.schedule(self.request(), local_handled=False)
        await observer.wait()

        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(len(self.repository.recent_decisions(10)), 1)

    async def test_classifier_concurrency_is_independently_bounded(self):
        classifier = CountingClassifier()
        observer = self.observer(classifier=classifier, classifier_concurrency_limit=2)

        for number in range(5):
            request = self.request()
            request = request.__class__(
                request.user_id, "source-%s" % number, request.text, request.timestamp
            )
            observer.schedule(request, local_handled=False)
        await observer.wait()

        self.assertEqual(len(classifier.calls), 5)
        self.assertLessEqual(classifier.peak, 2)

    async def test_live_config_switch_and_disabled_config_control_classification(self):
        classifier = CountingClassifier()
        observer = self.observer(classifier=classifier)

        observer.schedule(self.request(), local_handled=False)
        await observer.wait()
        active = self.repository.create_config(
            self.routing_config(
                classifier_model="openai/live-model",
                classifier_timeout_seconds=30,
                rule_version="f1-v2",
            ), activate=True
        )
        next_request = self.request().__class__(10001, "source-2", "继续分析", 100.0)
        observer.schedule(next_request, local_handled=False)
        await observer.wait()
        self.repository.create_config(
            self.routing_config(enabled=False, rule_version="f1-v3"), activate=True
        )
        observer.schedule(
            self.request().__class__(10001, "source-3", "不应记录", 100.0),
            local_handled=False,
        )
        await observer.wait()

        self.assertEqual(active["config"]["classifier_model"], "openai/live-model")
        self.assertEqual([call[1]["classifier_model"] for call in classifier.calls], [
            "openai/gpt-5.4-nano", "openai/live-model",
        ])
        self.assertEqual(len(self.repository.recent_decisions(10)), 2)

    async def test_same_setting_versions_keep_their_own_circuit_state(self):
        classifier = CountingClassifier()
        first = self.repository.create_config(
            self.routing_config(rule_version="f1-v1"), activate=True
        )
        self.repository.record_classifier_circuit(
            first["version"], "cooling", now=datetime.now(timezone.utc)
        )
        from qq_openclaw_bridge.application.shadow_routing import ShadowRoutingObserver

        observer = ShadowRoutingObserver(self.repository, classifier=classifier)
        observer.schedule(self.request(), local_handled=False)
        await observer.wait()
        second = self.repository.create_config(
            self.routing_config(rule_version="f1-v2"), activate=True
        )
        observer.schedule(
            self.request().__class__(10001, "source-v2", "继续分析", 100.0),
            local_handled=False,
        )
        await observer.wait()
        self.repository.record_classifier_circuit(
            second["version"], "cooling", now=datetime.now(timezone.utc)
        )
        self.repository.activate_config(first["version"])
        observer.schedule(
            self.request().__class__(10001, "source-v1", "再次分析", 100.0),
            local_handled=False,
        )
        await observer.wait()

        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(classifier.calls[0][0], "source-v2")

    async def test_concurrent_first_requests_wait_for_persisted_circuit_load(self):
        circuit_started = threading.Event()
        release_circuit = threading.Event()

        class Repository:
            def __init__(self, delegate):
                self._delegate = delegate

            def __getattr__(self, name):
                return getattr(self._delegate, name)

            def classifier_circuit(self, *args, **kwargs):
                circuit_started.set()
                release_circuit.wait(1)
                return self._delegate.classifier_circuit(*args, **kwargs)

        classifier = CountingClassifier()
        self.repository.create_config(self.routing_config(), activate=True)
        from qq_openclaw_bridge.application.shadow_routing import ShadowRoutingObserver

        observer = ShadowRoutingObserver(Repository(self.repository), classifier=classifier)
        observer.schedule(self.request(), local_handled=False)
        observer.schedule(
            self.request().__class__(10001, "source-concurrent", "并发分析", 100.0),
            local_handled=False,
        )
        self.assertTrue(await asyncio.to_thread(circuit_started.wait, 0.5))
        await asyncio.sleep(0.08)

        self.assertEqual(classifier.calls, [])
        release_circuit.set()
        await observer.wait()
        self.assertEqual(len(classifier.calls), 2)

    async def test_restart_recovers_expired_cooling_without_a_successful_classifier_call(self):
        now = [2_000.0]
        active = self.repository.create_config(
            self.routing_config(max_daily_calls=0, cooldown_seconds=300), activate=True
        )
        self.repository.record_classifier_circuit(
            active["version"],
            "cooling",
            now=datetime.fromtimestamp(now[0] - 301, timezone.utc),
        )
        classifier = CountingClassifier()
        from qq_openclaw_bridge.application.shadow_routing import ShadowRoutingObserver

        observer = ShadowRoutingObserver(
            self.repository, classifier=classifier, now_provider=lambda: now[0]
        )
        observer.schedule(self.request(), local_handled=False)
        await observer.wait()

        self.assertEqual(classifier.calls, [])
        self.assertEqual(
            self.repository.classifier_circuit(active["version"])["state"], "closed"
        )

    async def test_post_classifier_failure_records_attempted_error(self):
        observer = self.observer(
            classifier=CountingClassifier(), resolver=ExplodingResolver()
        )

        observer.schedule(self.request(), local_handled=False)
        await observer.wait()

        row = self.repository.recent_decisions(1)[0]
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["classifier_attempted"])
        self.assertEqual(row["safe_error_class"], "classifier_finalize_error")

    async def test_rule_failure_uses_controlled_error_category_without_attempt(self):
        observer = self.observer(rule_router=ExplodingRuleRouter())

        observer.schedule(self.request(), local_handled=False)
        await observer.wait()

        row = self.repository.recent_decisions(1)[0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["safe_error_class"], "rule_error")
        self.assertFalse(row["classifier_attempted"])

    async def test_classifier_reason_secret_is_not_persisted(self):
        secret = "api_key=shadow-secret-123"
        observer = self.observer(classifier=CountingClassifier(reason=secret))

        observer.schedule(self.request(), local_handled=False)
        await observer.wait()

        row = self.repository.recent_decisions(1)[0]
        self.assertNotIn(secret, row["reasons"])
        with self.store._connect() as connection:
            stored = connection.execute(
                "SELECT reasons_json FROM route_decisions WHERE id=?", (row["id"],)
            ).fetchone()[0]
        self.assertNotIn(secret, stored)


if __name__ == "__main__":
    unittest.main()
