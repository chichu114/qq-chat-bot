"""Best-effort shadow routing that never participates in Direct delivery."""

import asyncio
import hashlib
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Set

from ..domain.routing import (
    ClassifierDecision,
    ConservativeRouteResolver,
    DeterministicRiskRouter,
    RoutingContract,
    RuleDecision,
    TASK_KINDS,
    parse_classifier_response,
    redacted_preview,
)
from ..agent import CancellationGate
from ..models import AgentRequest


_SOURCE_HASH_PREFIX_LENGTH = 12
_PROMPT_MESSAGE_LIMIT = 4000
_PERSISTED_REASON_CODES = frozenset(
    {
        "local_handled",
        "input_truncated",
        "deletion",
        "external_send",
        "publishing",
        "finance",
        "credentials",
        "system_control",
        "classifier_disabled",
        "classifier_budget_exhausted",
        "classifier_fuse_open",
        "classifier_missing_fallback",
        "low_confidence_fallback",
        "model_downgrade_blocked",
    }
)


@dataclass(frozen=True)
class ClassifierCallResult:
    decision: ClassifierDecision
    provider: Optional[str]
    model: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    duration_ms: int


class LightweightRouteClassifier:
    """Call the isolated routing agent and retain only structured metadata."""

    def __init__(self, adapter=None, adapter_factory=None) -> None:
        if (adapter is None) == (adapter_factory is None):
            raise ValueError("provide exactly one classifier adapter source")
        self._adapter = adapter
        self._adapter_factory = adapter_factory
        self._active_adapters: Set[object] = set()
        self._active_adapters_lock = threading.Lock()
        self._cancellation_gate = CancellationGate()

    def classify(self, contract: RoutingContract, config) -> ClassifierCallResult:
        source_hash = hashlib.sha256(
            contract.source_message_id.encode("utf-8")
        ).hexdigest()
        request = AgentRequest(
            user_id=contract.user_id,
            message_id=contract.source_message_id,
            text=self._prompt(contract.text),
            timestamp=time.time(),
            session_key="agent:routing:qq-%s-%s" % (
                contract.user_id,
                source_hash[:_SOURCE_HASH_PREFIX_LENGTH],
            ),
        )
        started = time.perf_counter_ns()
        with self._cancellation_gate.lock:
            if self._cancellation_gate.is_closed():
                raise RuntimeError("routing classifier is shutting down")
            adapter = (
                self._adapter
                if self._adapter_factory is None
                else self._adapter_factory(config)
            )
            bind_gate = getattr(adapter, "bind_cancellation_gate", None)
            if callable(bind_gate):
                bind_gate(self._cancellation_gate)
            with self._active_adapters_lock:
                self._active_adapters.add(adapter)
        try:
            response = adapter.reply(request)
            measured_duration = (time.perf_counter_ns() - started) // 1_000_000
        finally:
            with self._active_adapters_lock:
                self._active_adapters.discard(adapter)
        decision = parse_classifier_response(response.text)
        duration = response.duration_ms
        if type(duration) is not int or duration < 0:
            duration = int(measured_duration)
        return ClassifierCallResult(
            decision=decision,
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            duration_ms=duration,
        )

    def cancel_active(self) -> None:
        self._cancel_active("cancel_active")

    def force_cancel_active(self) -> None:
        self._cancel_active("force_cancel_active")

    def _cancel_active(self, method_name: str) -> None:
        with self._cancellation_gate.lock:
            with self._active_adapters_lock:
                adapters = tuple(self._active_adapters)
            for adapter in adapters:
                method = getattr(adapter, method_name, None)
                if callable(method):
                    method()

    def bind_cancellation_gate(self, cancellation_gate: CancellationGate) -> None:
        with cancellation_gate.lock:
            if self._active_adapters:
                raise RuntimeError("cannot replace an active routing cancellation gate")
            self._cancellation_gate = cancellation_gate

    @staticmethod
    def _prompt(message: str) -> str:
        return build_routing_classifier_prompt(message)


def build_routing_classifier_prompt(message: str) -> str:
    """Build the bounded prompt shared by production routing and readiness probes."""
    bounded_message = message[:_PROMPT_MESSAGE_LIMIT]
    return (
        "你是只读路由分类器。你没有工具、执行、发送消息、写文件、配置或审批权限。\n"
        "强制风险规则会固定为 L3；不得尝试降低删除、外发、发布、财务、凭据或系统控制风险。\n"
        "分级 rubric：L1：闲聊、简单事实；L2：中等分析、有界写作或编码；"
        "L3：架构、重大决策、高影响或复杂多步。\n"
        "用户明确一起头脑风暴或架构规划，且要求先不执行时，必须为 L3 且 "
        "brainstorm_required=true。风险不得降低。\n"
        "按以下精确 JSON schema 分类，只允许 level 为 L1、L2 或 L3：\n"
        '{"level":"L1","confidence":0.0,"task_kind":"smalltalk",'
        '"reasons":["bounded_reason"],"brainstorm_required":false}\n'
        "task_kind 只能为：%s。\n" % ", ".join(TASK_KINDS)
        + "confidence 必须在 0 到 1 之间，reasons 最多 8 项，每项最多 64 字符。\n"
        + "只输出 JSON。\n\n"
        + "【原始消息】\n%s" % bounded_message
    )


class RouteCallFuse:
    """A small in-memory daily budget and consecutive-failure circuit breaker."""

    def __init__(self, failure_threshold: int, cooldown_seconds: int) -> None:
        if type(failure_threshold) is not int or failure_threshold < 1:
            raise ValueError("failure_threshold must be a positive integer")
        if type(cooldown_seconds) is not int or cooldown_seconds < 1:
            raise ValueError("cooldown_seconds must be a positive integer")
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._open_until = 0.0

    def allow(self, now: float, calls_today: int, max_daily_calls: int) -> bool:
        if type(calls_today) is not int or calls_today < 0:
            raise ValueError("calls_today must be a non-negative integer")
        if type(max_daily_calls) is not int or max_daily_calls < 0:
            raise ValueError("max_daily_calls must be a non-negative integer")
        return calls_today < max_daily_calls and now >= self._open_until

    def succeed(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0

    def fail(self, now: float) -> bool:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open_until = now + self._cooldown_seconds
        return self._open_until > now

    def restore_cooling(self, opened_at: float) -> None:
        self._open_until = opened_at + self._cooldown_seconds

    def cooling_expired(self, now: float) -> bool:
        return self._open_until > 0 and now >= self._open_until


class ShadowRoutingObserver:
    """Observe requests in tracked tasks while keeping all failures off the main path."""

    def __init__(
        self,
        repository,
        classifier=None,
        rule_router=None,
        resolver=None,
        logger: Optional[logging.Logger] = None,
        now_provider: Optional[Callable[[], float]] = None,
        classifier_concurrency_limit: int = 2,
    ) -> None:
        if type(classifier_concurrency_limit) is not int or classifier_concurrency_limit < 1:
            raise ValueError("classifier_concurrency_limit must be a positive integer")
        self._repository = repository
        self._classifier = classifier
        self._rule_router = rule_router or DeterministicRiskRouter()
        self._resolver = resolver or ConservativeRouteResolver()
        self._logger = logger or logging.getLogger("qq_openclaw_bridge")
        self._now_provider = now_provider or time.time
        self._tasks: Set[asyncio.Task] = set()
        self._classifier_calls: Set[asyncio.Task] = set()
        self._classifier_call_started = {}
        self._cancellation_gate = CancellationGate()
        bind_gate = getattr(classifier, "bind_cancellation_gate", None)
        if callable(bind_gate):
            bind_gate(self._cancellation_gate)
        self._classifier_concurrency_limit = classifier_concurrency_limit
        self._classifier_semaphore = None
        self._fuses = {}
        self._fuse_opened_at = {}
        self._fuse_lock = None

    def schedule(self, request: AgentRequest, local_handled: bool) -> float:
        """Register exactly one task and return only synchronous dispatch overhead."""
        started = time.perf_counter_ns()
        if self._cancellation_gate.is_closed():
            return (time.perf_counter_ns() - started) / 1_000_000.0
        if self._classifier_semaphore is None:
            self._classifier_semaphore = asyncio.Semaphore(
                self._classifier_concurrency_limit
            )
        dispatch_overhead = [0]
        task = asyncio.create_task(
            self._observe(request, local_handled, dispatch_overhead)
        )
        self._tasks.add(task)
        task.add_done_callback(self._consume_task)
        overhead = (time.perf_counter_ns() - started) / 1_000_000.0
        dispatch_overhead[0] = int(overhead)
        return overhead

    async def wait(self) -> None:
        await self._drain_owned_work()

    async def shutdown(self, timeout_seconds: float) -> bool:
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._cancellation_gate.close()
        self._cancel_classifier_work()
        for task in tuple(self._classifier_calls):
            started = self._classifier_call_started.get(task)
            if started is None or not started.is_set():
                task.cancel()
        for task in tuple(self._tasks):
            task.cancel()
        graceful_timeout = float(timeout_seconds) * 0.8
        try:
            await asyncio.wait_for(self._drain_owned_work(), graceful_timeout)
            return True
        except asyncio.TimeoutError:
            self._cancel_classifier_work(force=True)
            try:
                await asyncio.wait_for(
                    self._drain_owned_work(),
                    float(timeout_seconds) - graceful_timeout,
                )
                return True
            except asyncio.TimeoutError:
                self._log_failure("ShadowShutdownTimeout")
                return False

    def recover(self) -> int:
        return self._repository.recover_pending_as_interrupted()

    async def _observe(
        self, request: AgentRequest, local_handled: bool, dispatch_overhead
    ) -> None:
        try:
            active = await asyncio.to_thread(self._repository.active_config)
            if active is None:
                return
            config = active.get("config")
            version = active.get("version")
            if type(config) is not dict or type(version) is not int:
                self._log_failure("InvalidRoutingConfig")
                return
            if not config.get("enabled", False):
                return
            contract = RoutingContract.from_request(request, local_handled, version)
            decision = await asyncio.to_thread(
                self._repository.start_decision,
                user_id=contract.user_id,
                source_message_sha256=self._sha256(contract.source_message_id),
                message_sha256=self._sha256(contract.text),
                message_preview=redacted_preview(contract.text),
                routing_config_version=version,
                rule_version=config["rule_version"],
                dispatch_overhead_ms=dispatch_overhead[0],
            )
            try:
                rule = self._rule_router.evaluate(contract)
            except Exception as exc:
                await self._fail(decision, "rule_error", attempted=False)
                return

            if not rule.needs_classifier:
                await self._finish_rule_only(decision, rule, config)
                return

            denied_reason = self._classifier_disabled_reason(config)
            if denied_reason is not None:
                fallback_rule = RuleDecision(
                    level="L2",
                    needs_classifier=False,
                    reasons=rule.reasons + (denied_reason,),
                )
                await self._finish_rule_only(decision, fallback_rule, config)
                return

            fuse_key, fuse = await self._fuse_for_config(config, version)
            if fuse is None or not fuse.allow(self._now_provider(), 0, 1):
                await self._finish_rule_only(
                    decision,
                    RuleDecision(
                        level="L2",
                        needs_classifier=False,
                        reasons=rule.reasons + ("classifier_fuse_open",),
                    ),
                    config,
                )
                return
            reservation = await asyncio.to_thread(
                self._repository.reserve_classifier_attempt,
                decision["id"],
                decision["updated_at"],
                max_daily_calls=config["max_daily_calls"],
            )
            outcome = reservation["outcome"]
            if outcome == "duplicate_or_terminal":
                return
            decision = reservation["decision"]
            if outcome == "budget_exhausted":
                await self._finish_rule_only(
                    decision,
                    RuleDecision(
                        level="L2",
                        needs_classifier=False,
                        reasons=rule.reasons + ("classifier_budget_exhausted",),
                    ),
                    config,
                )
                return

            try:
                async with self._classifier_semaphore:
                    result = await self._classify(contract, config)
            except Exception as exc:
                if fuse.fail(self._now_provider()):
                    circuit = await self._record_classifier_circuit(
                        version, "cooling"
                    )
                    self._restore_fuse_circuit(fuse_key, fuse, circuit)
                await self._fail(
                    decision, self._classifier_error_class(exc), attempted=True
                )
                return

            fuse.succeed()
            circuit = await self._record_classifier_circuit(version, "closed")
            self._restore_fuse_circuit(fuse_key, fuse, circuit)
            try:
                resolution = self._resolver.resolve(
                    rule,
                    result.decision,
                    config["confidence_threshold_percent"] / 100.0,
                )
                await asyncio.to_thread(
                    self._repository.finish_decision,
                    decision["id"],
                    decision["updated_at"],
                    status="classified",
                    rule_level=rule.level,
                    model_level=result.decision.level,
                    final_level=resolution.final_level,
                    confidence=resolution.confidence,
                    task_kind=resolution.task_kind,
                    risk_flags=rule.risk_flags,
                    reasons=self._persisted_reasons(resolution.reasons),
                    brainstorm_required=resolution.brainstorm_required,
                    brainstorm_mode=resolution.brainstorm_mode,
                    classifier_provider=self._safe_identifier(result.provider, "unknown"),
                    classifier_model=self._safe_identifier(
                        result.model, config["classifier_model"]
                    ),
                    classifier_duration_ms=result.duration_ms,
                )
            except Exception:
                await self._fail(
                    decision, "classifier_finalize_error", attempted=True
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_failure(type(exc).__name__)

    async def _finish_rule_only(self, decision, rule: RuleDecision, config) -> None:
        resolution = self._resolver.resolve(
            rule, None, config["confidence_threshold_percent"] / 100.0
        )
        await asyncio.to_thread(
            self._repository.finish_decision,
            decision["id"],
            decision["updated_at"],
            status="rule_only",
            rule_level=rule.level,
            model_level=None,
            final_level=resolution.final_level,
            confidence=resolution.confidence,
            task_kind=resolution.task_kind,
            risk_flags=rule.risk_flags,
            reasons=self._persisted_reasons(resolution.reasons),
            brainstorm_required=resolution.brainstorm_required,
            brainstorm_mode=resolution.brainstorm_mode,
            classifier_provider=None,
            classifier_model=None,
            classifier_duration_ms=None,
        )

    async def _fail(self, decision, error_class: str, attempted: bool) -> None:
        try:
            await asyncio.to_thread(
                self._repository.fail_decision,
                decision["id"],
                decision["updated_at"],
                self._safe_error_class(error_class),
                classifier_attempted=attempted,
            )
        except Exception as exc:
            self._log_failure(type(exc).__name__)

    def _classifier_disabled_reason(self, config) -> Optional[str]:
        if not config.get("classifier_enabled", False) or self._classifier is None:
            return "classifier_disabled"
        return None

    async def _fuse_for_config(self, config, config_version: int):
        key = (
            config_version,
            config["failure_threshold"],
            config["cooldown_seconds"],
        )
        if self._fuse_lock is None:
            self._fuse_lock = asyncio.Lock()
        async with self._fuse_lock:
            fuse = self._fuses.get(key)
            if fuse is None:
                fuse = RouteCallFuse(key[1], key[2])
                circuit = await asyncio.to_thread(
                    self._repository.classifier_circuit, config_version
                )
                if not self._restore_fuse_circuit(key, fuse, circuit):
                    return key, None
                self._fuses[key] = fuse
            if fuse.cooling_expired(self._now_provider()):
                circuit = await asyncio.to_thread(
                    self._repository.close_classifier_circuit_if_expired,
                    config_version,
                    self._fuse_opened_at.get(key),
                    now=self._circuit_now(),
                )
                if not self._restore_fuse_circuit(key, fuse, circuit):
                    return key, None
        return key, fuse

    def _restore_fuse_circuit(self, key, fuse, circuit) -> bool:
        if not isinstance(circuit, dict):
            return False
        state = circuit.get("state")
        if state == "closed":
            fuse.succeed()
            self._fuse_opened_at.pop(key, None)
            return True
        if state != "cooling":
            return False
        opened_at = circuit.get("opened_at")
        try:
            opened = datetime.fromisoformat(str(opened_at))
            if opened.tzinfo is None or opened.utcoffset() is None:
                return False
            fuse.restore_cooling(opened.timestamp())
        except (TypeError, ValueError):
            return False
        self._fuse_opened_at[key] = str(opened_at)
        return True

    def _circuit_now(self) -> datetime:
        return datetime.fromtimestamp(self._now_provider(), tz=timezone.utc)

    async def _record_classifier_circuit(self, config_version: int, state: str):
        try:
            return await asyncio.to_thread(
                self._repository.record_classifier_circuit,
                config_version,
                state,
                now=self._circuit_now(),
            )
        except Exception as exc:
            self._log_failure(type(exc).__name__)
            return None

    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_error_class(value: str) -> str:
        return value if value in {
            "rule_error",
            "invalid_classifier_response",
            "classifier_timeout",
            "classifier_error",
            "classifier_finalize_error",
        } else "classifier_error"

    @staticmethod
    def _classifier_error_class(error: Exception) -> str:
        if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            return "classifier_timeout"
        if isinstance(error, ValueError):
            return "invalid_classifier_response"
        return "classifier_error"

    @staticmethod
    def _safe_identifier(value, fallback: str) -> str:
        if (
            type(value) is str
            and len(value) <= 128
            and value
            and all(char.isalnum() or char in "._-/" for char in value)
        ):
            return value
        return fallback

    @staticmethod
    def _persisted_reasons(reasons) -> tuple:
        return tuple(reason for reason in reasons if reason in _PERSISTED_REASON_CODES)

    def _consume_task(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self._log_failure(type(exc).__name__)

    async def _classify(self, contract, config):
        started = threading.Event()

        def classify():
            with self._cancellation_gate.lock:
                started.set()
                if self._cancellation_gate.is_closed():
                    raise RuntimeError("routing classifier is shutting down")
            return self._classifier.classify(contract, config)

        task = asyncio.create_task(
            asyncio.to_thread(classify)
        )
        self._classifier_calls.add(task)
        self._classifier_call_started[task] = started
        task.add_done_callback(self._consume_classifier_call)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._cancel_classifier_work()
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise

    async def _drain_owned_work(self) -> None:
        while self._tasks or self._classifier_calls:
            tasks = tuple(self._tasks | self._classifier_calls)
            if not tasks:
                return
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks), return_exceptions=True
            )

    def _cancel_classifier_work(self, force: bool = False) -> None:
        if self._classifier is None:
            return
        method = getattr(
            self._classifier,
            "force_cancel_active" if force else "cancel_active",
            None,
        )
        if callable(method):
            try:
                method()
            except Exception as exc:
                self._log_failure(type(exc).__name__)

    def _consume_classifier_call(self, task: asyncio.Task) -> None:
        self._classifier_calls.discard(task)
        self._classifier_call_started.pop(task, None)

    def _log_failure(self, error_class: str) -> None:
        self._logger.warning("shadow routing task failed: %s", error_class[:128])


__all__ = [
    "ClassifierCallResult",
    "LightweightRouteClassifier",
    "RouteCallFuse",
    "ShadowRoutingObserver",
]
