import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ..models import AgentRequest
from ..orchestration.models import TaskContract
from ..orchestration.ports import (
    ExecutionEngine,
    PlanningStrategy,
    Runtime,
    VerificationStrategy,
)
from .commands import CommandHandler


@dataclass(frozen=True)
class SecurityInterception:
    request: AgentRequest
    reply: Optional[str] = None
    execution_token: Optional[object] = None


@dataclass(frozen=True)
class PreparedMessage:
    request: AgentRequest
    reply: Optional[str] = None


@dataclass(frozen=True)
class CoordinatorResolution:
    request: AgentRequest
    reply: str


class MessageCoordinator:
    def __init__(
        self,
        command_chain: CommandHandler,
        fallback_mapper: Callable[[Exception], str],
        truncator: Callable[[str], str],
        planner: Optional[PlanningStrategy] = None,
        executor: Optional[ExecutionEngine] = None,
        verifier: Optional[VerificationStrategy] = None,
        security_interceptor: Optional[
            Callable[[AgentRequest], SecurityInterception]
        ] = None,
        contract_factory: Callable[[AgentRequest], TaskContract] = (
            TaskContract.from_request
        ),
        context_enricher: Optional[Callable[[AgentRequest], AgentRequest]] = None,
        conversation_observer: Optional[
            Callable[[AgentRequest, str], Awaitable[None]]
        ] = None,
        orchestration_runtime: Optional[Runtime] = None,
        action_intent_repository: Optional[object] = None,
        shadow_routing_observer: Optional[object] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        legacy_parts = (planner, executor, verifier)
        has_legacy = all(part is not None for part in legacy_parts)
        has_partial_legacy = any(part is not None for part in legacy_parts)
        if has_partial_legacy and not has_legacy:
            raise ValueError("legacy orchestration path must be complete")
        if (orchestration_runtime is None) == (not has_legacy):
            raise ValueError("provide exactly one orchestration path")
        self._command_chain = command_chain
        self._planner = planner
        self._executor = executor
        self._verifier = verifier
        self._fallback_mapper = fallback_mapper
        self._truncator = truncator
        self._security_interceptor = security_interceptor
        self._contract_factory = contract_factory
        self._context_enricher = context_enricher
        self._conversation_observer = conversation_observer
        self._orchestration_runtime = orchestration_runtime
        self._action_intent_repository = action_intent_repository
        self._shadow_routing_observer = shadow_routing_observer
        self._logger = logger or logging.getLogger(__name__)

    @property
    def orchestration_runtime(self) -> Optional[Runtime]:
        return self._orchestration_runtime

    @property
    def has_shadow_routing_observer(self) -> bool:
        return self._shadow_routing_observer is not None

    async def handle(
        self,
        request: AgentRequest,
        reply_sink: Callable[[AgentRequest, str], Awaitable[None]],
    ) -> bool:
        resolution = await self.resolve_prepared(self.prepare(request))
        await self.deliver(resolution, reply_sink)
        return True

    def prepare(self, request: AgentRequest) -> PreparedMessage:
        active_request = request
        reply_text = None
        execution_token = None
        if self._security_interceptor is not None:
            interception = self._security_interceptor(request)
            active_request = interception.request
            reply_text = interception.reply
            execution_token = interception.execution_token

        if reply_text is None:
            if execution_token is not None:
                reply_text = self._execute_action_intent(
                    active_request, execution_token
                )
            else:
                reply_text = self._command_chain.handle(active_request)
        return PreparedMessage(active_request, reply_text)

    def _execute_action_intent(self, request, token) -> str:
        repository = self._action_intent_repository
        if repository is None:
            return "审批动作未执行：安全状态仓库不可用。别重复确认，先检查服务。"
        try:
            reply = self._command_chain.handle(request)
        except Exception as error:
            persisted = self._mark_action_unknown(
                token,
                error.__class__.__name__,
                "local action handler raised after side effect may have started",
            )
            if not persisted:
                return "动作执行状态未知。请先人工核验实际结果，暂勿重复发起。"
            return "动作执行状态未知。请先人工核验实际结果，暂勿重复发起。"
        if reply is None:
            persisted = self._finish_action(
                token,
                "failed",
                "UnhandledLocalAction",
                "local action was not handled",
            )
            if not persisted:
                return "动作执行状态未知。请先人工核验实际结果，暂勿重复发起。"
            return "审批动作未执行：没有匹配到受限本地处理器。"
        try:
            repository.finish(
                token.intent_id,
                token.user_id,
                token.payload_sha256,
                token.config_version,
                "succeeded",
                "Success",
                "local action completed",
            )
        except Exception:
            self._logger.critical(
                "action intent success persistence failed intent_id=%s",
                token.intent_id,
            )
            return "动作可能已经执行，但状态记录失败。先检查结果，别重复操作。"
        return reply

    def _finish_action(
        self, token, status: str, result_class: str, summary: str
    ) -> bool:
        try:
            self._action_intent_repository.finish(
                token.intent_id,
                token.user_id,
                token.payload_sha256,
                token.config_version,
                status,
                result_class,
                summary,
            )
            return True
        except Exception:
            self._logger.critical(
                "action intent terminal persistence failed intent_id=%s",
                token.intent_id,
            )
            return False

    def _mark_action_unknown(self, token, result_class: str, summary: str) -> bool:
        try:
            self._action_intent_repository.mark_unknown(
                token.intent_id,
                token.user_id,
                token.payload_sha256,
                token.config_version,
                result_class,
                summary,
            )
            return True
        except Exception:
            self._logger.critical(
                "action intent unknown persistence failed intent_id=%s",
                token.intent_id,
            )
            return False

    async def resolve_prepared(
        self, prepared: PreparedMessage, map_errors: bool = True
    ) -> CoordinatorResolution:
        active_request = prepared.request
        reply_text = prepared.reply
        if self._shadow_routing_observer is not None:
            try:
                self._shadow_routing_observer.schedule(
                    active_request, local_handled=reply_text is not None
                )
            except Exception:
                self._logger.exception("shadow routing schedule failed")
        if reply_text is None:
            try:
                if self._context_enricher is not None:
                    active_request = self._context_enricher(active_request)
                contract = self._contract_factory(active_request)
                if self._orchestration_runtime is not None:
                    result = await self._orchestration_runtime.execute(contract)
                else:
                    assert self._planner is not None
                    assert self._executor is not None
                    assert self._verifier is not None
                    plan = self._planner.plan(contract)
                    result = await self._executor.execute(plan)
                    report = self._verifier.verify(contract, result)
                    if not report.accepted:
                        raise RuntimeError(report.reason or "agent reply rejected")
                reply_text = result.text
            except Exception as exc:
                if not map_errors:
                    raise
                reply_text = self._fallback_mapper(exc)

        final_reply = self._truncator(reply_text)
        return CoordinatorResolution(active_request, final_reply)

    async def wait_for_shadow_tasks(self) -> None:
        if self._shadow_routing_observer is not None:
            await self._shadow_routing_observer.wait()

    async def shutdown_shadow_routing(self, timeout_seconds: float) -> bool:
        if self._shadow_routing_observer is None:
            return True
        return await self._shadow_routing_observer.shutdown(timeout_seconds)

    async def deliver(
        self,
        resolution: CoordinatorResolution,
        reply_sink: Callable[[AgentRequest, str], Awaitable[None]],
    ) -> bool:
        delivered = await reply_sink(resolution.request, resolution.reply)
        if delivered is not False:
            await self.observe(resolution)
        return delivered is not False

    async def observe(self, resolution: CoordinatorResolution) -> None:
        if self._conversation_observer is not None:
            await self._conversation_observer(
                resolution.request, resolution.reply
            )
