import asyncio
import json
import logging
import subprocess
import time
from dataclasses import replace
from datetime import datetime
from typing import Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from .agent import AgentAdapter
from .application.background import BackupService, ReminderPushService
from .application.coordinator import (
    CoordinatorResolution,
    MessageCoordinator,
    SecurityInterception,
)
from .application.coordinator_factory import (
    create_command_chain,
    create_message_coordinator,
)
from .application.delivery import ReliableDeliveryService
from .application.dispatch import EventDispatcher
from .application.group_chat import GroupReply
from .application.long_tasks import DeferredTaskResult, NotificationDeliveryOutcome
from .application.media_context import compile_media_context
from .config import Config
from .filters import evaluate_event, evaluate_group_event
from .onebot import build_send_private_msg, parse_group_message, parse_qq_user_id
from .models import AgentRequest
from .rate_limit import RateLimiter
from .runtime.supervisor import WebSocketSupervisor
from .runtime.onebot_rpc import combine_disconnect_hooks, combine_payload_consumers
from .security import ConfirmationGuard


TEXT_ONLY_NOTICE = "第一版只支持文本消息。"
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
_ORCHESTRATION_RECONCILE_INTERVAL_SECONDS = 5.0
_SHADOW_ROUTING_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_GROUP_CONTROL_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


def _capture_group_boundary(function, arguments):
    try:
        return "ok", function(*arguments)
    except BaseException as error:
        if type(error) in _GROUP_CONTROL_EXCEPTIONS:
            return "control", type(error)
        return ("error", None) if isinstance(error, Exception) else ("base_error", None)


class Bridge:
    def __init__(
        self,
        config: Config,
        agent_adapter: AgentAdapter,
        style_memory: Optional[object] = None,
        housekeeper: Optional[object] = None,
        reminder_store: Optional[object] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        confirmation_guard: Optional[object] = None,
        backup_manager: Optional[object] = None,
        state_store: Optional[object] = None,
        memory_runtime: Optional[object] = None,
        media_runtime: Optional[object] = None,
        group_runtime: Optional[object] = None,
        message_coordinator: Optional[MessageCoordinator] = None,
    ) -> None:
        self.config = config
        self.agent_adapter = agent_adapter
        self.style_memory = style_memory
        self.housekeeper = housekeeper
        self.reminder_store = reminder_store
        self.backup_manager = backup_manager
        self.state_store = state_store
        self.memory_runtime = memory_runtime
        self.media_runtime = media_runtime
        self.group_runtime = (
            group_runtime if config.enable_group_agent else None
        )
        self._now_provider = now_provider or (
            lambda: datetime.now(tz=ZoneInfo("Asia/Shanghai"))
        )
        self.confirmation_guard = confirmation_guard
        if self.confirmation_guard is None and config.enable_security_confirmation:
            self.confirmation_guard = ConfirmationGuard(
                timeout_seconds=config.confirmation_timeout_seconds,
                now_provider=self._now_provider,
            )
        self.rate_limiter = RateLimiter(config.rate_limit_messages_per_minute)
        self.logger = logging.getLogger("qq_openclaw_bridge")
        self._agent_semaphore = asyncio.Semaphore(config.max_concurrent_agent_calls)
        self._send_lock = asyncio.Lock()
        self._agent_failures = 0
        self._circuit_open_until = 0.0
        self._next_orchestration_reconcile_at = 0.0
        self._event_dispatcher = EventDispatcher(
            config.max_pending_events, self.logger
        )
        self._group_event_dispatcher = EventDispatcher(
            config.max_pending_events, self.logger
        )
        self._delivery_service = ReliableDeliveryService(
            state_store,
            self._send_private_reply,
            config.outbox_retry_base_seconds,
            config.outbox_max_attempts,
            self.logger,
            observation_handler=self._handle_durable_observation,
            now_provider=self._now_provider,
            task_notice_interval_seconds=(
                config.long_task_progress_interval_seconds
            ),
            final_delivery_handler=self._handle_orchestration_delivery,
        )
        self._reminder_service = ReminderPushService(
            reminder_store,
            state_store,
            self._send_private_reply,
            self.deliver_outbox_once,
            self._now_provider,
            self.logger,
        )
        self._backup_service = BackupService(
            backup_manager,
            config.enable_auto_backup,
            config.auto_backup_interval_hours,
            self.logger,
        )
        self._command_chain = None
        if message_coordinator is None:
            self._command_chain = create_command_chain(
                style_memory=style_memory,
                housekeeper=housekeeper,
                memory_runtime=memory_runtime,
                media_runtime=media_runtime,
                logger=self.logger,
            )
            message_coordinator = create_message_coordinator(
                agent_call=self._call_agent,
                command_chain=self._command_chain,
                fallback_mapper=self._map_agent_error,
                truncator=self._truncate,
                security_interceptor=(
                    self._intercept_security
                    if self.confirmation_guard is not None
                    else None
                ),
                context_enricher=(
                    memory_runtime.context_service.enrich
                    if memory_runtime is not None
                    else None
                ),
                conversation_observer=(
                    self._observe_conversation
                    if memory_runtime is not None
                    else None
                ),
                action_intent_repository=getattr(
                    state_store, "action_intent_repository", None
                ),
                logger=self.logger,
            )
        self._message_coordinator = message_coordinator
        self.message_coordinator = message_coordinator
        connection_workers = []
        if state_store is not None:
            connection_workers.append(self._outbox_loop)
        if reminder_store is not None and config.enable_reminder_push:
            connection_workers.append(self._reminder_loop)
        payload_consumers = (
            media_runtime.rpc_client.consume if media_runtime is not None else None,
            self.group_runtime.rpc_client.consume
            if self.group_runtime is not None else None,
        )
        disconnect_hooks = (
            media_runtime.rpc_client.fail_pending if media_runtime is not None else None,
            self.group_runtime.rpc_client.fail_pending
            if self.group_runtime is not None else None,
        )
        self._websocket_supervisor = WebSocketSupervisor(
            config.onebot_ws_url,
            self.dispatch_event,
            self.logger,
            worker_factories=tuple(connection_workers),
            payload_consumer=(
                combine_payload_consumers(*payload_consumers)
                if any(consumer is not None for consumer in payload_consumers)
                else None
            ),
            disconnect_hook=(
                combine_disconnect_hooks(*disconnect_hooks)
                if any(hook is not None for hook in disconnect_hooks)
                else None
            ),
        )

    async def handle_event(self, event: Mapping[str, object], websocket) -> bool:
        if self.group_runtime is not None:
            group_id = self._group_event_id(event)
            if group_id is not None:
                return await self._group_event_dispatcher.run(
                    group_id,
                    lambda: self._handle_group_event_safe(event, websocket),
                )
        user_id = self._event_user_id(event)
        return await self._event_dispatcher.run(
            user_id,
            lambda: self._handle_event_core(event, websocket),
        )

    async def _handle_event_core(
        self, event: Mapping[str, object], websocket
    ) -> bool:
        decision = evaluate_event(
            event,
            self.config.allowed_qq_users,
            self.rate_limiter,
            enable_media_pipeline=self.config.enable_media_pipeline,
            enable_quote_resolution=(
                self.media_runtime is not None
                and (
                    self.config.enable_media_pipeline
                    or self.config.enable_long_tasks
                )
            ),
            duplicate_checker=(
                self.state_store.is_duplicate_inbound
                if self.state_store is not None
                else None
            ),
        )

        if not decision.accepted:
            if decision.reason == "unsupported_message":
                user_id = int(event["user_id"])
                await self._send_private_reply(websocket, user_id, TEXT_ONLY_NOTICE)
                return True
            self.logger.debug("ignored OneBot event: %s", decision.reason)
            return False

        envelope = decision.envelope
        assert envelope is not None
        request_data = decision.request
        if request_data is None:
            request_data = AgentRequest(
                envelope.user_id,
                envelope.message_id,
                "",
                envelope.timestamp,
            )

        if (
            self.state_store is not None
            and request_data.message_id
            and not self.state_store.claim_inbound(
                request_data.message_id, request_data.user_id
            )
        ):
            self.logger.info(
                "ignored duplicate OneBot message: message_id=%s",
                request_data.message_id,
            )
            return False

        return await self._handle_claimed_request(
            request_data, envelope, websocket
        )

    @staticmethod
    def _group_event_id(event: Mapping[str, object]) -> Optional[int]:
        control_type = None
        try:
            if event.get("post_type") != "message" or event.get("message_type") != "group":
                return None
            return parse_qq_user_id(event.get("group_id"))
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit, GeneratorExit):
                control_type = type(error)
        if control_type is not None:
            raise control_type().with_traceback(None) from None
        return None

    async def _handle_group_event_safe(self, event, websocket) -> bool:
        control_type = None
        try:
            return await self._handle_group_event(event, websocket)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in _GROUP_CONTROL_EXCEPTIONS:
                control_type = type(error)
            else:
                self.logger.error("group event failed")
                return False
        if control_type is not None:
            raise control_type().with_traceback(None) from None
        return False

    @staticmethod
    async def _group_boundary(function, *arguments):
        kind, value = await asyncio.to_thread(
            _capture_group_boundary, function, arguments
        )
        if kind == "control":
            raise value().with_traceback(None) from None
        return kind, value

    async def _handle_group_event(self, event, websocket) -> bool:
        runtime = self.group_runtime
        if runtime is None:
            return False
        group_id = self._group_event_id(event)
        if group_id is None:
            return False
        boundary, allowed_groups = await self._group_boundary(
            runtime.allowed_groups_provider
        )
        if (
            boundary != "ok"
            or type(allowed_groups) is not frozenset
            or any(type(value) is not int for value in allowed_groups)
            or group_id not in allowed_groups
        ):
            return False
        envelope = parse_group_message(event, self.config.bot_qq)
        if envelope is None:
            return False
        if envelope.user_id == self.config.bot_qq:
            return False

        reply_sender_id = None
        if envelope.quote is not None and not envelope.mentioned_bot:
            message_id = envelope.quote.message_id
            boundary, known = await self._group_boundary(
                runtime.repository.is_bot_message, group_id, message_id
            )
            if boundary != "ok" or type(known) is not bool:
                return False
            if known:
                reply_sender_id = self.config.bot_qq
            else:
                response = await runtime.rpc_client.request(
                    websocket,
                    "get_msg",
                    {"message_id": message_id},
                    timeout=5,
                )
                reply_sender_id = self._reply_sender_id(response)
                if reply_sender_id is None:
                    return False

        explicit_trigger = (
            envelope.mentioned_bot
            or reply_sender_id == self.config.bot_qq
        )
        allow_untriggered = False
        if not explicit_trigger:
            decider = getattr(runtime, "interjection_decider", None)
            limiter = getattr(runtime, "interjection_limiter", None)
            if (
                not callable(getattr(decider, "should_reply", None))
                or not callable(getattr(limiter, "allow", None))
            ):
                return False
            async with runtime.generation_semaphore:
                boundary, should_reply = await self._group_boundary(
                    decider.should_reply, envelope.text
                )
            if boundary != "ok" or type(should_reply) is not bool or not should_reply:
                return False
            boundary, allowed = await self._group_boundary(
                limiter.allow, "group-interjection:%s" % group_id
            )
            if boundary != "ok" or type(allowed) is not bool or not allowed:
                return False
            allow_untriggered = True

        decision = evaluate_group_event(
            event,
            bot_qq=self.config.bot_qq,
            allowed_groups=set(allowed_groups),
            group_limiter=runtime.group_limiter,
            user_limiter=runtime.user_limiter,
            reply_sender_id=reply_sender_id,
            allow_untriggered=allow_untriggered,
        )
        if not decision.accepted or decision.request is None:
            return False
        request = decision.request
        claim_token = await runtime.delivery.claim(request)
        if claim_token is None:
            return False

        try:
            async with runtime.generation_semaphore:
                boundary, reply = await self._group_boundary(
                    runtime.coordinator.reply, request
                )
            if boundary == "base_error":
                return False
            if boundary != "ok" or type(reply) is not GroupReply:
                raise ValueError("invalid group reply")
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in _GROUP_CONTROL_EXCEPTIONS:
                raise type(error)().with_traceback(None) from None
            reply = GroupReply(
                "群聊智能体暂时不可用，请稍后再试。", "neutral", None,
                "ollama_unavailable",
            )
        return await runtime.delivery.send(
            websocket,
            request,
            reply,
            claim_token=claim_token,
        )

    @staticmethod
    def _reply_sender_id(response: object) -> Optional[int]:
        control_type = None
        try:
            if not isinstance(response, Mapping):
                return None
            status = response.get("status")
            if type(status) is not str or status != "ok":
                return None
            retcode = response.get("retcode")
            if type(retcode) is not int or retcode != 0:
                return None
            data = response.get("data")
            if not isinstance(data, Mapping):
                return None
            sender = data.get("sender")
            if not isinstance(sender, Mapping):
                return None
            return parse_qq_user_id(sender.get("user_id"))
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit, GeneratorExit):
                control_type = type(error)
        if control_type is not None:
            raise control_type().with_traceback(None) from None
        return None

    @staticmethod
    def _event_user_id(event: Mapping[str, object]) -> int:
        try:
            return int(event.get("user_id", 0))
        except (TypeError, ValueError):
            return 0

    async def _handle_claimed_request(
        self, request_data, envelope, websocket
    ) -> bool:
        async def reply_sink(active_request, reply_text: str) -> None:
            echo = (
                "reply:%s" % active_request.message_id
                if active_request.message_id
                else None
            )
            return await self._send_reply_reliably(
                websocket,
                active_request.user_id,
                reply_text,
                echo=echo,
                message_id=active_request.message_id,
            )

        if not (
            self.media_runtime is not None
            and (self.config.enable_media_pipeline or self.config.enable_long_tasks)
        ):
            return await self._message_coordinator.handle(
                request_data,
                reply_sink,
            )

        prepared = self._message_coordinator.prepare(request_data)
        if prepared.reply is not None:
            resolution = await self._message_coordinator.resolve_prepared(prepared)
            return await self._deliver_e4_resolution(resolution, websocket)

        return await self._run_e4_request(
            envelope, prepared, websocket
        )

    async def _run_e4_request(
        self, envelope, prepared, websocket
    ) -> bool:
        runtime = self.media_runtime
        assert runtime is not None
        task_service = (
            runtime.task_service if self.config.enable_long_tasks else None
        )
        notice_sequence = 0
        bound_notice_task_id = [None]

        async def notice_sink(notice: str) -> bool:
            nonlocal notice_sequence
            notice_sequence += 1
            task_id = bound_notice_task_id[0]
            if task_id is None:
                raise RuntimeError("task notice has no active task")
            delivered = await self._delivery_service.send_task_notice(
                websocket,
                task_id,
                envelope.user_id,
                notice,
                role="ack" if notice_sequence == 1 else "progress",
            )
            return NotificationDeliveryOutcome(
                delivered=delivered, audit_deferred=True
            )

        def bind_notice_task(task_id):
            bound_notice_task_id[0] = str(task_id)
            return notice_sink

        notice_sink.bind_task = bind_notice_task

        async def operation(task_id, cancellation) -> bool:
            def report(stage, detail=""):
                if task_service is not None and task_id is not None:
                    task_service.report(task_id, stage, detail)

            report("validating")
            if envelope.quote is not None:
                report("resolving_quote")
            quote = await runtime.quote_resolver.resolve(envelope, websocket)

            artifacts = ()
            if self.config.enable_media_pipeline and envelope.attachments:
                if runtime.pipeline is None:
                    raise RuntimeError("media pipeline is unavailable")
                artifacts = await runtime.pipeline.process(
                    envelope, report, cancellation
                )

            active_request = prepared.request
            if envelope.attachments or envelope.quote is not None:
                media_context = compile_media_context(
                    envelope,
                    artifacts,
                    quote,
                    self.config.memory_context_max_chars,
                )
                active_request = replace(
                    active_request, media_context=media_context
                )
            if task_id is not None:
                active_request = replace(active_request, task_id=str(task_id))

            report("reasoning")
            active_prepared = replace(prepared, request=active_request)
            resolution = await self._message_coordinator.resolve_prepared(
                active_prepared, map_errors=False
            )
            report("delivering")
            return await self._deliver_e4_resolution(resolution, websocket)

        try:
            if task_service is not None:
                return await task_service.run(
                    envelope, operation, notice_sink
                )
            return await operation(None, None)
        except asyncio.CancelledError:
            return False
        except Exception as exc:
            self.logger.error("E4 request failed")
            fallback = self._truncate(self._fallback_reply_for_error(exc))
            resolution = CoordinatorResolution(prepared.request, fallback)
            return await self._deliver_e4_resolution(resolution, websocket)

    async def _deliver_e4_resolution(
        self, resolution: CoordinatorResolution, websocket
    ) -> bool:
        request = resolution.request
        delivered = await self._delivery_service.send_final(
            websocket,
            request,
            resolution.reply,
            task_id=request.task_id,
        )
        if request.task_id is not None:
            if request.message_id and self.state_store is not None:
                task = self.state_store.e4_repository.get_task(request.task_id)
                if task is not None and task["stage"] == "delivery_committed":
                    return DeferredTaskResult(delivered)
            return delivered
        return delivered

    def _intercept_security(self, request_data) -> SecurityInterception:
        assert self.confirmation_guard is not None
        security_decision = self.confirmation_guard.evaluate(
            request_data.user_id, request_data.text
        )
        if security_decision.reply is not None:
            self.logger.info(
                "security confirmation intercepted user=%s",
                request_data.user_id,
            )
            return SecurityInterception(
                request=request_data,
                reply=security_decision.reply,
            )
        if security_decision.command is not None:
            self.logger.info(
                "security confirmation approved user=%s",
                request_data.user_id,
            )
            request_data = replace(
                request_data, text=security_decision.command
            )
        return SecurityInterception(request=request_data)

    def _map_agent_error(self, exc: Exception) -> str:
        self.logger.exception("agent adapter failed")
        return self._fallback_reply_for_error(exc)

    async def _call_agent(self, request_data):
        now = time.monotonic()
        if now < self._circuit_open_until:
            self.logger.error("agent circuit breaker is open")
            raise RuntimeError("OpenClaw circuit breaker is cooling down")

        adapter = self.agent_adapter
        if request_data.user_id in self.config.kanagawave_private_users:
            adapter = getattr(
                self.group_runtime, "private_agent_adapter", None
            )
            if not callable(getattr(adapter, "reply", None)):
                raise RuntimeError("KanagawaWave private adapter is unavailable")

        async with self._agent_semaphore:
            last_error = None
            for attempt in range(1, self.config.agent_retry_attempts + 1):
                try:
                    response = await asyncio.to_thread(
                        adapter.reply, request_data
                    )
                    self._record_main_model_call(request_data, response=response)
                    self._agent_failures = 0
                    self._circuit_open_until = 0.0
                    self._set_runtime_metric(
                        "last_agent_success_at", self._utc_now_text()
                    )
                    self._set_runtime_metric("circuit_state", "closed")
                    return response
                except Exception as exc:
                    self._record_main_model_call(request_data, error=exc)
                    last_error = exc
                    if (
                        attempt >= self.config.agent_retry_attempts
                        or not self._is_transient_agent_error(exc)
                    ):
                        break
                    self.logger.warning(
                        "transient agent failure; retrying attempt=%d", attempt + 1
                    )

            self._agent_failures += 1
            if self._agent_failures >= self.config.agent_circuit_failure_threshold:
                self._circuit_open_until = (
                    time.monotonic()
                    + self.config.agent_circuit_cooldown_seconds
                )
                self._set_runtime_metric("circuit_state", "open")
            assert last_error is not None
            raise last_error

    def _record_main_model_call(
        self, request_data, response=None, error=None
    ) -> None:
        try:
            repository = getattr(self.state_store, "e4_repository", None)
            if repository is None:
                return
            repository.record_model_call(
                call_kind="chat",
                provider=(
                    getattr(response, "provider", None) or "unknown"
                ),
                model=getattr(response, "model", None) or "unknown",
                success=error is None,
                user_id=request_data.user_id,
                task_id=request_data.task_id,
                input_tokens=getattr(response, "input_tokens", None),
                output_tokens=getattr(response, "output_tokens", None),
                duration_ms=getattr(response, "duration_ms", None),
                error_class=(type(error).__name__ if error is not None else None),
            )
        except Exception:
            self.logger.warning("failed to persist main model call metrics")

    def _is_transient_agent_error(self, exc: Exception) -> bool:
        if isinstance(exc, subprocess.TimeoutExpired):
            return False
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        detail = str(exc).lower()
        return any(
            marker in detail
            for marker in (
                "timeout",
                "timed out",
                "connection reset",
                "connection refused",
                "socket closed",
                "temporarily unavailable",
                "fetch failed",
            )
        )

    async def dispatch_event(self, event: Mapping[str, object], websocket):
        if self.group_runtime is not None:
            group_id = self._group_event_id(event)
            if group_id is not None:
                return await self._group_event_dispatcher.dispatch(
                    group_id,
                    lambda: self._handle_group_event_safe(event, websocket),
                )
        return await self._event_dispatcher.dispatch(
            self._event_user_id(event),
            lambda: self._handle_event_core(event, websocket),
        )

    async def wait_for_event_tasks(self) -> None:
        await self._event_dispatcher.wait()
        await self._group_event_dispatcher.wait()
        if self.memory_runtime is not None:
            await self.memory_runtime.learning_service.wait()

    async def _shutdown_shadow_routing(self) -> None:
        if not getattr(
            self._message_coordinator, "has_shadow_routing_observer", False
        ):
            return
        try:
            drained = await asyncio.wait_for(
                self._message_coordinator.shutdown_shadow_routing(
                    _SHADOW_ROUTING_SHUTDOWN_TIMEOUT_SECONDS
                ),
                timeout=_SHADOW_ROUTING_SHUTDOWN_TIMEOUT_SECONDS,
            )
            if not drained:
                self.logger.warning("shadow routing tasks did not drain before shutdown")
        except asyncio.TimeoutError:
            self.logger.warning("shadow routing tasks did not drain before shutdown")

    async def _observe_conversation(self, request_data, reply_text: str) -> None:
        try:
            await self.memory_runtime.learning_service.observe(
                request_data, reply_text
            )
        except Exception:
            self.logger.exception("E3 memory observation failed")

    async def _handle_durable_observation(
        self, request_data, reply_text: str
    ) -> None:
        if self.memory_runtime is not None:
            await self.memory_runtime.learning_service.observe(
                request_data, reply_text
            )

    async def _handle_orchestration_delivery(
        self, user_id: int, message_id: str
    ) -> None:
        repository = getattr(
            self.state_store, "orchestration_repository", None
        )
        if repository is not None:
            await asyncio.to_thread(
                repository.complete_delivery,
                user_id,
                message_id,
                self._now_provider(),
            )

    async def _send_reply_reliably(
        self,
        websocket,
        user_id: int,
        message: str,
        echo: Optional[str],
        message_id: Optional[str],
    ) -> bool:
        return await self._delivery_service.send(
            websocket,
            user_id,
            message,
            echo,
            message_id,
        )

    async def deliver_outbox_once(self, websocket) -> bool:
        return await self._delivery_service.deliver_once(websocket)

    async def _outbox_loop(self, websocket) -> None:
        while True:
            await self._reconcile_orchestration_deliveries()
            try:
                delivered = await self.deliver_outbox_once(websocket)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("outbox loop failed")
                delivered = False
            await asyncio.sleep(0 if delivered else 1)

    async def _reconcile_orchestration_deliveries(self) -> None:
        now = time.monotonic()
        if now < self._next_orchestration_reconcile_at:
            return
        self._next_orchestration_reconcile_at = (
            now + _ORCHESTRATION_RECONCILE_INTERVAL_SECONDS
        )
        repository = getattr(
            self.state_store, "orchestration_repository", None
        )
        if repository is None:
            return
        try:
            await asyncio.to_thread(
                repository.reconcile_deliveries,
                self._now_provider(),
            )
        except Exception:
            self.logger.warning("orchestration delivery reconciliation failed")

    def _set_runtime_metric(self, name: str, value: str) -> None:
        if self.state_store is not None:
            self.state_store.set_runtime_metric(name, value)

    def _utc_now_text(self) -> str:
        return datetime.now(tz=ZoneInfo("UTC")).isoformat(timespec="seconds")

    def _handle_housekeeper_command(self, text: str, user_id: int) -> Optional[str]:
        if self.housekeeper is None:
            return None

        try:
            return self.housekeeper.handle(text, user_id=user_id)
        except Exception:
            self.logger.exception("housekeeper command failed")
            return None

    def _handle_style_command(self, text: str) -> Optional[str]:
        if self.style_memory is None:
            return None

        handler = getattr(self.style_memory, "handle_command", None)
        if handler is None:
            return None

        try:
            return handler(text)
        except Exception:
            self.logger.exception("style memory command failed")
            return None

    def _record_style_feedback(self, text: str) -> Optional[str]:
        if self.style_memory is None:
            return None

        try:
            return self.style_memory.record_if_feedback(text)
        except Exception:
            self.logger.exception("style memory update failed")
            return None

    async def _send_private_reply(
        self, websocket, user_id: int, message: str, echo: str = None
    ) -> None:
        action = build_send_private_msg(user_id, message, echo=echo)
        async with self._send_lock:
            await websocket.send(json.dumps(action, ensure_ascii=False))

    def _truncate(self, text: str) -> str:
        if len(text) <= self.config.max_reply_chars:
            return text
        if self.config.max_reply_chars <= len(TRUNCATED_SUFFIX):
            return TRUNCATED_SUFFIX[: self.config.max_reply_chars]
        keep = self.config.max_reply_chars - len(TRUNCATED_SUFFIX)
        return text[:keep] + TRUNCATED_SUFFIX

    def _fallback_reply_for_error(self, exc: Exception) -> str:
        if isinstance(exc, subprocess.TimeoutExpired):
            return TIMEOUT_FALLBACK_REPLY

        detail = str(exc).lower()
        if any(
            marker in detail
            for marker in (
                "unsupported_region",
                "unsupported_country",
                "country_region",
                "country, region",
                "proxy",
                "network route",
                "connect timeout",
                "fetch timeout",
            )
        ):
            return PROXY_FALLBACK_REPLY

        if any(
            marker in detail
            for marker in (
                "api key",
                "apikey",
                "unauthorized",
                "invalid_api_key",
                "no api key",
                "401",
            )
        ):
            return AUTH_FALLBACK_REPLY

        return self.config.fallback_reply

    async def _send_due_reminders(
        self,
        websocket,
        now: Optional[datetime] = None,
    ) -> None:
        await self._reminder_service.run_once(websocket, now)

    async def _reminder_loop(self, websocket) -> None:
        await self._reminder_service.loop(
            websocket, self.config.reminder_check_interval_seconds
        )

    def _run_auto_backup_once(self) -> None:
        self._backup_service.run_once()

    async def _backup_loop(self) -> None:
        await self._backup_service.loop()

    async def run_forever(self) -> None:
        if self._backup_service.enabled:
            asyncio.create_task(self._backup_loop())
        if not getattr(
            self._message_coordinator, "has_shadow_routing_observer", False
        ):
            await self._websocket_supervisor.run_forever()
            return
        try:
            await self._websocket_supervisor.run_forever()
        finally:
            await self._shutdown_shadow_routing()
