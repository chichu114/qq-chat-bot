from pathlib import Path
from dataclasses import replace
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _Planner:
    def __init__(self):
        self.contracts = []

    def plan(self, contract):
        from qq_openclaw_bridge.orchestration.direct import DirectPlanningStrategy

        self.contracts.append(contract)
        return DirectPlanningStrategy().plan(contract)


class _Executor:
    def __init__(self, text="模型回复", error=None):
        self.text = text
        self.error = error
        self.plans = []

    async def execute(self, plan):
        from qq_openclaw_bridge.orchestration.models import ExecutionResult

        self.plans.append(plan)
        if self.error is not None:
            raise self.error
        return ExecutionResult(self.text)


class _Verifier:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = []

    def verify(self, contract, result):
        from qq_openclaw_bridge.orchestration.models import VerificationReport

        self.calls.append((contract, result))
        return VerificationReport(self.accepted, "rejected" if not self.accepted else "")


class _CommandChain:
    def __init__(self, reply=None):
        self.reply = reply
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        return self.reply


class _ShadowObserver:
    def __init__(self):
        self.scheduled = []
        self.waits = 0

    def schedule(self, request, local_handled):
        self.scheduled.append((request, local_handled))
        return 0.0

    async def wait(self):
        self.waits += 1


class ApplicationCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, text="原始消息"):
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(10001, "m1", text, 10.0)

    def _coordinator(
        self,
        chain,
        planner,
        executor,
        verifier,
        security=None,
        context_enricher=None,
        conversation_observer=None,
        shadow_routing_observer=None,
    ):
        from qq_openclaw_bridge.application.coordinator import MessageCoordinator

        return MessageCoordinator(
            command_chain=chain,
            planner=planner,
            executor=executor,
            verifier=verifier,
            fallback_mapper=lambda exc: "兜底：%s" % exc,
            truncator=lambda text: text[:20],
            security_interceptor=security,
            context_enricher=context_enricher,
            conversation_observer=conversation_observer,
            shadow_routing_observer=shadow_routing_observer,
        )

    async def test_local_reply_bypasses_planning_and_execution(self):
        chain = _CommandChain("本地回复")
        planner = _Planner()
        executor = _Executor()
        verifier = _Verifier()
        sent = []
        coordinator = self._coordinator(chain, planner, executor, verifier)

        await coordinator.handle(
            self._request(), lambda request, text: _capture(sent, request, text)
        )

        self.assertEqual(planner.contracts, [])
        self.assertEqual(executor.plans, [])
        self.assertEqual(verifier.calls, [])
        self.assertEqual(sent[0][1], "本地回复")

    async def test_shadow_observer_marks_local_reply_without_model_work(self):
        chain = _CommandChain("本地回复")
        planner = _Planner()
        executor = _Executor()
        verifier = _Verifier()
        observer = _ShadowObserver()
        coordinator = self._coordinator(
            chain, planner, executor, verifier, shadow_routing_observer=observer
        )

        resolution = await coordinator.resolve_prepared(
            coordinator.prepare(self._request())
        )

        self.assertEqual(resolution.reply, "本地回复")
        self.assertEqual([(request.message_id, local) for request, local in observer.scheduled], [
            ("m1", True)
        ])
        self.assertEqual(planner.contracts, [])
        self.assertEqual(executor.plans, [])

    async def test_shadow_observer_marks_model_reply_once_and_waits_for_shutdown(self):
        observer = _ShadowObserver()
        planner = _Planner()
        executor = _Executor("模型完成")
        coordinator = self._coordinator(
            _CommandChain(), planner, executor, _Verifier(),
            shadow_routing_observer=observer,
        )

        resolution = await coordinator.resolve_prepared(
            coordinator.prepare(self._request())
        )
        await coordinator.wait_for_shadow_tasks()

        self.assertEqual(resolution.reply, "模型完成")
        self.assertEqual([(request.message_id, local) for request, local in observer.scheduled], [
            ("m1", False)
        ])
        self.assertEqual(len(executor.plans), 1)
        self.assertEqual(observer.waits, 1)

    async def test_model_reply_uses_contract_plan_execution_and_verification(self):
        chain = _CommandChain()
        planner = _Planner()
        executor = _Executor("模型完成")
        verifier = _Verifier()
        sent = []
        coordinator = self._coordinator(chain, planner, executor, verifier)

        await coordinator.handle(
            self._request(), lambda request, text: _capture(sent, request, text)
        )

        self.assertEqual(len(planner.contracts), 1)
        self.assertEqual(planner.contracts[0].goal, "原始消息")
        self.assertEqual(len(executor.plans), 1)
        self.assertEqual(len(verifier.calls), 1)
        self.assertEqual(sent[0][1], "模型完成")

    async def test_execution_error_and_verification_rejection_use_fallback(self):
        for executor, verifier, expected in (
            (_Executor(error=RuntimeError("model down")), _Verifier(), "兜底：model down"),
            (_Executor(""), _Verifier(accepted=False), "兜底：rejected"),
        ):
            with self.subTest(expected=expected):
                sent = []
                coordinator = self._coordinator(
                    _CommandChain(), _Planner(), executor, verifier
                )
                await coordinator.handle(
                    self._request(),
                    lambda request, text: _capture(sent, request, text),
                )
                self.assertEqual(sent[0][1], expected)

    async def test_security_can_reply_or_replace_request_before_commands(self):
        from qq_openclaw_bridge.application.coordinator import SecurityInterception
        from dataclasses import replace

        sent = []
        coordinator = self._coordinator(
            _CommandChain(),
            _Planner(),
            _Executor(),
            _Verifier(),
            security=lambda request: SecurityInterception(
                request=request, reply="需要确认"
            ),
        )
        await coordinator.handle(
            self._request(), lambda request, text: _capture(sent, request, text)
        )
        self.assertEqual(sent[0][1], "需要确认")

        chain = _CommandChain("已执行本地命令")
        coordinator = self._coordinator(
            chain,
            _Planner(),
            _Executor(),
            _Verifier(),
            security=lambda request: SecurityInterception(
                request=replace(request, text="批准后的命令")
            ),
        )
        await coordinator.handle(
            self._request(), lambda request, text: _capture([], request, text)
        )
        self.assertEqual(chain.requests[0].text, "批准后的命令")

    async def test_context_is_added_only_after_local_commands_and_reply_is_observed(self):
        from dataclasses import replace

        observed = []

        async def observer(request, reply):
            observed.append((request, reply))

        coordinator = self._coordinator(
            _CommandChain(),
            _Planner(),
            _Executor("模型完成"),
            _Verifier(),
            context_enricher=lambda request: replace(request, context="相关记忆"),
            conversation_observer=observer,
        )
        sent = []

        await coordinator.handle(
            self._request(), lambda request, text: _capture(sent, request, text)
        )

        self.assertEqual(sent[0][0].context, "相关记忆")
        self.assertEqual(observed[0][0].text, "原始消息")
        self.assertEqual(observed[0][1], "模型完成")

        local_observed = []

        async def local_observer(request, reply):
            local_observed.append((request, reply))

        local = self._coordinator(
            _CommandChain("本地回复"),
            _Planner(),
            _Executor(),
            _Verifier(),
            context_enricher=lambda request: (_ for _ in ()).throw(
                AssertionError("local command must not be enriched")
            ),
            conversation_observer=local_observer,
        )
        await local.handle(
            self._request(), lambda request, text: _capture([], request, text)
        )
        self.assertEqual(local_observed[0][1], "本地回复")

    async def test_resolution_boundary_does_not_send_or_observe_until_delivery(self):
        from dataclasses import replace

        observed = []

        async def observer(request, reply):
            observed.append((request, reply))

        coordinator = self._coordinator(
            _CommandChain(),
            _Planner(),
            _Executor("边界回复"),
            _Verifier(),
            context_enricher=lambda request: replace(request, context="E3 context"),
            conversation_observer=observer,
        )
        sent = []

        prepared = coordinator.prepare(self._request())
        self.assertIsNone(prepared.reply)
        resolution = await coordinator.resolve_prepared(prepared)

        self.assertEqual(resolution.request.context, "E3 context")
        self.assertEqual(resolution.reply, "边界回复")
        self.assertEqual(sent, [])
        self.assertEqual(observed, [])

        await coordinator.deliver(
            resolution, lambda request, text: _capture(sent, request, text)
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(observed), 1)
        self.assertIs(sent[0][0], observed[0][0])

    async def test_prepared_request_can_be_enriched_without_repeating_commands(self):
        from dataclasses import replace

        chain = _CommandChain()
        coordinator = self._coordinator(
            chain, _Planner(), _Executor("完成"), _Verifier()
        )
        prepared = coordinator.prepare(self._request())
        prepared = replace(
            prepared,
            request=replace(
                prepared.request,
                media_context="E4 context",
                task_id="task-1",
            ),
        )

        resolution = await coordinator.resolve_prepared(prepared)

        self.assertEqual(len(chain.requests), 1)
        self.assertEqual(resolution.request.media_context, "E4 context")
        self.assertEqual(resolution.request.task_id, "task-1")

    async def test_action_intent_finishes_only_after_local_handler_returns(self):
        from qq_openclaw_bridge.application.coordinator import (
            MessageCoordinator,
            SecurityInterception,
        )

        token = SimpleNamespace(
            intent_id="intent-1",
            user_id=10001,
            payload_sha256="a" * 64,
            config_version=1,
        )

        class Repository:
            def __init__(self):
                self.calls = []

            def finish(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        repository = Repository()
        planner = _Planner()
        coordinator = MessageCoordinator(
            command_chain=_CommandChain("已删除"),
            planner=planner,
            executor=_Executor(),
            verifier=_Verifier(),
            fallback_mapper=lambda exc: "fallback",
            truncator=lambda text: text,
            security_interceptor=lambda request: SecurityInterception(
                request=replace(request, text="删除记忆 1"),
                execution_token=token,
            ),
            action_intent_repository=repository,
        )

        prepared = coordinator.prepare(self._request("确认执行 A1B2C3"))

        self.assertEqual(prepared.reply, "已删除")
        self.assertEqual(planner.contracts, [])
        self.assertEqual(repository.calls[0][0][4], "succeeded")

    async def test_unhandled_action_intent_fails_closed_before_model(self):
        from qq_openclaw_bridge.application.coordinator import (
            MessageCoordinator,
            SecurityInterception,
        )

        token = SimpleNamespace(
            intent_id="intent-1",
            user_id=10001,
            payload_sha256="a" * 64,
            config_version=1,
        )

        class Repository:
            def __init__(self):
                self.statuses = []

            def finish(self, *args, **kwargs):
                self.statuses.append(args[4])

        repository = Repository()
        planner = _Planner()
        coordinator = MessageCoordinator(
            command_chain=_CommandChain(None),
            planner=planner,
            executor=_Executor(),
            verifier=_Verifier(),
            fallback_mapper=lambda exc: "fallback",
            truncator=lambda text: text,
            security_interceptor=lambda request: SecurityInterception(
                request=replace(request, text="删除记忆 1"),
                execution_token=token,
            ),
            action_intent_repository=repository,
        )

        prepared = coordinator.prepare(self._request("确认执行 A1B2C3"))

        self.assertIn("未执行", prepared.reply)
        self.assertEqual(repository.statuses, ["failed"])
        self.assertEqual(planner.contracts, [])

    async def test_handler_failure_and_terminal_write_failure_require_manual_check(self):
        from qq_openclaw_bridge.application.coordinator import (
            MessageCoordinator,
            SecurityInterception,
        )

        token = SimpleNamespace(
            intent_id="intent-1",
            user_id=10001,
            payload_sha256="a" * 64,
            config_version=1,
        )

        class FailingChain:
            def handle(self, request):
                raise RuntimeError("side effect state is unknown")

        class FailingRepository:
            def finish(self, *args, **kwargs):
                raise RuntimeError("database unavailable")

        logger = MagicMock()
        planner = _Planner()
        coordinator = MessageCoordinator(
            command_chain=FailingChain(),
            planner=planner,
            executor=_Executor(),
            verifier=_Verifier(),
            fallback_mapper=lambda exc: "fallback",
            truncator=lambda text: text,
            security_interceptor=lambda request: SecurityInterception(
                request=replace(request, text="删除记忆 1"),
                execution_token=token,
            ),
            action_intent_repository=FailingRepository(),
            logger=logger,
        )

        prepared = coordinator.prepare(self._request("确认执行 A1B2C3"))

        self.assertIn("状态未知", prepared.reply)
        self.assertIn("人工核验", prepared.reply)
        self.assertIn("暂勿重复", prepared.reply)
        self.assertNotIn("重新发起", prepared.reply)
        logger.critical.assert_called_once()
        self.assertEqual(planner.contracts, [])

    async def test_handler_exception_marks_action_unknown_instead_of_failed(self):
        from qq_openclaw_bridge.application.coordinator import (
            MessageCoordinator,
            SecurityInterception,
        )

        token = SimpleNamespace(
            intent_id="intent-1",
            user_id=10001,
            payload_sha256="a" * 64,
            config_version=1,
        )

        class FailingChain:
            def handle(self, request):
                raise RuntimeError("handler may have mutated state")

        class Repository:
            def __init__(self):
                self.unknown = []
                self.finished = []

            def mark_unknown(self, *args, **kwargs):
                self.unknown.append((args, kwargs))

            def finish(self, *args, **kwargs):
                self.finished.append((args, kwargs))

        repository = Repository()
        coordinator = MessageCoordinator(
            command_chain=FailingChain(),
            planner=_Planner(),
            executor=_Executor(),
            verifier=_Verifier(),
            fallback_mapper=lambda exc: "fallback",
            truncator=lambda text: text,
            security_interceptor=lambda request: SecurityInterception(
                request=replace(request, text="删除记忆 1"),
                execution_token=token,
            ),
            action_intent_repository=repository,
        )

        prepared = coordinator.prepare(self._request("确认执行 A1B2C3"))

        self.assertIn("状态未知", prepared.reply)
        self.assertEqual(len(repository.unknown), 1)
        self.assertEqual(repository.finished, [])

    async def test_confirmed_housekeeper_handler_exception_is_not_swallowed(self):
        from qq_openclaw_bridge.application.coordinator_factory import (
            create_command_chain,
        )

        class Housekeeper:
            def handle(self, text, user_id):
                raise RuntimeError("write completed before crash")

        chain = create_command_chain(
            style_memory=None,
            housekeeper=Housekeeper(),
            memory_runtime=None,
            media_runtime=None,
            logger=MagicMock(),
        )

        with self.assertRaisesRegex(RuntimeError, "write completed"):
            chain.handle(self._request("删除记忆 1"))

    async def test_successful_side_effect_with_terminal_write_failure_blocks_retry(self):
        from qq_openclaw_bridge.application.coordinator import (
            MessageCoordinator,
            SecurityInterception,
        )

        token = SimpleNamespace(
            intent_id="intent-1",
            user_id=10001,
            payload_sha256="a" * 64,
            config_version=1,
        )

        class FailingRepository:
            def finish(self, *args, **kwargs):
                raise RuntimeError("database unavailable")

        logger = MagicMock()
        coordinator = MessageCoordinator(
            command_chain=_CommandChain("已删除"),
            planner=_Planner(),
            executor=_Executor(),
            verifier=_Verifier(),
            fallback_mapper=lambda exc: "fallback",
            truncator=lambda text: text,
            security_interceptor=lambda request: SecurityInterception(
                request=replace(request, text="删除记忆 1"),
                execution_token=token,
            ),
            action_intent_repository=FailingRepository(),
            logger=logger,
        )

        prepared = coordinator.prepare(self._request("确认执行 A1B2C3"))

        self.assertIn("可能已经执行", prepared.reply)
        self.assertIn("别重复", prepared.reply)
        self.assertNotIn("重新发起", prepared.reply)
        logger.critical.assert_called_once()


async def _capture(target, request, text):
    target.append((request, text))


if __name__ == "__main__":
    unittest.main()
