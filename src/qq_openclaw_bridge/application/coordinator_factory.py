import asyncio
import subprocess
from dataclasses import replace
from typing import Awaitable, Callable, Optional

from ..identity import identity_reply
from ..models import AgentRequest, AgentResponse
from ..orchestration.config import OrchestrationConfig
from ..orchestration.direct import (
    CompatibilityVerificationStrategy,
    DirectPlanningStrategy,
    ExistingLimitBudgetPolicy,
    SingleAgentExecutionEngine,
    StaticCapabilityRegistry,
)
from ..orchestration.runtime import OrchestrationRuntime
from .commands import CallableCommandHandler, CommandChain
from .coordinator import MessageCoordinator, SecurityInterception


TRUNCATED_SUFFIX = "（已截断）"
TIMEOUT_FALLBACK_REPLY = (
    "OpenClaw 这轮卡住了，八成是模型或网络在磨蹭。先别急着砸键盘，"
    "我这边已经收手了；你可以稍后再问，或者检查代理和日志。"
)
PROXY_FALLBACK_REPLY = (
    "chichu 这边没连上模型，多半是代理、地区或网络线路在闹脾气。"
    "先看看 Clash 和代理端口，别让小麻烦升级成大爆炸。"
)
AUTH_FALLBACK_REPLY = (
    "模型钥匙像是没带对，API Key 或 OpenClaw 登录状态需要检查。"
    "这事不玄学，先看配置和日志。"
)


class AgentCallProxy:
    def __init__(self) -> None:
        self._target = None

    def bind(
        self, target: Callable[[AgentRequest], Awaitable[AgentResponse]]
    ) -> None:
        if self._target is not None:
            raise RuntimeError("agent call proxy is already bound")
        if not callable(target):
            raise ValueError("agent call target must be callable")
        self._target = target

    async def __call__(self, request: AgentRequest) -> AgentResponse:
        if self._target is None:
            raise RuntimeError("agent call proxy is not bound")
        return await self._target(request)


class _NullTraceSink:
    def start(self, contract, mode, config_version):
        return None

    def transition(self, run_id, stage, detail=""):
        return None

    def finish(self, run_id, status, detail=""):
        return None


def create_direct_runtime(
    agent_call, trace_sink=None, config_version: int = 1
) -> OrchestrationRuntime:
    return OrchestrationRuntime(
        planner=DirectPlanningStrategy(),
        executor=SingleAgentExecutionEngine(agent_call),
        verifier=CompatibilityVerificationStrategy(),
        budget_policy=ExistingLimitBudgetPolicy(),
        capability_registry=StaticCapabilityRegistry("openclaw-agent"),
        trace_sink=trace_sink or _NullTraceSink(),
        config=OrchestrationConfig("direct", config_version),
    )


def create_message_coordinator(
    *,
    agent_call,
    command_chain,
    fallback_mapper,
    truncator,
    security_interceptor=None,
    context_enricher=None,
    conversation_observer=None,
    orchestration_runtime=None,
    action_intent_repository=None,
    shadow_routing_observer=None,
    logger=None,
) -> MessageCoordinator:
    legacy = orchestration_runtime is None
    return MessageCoordinator(
        command_chain=command_chain,
        planner=DirectPlanningStrategy() if legacy else None,
        executor=SingleAgentExecutionEngine(agent_call) if legacy else None,
        verifier=CompatibilityVerificationStrategy() if legacy else None,
        fallback_mapper=fallback_mapper,
        truncator=truncator,
        security_interceptor=security_interceptor,
        context_enricher=context_enricher,
        conversation_observer=conversation_observer,
        orchestration_runtime=orchestration_runtime,
        action_intent_repository=action_intent_repository,
        shadow_routing_observer=shadow_routing_observer,
        logger=logger,
    )


def create_command_chain(
    *, style_memory, housekeeper, memory_runtime, media_runtime, logger
) -> CommandChain:
    handlers = []
    if media_runtime is not None and media_runtime.task_commands is not None:
        handlers.append(media_runtime.task_commands)
    handlers.append(
        CallableCommandHandler(lambda request: identity_reply(request.text))
    )
    memory_command = getattr(memory_runtime, "command_handler", None)
    if memory_command is not None:
        handlers.append(memory_command)

    def housekeeper_command(request):
        if housekeeper is None:
            return None
        return housekeeper.handle(request.text, user_id=request.user_id)

    def style_command(request):
        handler = getattr(style_memory, "handle_command", None)
        if handler is None:
            return None
        return handler(request.text)

    def style_feedback(request):
        if style_memory is None:
            return None
        try:
            return style_memory.record_if_feedback(request.text)
        except Exception:
            logger.exception("style memory update failed")
            return None

    handlers.extend(
        (
            CallableCommandHandler(housekeeper_command),
            CallableCommandHandler(style_command),
            CallableCommandHandler(style_feedback),
        )
    )
    return CommandChain(tuple(handlers))


def create_fallback_mapper(config, logger):
    def fallback(error: Exception) -> str:
        logger.exception("agent adapter failed")
        if isinstance(error, subprocess.TimeoutExpired):
            return TIMEOUT_FALLBACK_REPLY
        detail = str(error).lower()
        if any(marker in detail for marker in (
            "unsupported_region", "unsupported_country", "country_region",
            "country, region", "proxy", "network route", "connect timeout",
            "fetch timeout",
        )):
            return PROXY_FALLBACK_REPLY
        if any(marker in detail for marker in (
            "api key", "apikey", "unauthorized", "invalid_api_key",
            "no api key", "401",
        )):
            return AUTH_FALLBACK_REPLY
        return config.fallback_reply

    return fallback


def create_truncator(config):
    def truncate(text: str) -> str:
        if len(text) <= config.max_reply_chars:
            return text
        if config.max_reply_chars <= len(TRUNCATED_SUFFIX):
            return TRUNCATED_SUFFIX[: config.max_reply_chars]
        keep = config.max_reply_chars - len(TRUNCATED_SUFFIX)
        return text[:keep] + TRUNCATED_SUFFIX

    return truncate


def create_security_interceptor(confirmation_guard, logger):
    if confirmation_guard is None:
        return None

    def intercept(request: AgentRequest) -> SecurityInterception:
        decision = confirmation_guard.evaluate(
            request.user_id,
            request.text,
            message_id=request.message_id,
            session_key=request.session_key,
        )
        if decision.reply is not None:
            logger.info("security confirmation intercepted user=%s", request.user_id)
            return SecurityInterception(request=request, reply=decision.reply)
        if decision.command is not None:
            logger.info("security confirmation approved user=%s", request.user_id)
            request = replace(request, text=decision.command)
        return SecurityInterception(
            request=request,
            execution_token=decision.execution_token,
        )

    return intercept


def create_conversation_observer(memory_runtime, logger):
    learning_service = getattr(memory_runtime, "learning_service", None)
    if learning_service is None:
        return None

    async def observe(request: AgentRequest, reply: str) -> None:
        try:
            await learning_service.observe(request, reply)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("E3 memory observation failed")

    return observe


__all__ = [
    "AgentCallProxy",
    "create_command_chain",
    "create_conversation_observer",
    "create_direct_runtime",
    "create_fallback_mapper",
    "create_message_coordinator",
    "create_security_interceptor",
    "create_truncator",
]
