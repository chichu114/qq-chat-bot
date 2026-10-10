import logging
from typing import Callable

from ..agent import create_agent_adapter
from ..application.coordinator_factory import (
    AgentCallProxy,
    create_command_chain,
    create_conversation_observer,
    create_direct_runtime,
    create_fallback_mapper,
    create_message_coordinator,
    create_security_interceptor,
    create_truncator,
)
from ..backups import create_backup_manager
from ..bridge import Bridge
from ..housekeeper import create_housekeeper
from ..persistence.orchestration import SqliteOrchestrationTraceSink
from ..reminders import create_reminder_store
from ..security import ConfirmationGuard
from ..storage import create_workspace_state_store
from ..style_memory import create_style_memory
from .memory import create_memory_runtime
from .media import create_media_runtime
from .community_style import create_group_runtime
from .routing import create_shadow_routing_runtime
from .secrets import ApprovalBindingKeyProvider


def resolve_orchestration_config_version(repository, mode: str) -> int:
    if repository is None:
        return 1
    active_config = repository.active_config()
    if active_config is None:
        active_config = repository.create_config_version(
            {"mode": mode}, activate=True
        )
    stored_config = active_config.get("config")
    active_mode = (
        stored_config.get("mode") if isinstance(stored_config, dict) else None
    )
    if active_mode != mode:
        raise RuntimeError("active orchestration mode does not match runtime mode")
    version = active_config.get("version")
    if type(version) is not int or version <= 0:
        raise RuntimeError("active orchestration config version is invalid")
    return version


def build_application(
    config,
    state_store_factory: Callable = create_workspace_state_store,
    agent_factory: Callable = create_agent_adapter,
    style_factory: Callable = create_style_memory,
    housekeeper_factory: Callable = create_housekeeper,
    reminder_factory: Callable = create_reminder_store,
    memory_factory: Callable = create_memory_runtime,
    confirmation_guard_factory: Callable = ConfirmationGuard,
    approval_binding_key_provider_factory: Callable = ApprovalBindingKeyProvider,
    backup_factory: Callable = create_backup_manager,
    media_factory: Callable = create_media_runtime,
    group_runtime_factory: Callable = create_group_runtime,
    bridge_factory: Callable = Bridge,
):
    """Builds one bridge graph around one canonical state store."""

    state_store = state_store_factory(config)
    logger = logging.getLogger("qq_openclaw_bridge")
    e4_enabled = config.enable_media_pipeline or config.enable_long_tasks
    if e4_enabled and (
        state_store is None or getattr(state_store, "e4_repository", None) is None
    ):
        raise RuntimeError("E4 features require a SQLite state store")
    if config.enable_shadow_routing and (
        state_store is None or getattr(state_store, "routing_repository", None) is None
    ):
        raise RuntimeError("shadow routing requires a SQLite state store")
    confirmation_guard = None
    action_intent_repository = getattr(
        state_store, "action_intent_repository", None
    )
    orchestration_repository = getattr(
        state_store, "orchestration_repository", None
    )
    config_version = resolve_orchestration_config_version(
        orchestration_repository, config.orchestration_mode
    )
    if action_intent_repository is not None:
        action_intent_repository.recover_executing_as_unknown()
    if config.enable_security_confirmation:
        binding_key = (
            approval_binding_key_provider_factory().get()
            if action_intent_repository is not None
            else None
        )
        confirmation_guard = confirmation_guard_factory(
            timeout_seconds=config.confirmation_timeout_seconds,
            approval_store=state_store,
            binding_key=binding_key,
            config_version=config_version,
        )
    backup_manager = (
        backup_factory(config) if config.enable_auto_backup else None
    )
    housekeeper = housekeeper_factory(config, state_store=state_store)
    memory_runtime = memory_factory(config, state_store, housekeeper)
    media_runtime = None
    if e4_enabled:
        media_runtime = media_factory(config, state_store)
    group_runtime = group_runtime_factory(config, state_store)
    shadow_routing_observer = None
    if config.enable_shadow_routing:
        shadow_routing_observer = create_shadow_routing_runtime(config, state_store)
        if shadow_routing_observer is not None:
            shadow_routing_observer.recover()
    agent_adapter = agent_factory(config)
    style_memory = style_factory(config, state_store=state_store)
    agent_call = AgentCallProxy()
    command_chain = create_command_chain(
        style_memory=style_memory,
        housekeeper=housekeeper,
        memory_runtime=memory_runtime,
        media_runtime=media_runtime,
        logger=logger,
    )
    context_service = getattr(memory_runtime, "context_service", None)
    orchestration_runtime = None
    if config.orchestration_mode == "direct":
        trace_sink = (
            SqliteOrchestrationTraceSink(orchestration_repository)
            if orchestration_repository is not None
            else None
        )
        orchestration_runtime = create_direct_runtime(
            agent_call,
            trace_sink=trace_sink,
            config_version=config_version,
        )
    message_coordinator = create_message_coordinator(
        agent_call=agent_call,
        command_chain=command_chain,
        fallback_mapper=create_fallback_mapper(config, logger),
        truncator=create_truncator(config),
        security_interceptor=create_security_interceptor(
            confirmation_guard, logger
        ),
        context_enricher=(
            context_service.enrich if context_service is not None else None
        ),
        conversation_observer=create_conversation_observer(
            memory_runtime, logger
        ),
        orchestration_runtime=orchestration_runtime,
        action_intent_repository=action_intent_repository,
        shadow_routing_observer=shadow_routing_observer,
        logger=logger,
    )
    application = bridge_factory(
        config,
        agent_adapter,
        style_memory=style_memory,
        housekeeper=housekeeper,
        reminder_store=reminder_factory(config, state_store=state_store),
        memory_runtime=memory_runtime,
        media_runtime=media_runtime,
        group_runtime=group_runtime,
        confirmation_guard=confirmation_guard,
        backup_manager=backup_manager,
        state_store=state_store,
        message_coordinator=message_coordinator,
    )
    bridge_agent_call = getattr(application, "_call_agent", None)
    if not callable(bridge_agent_call):
        raise RuntimeError("bridge_factory must provide callable _call_agent")
    agent_call.bind(bridge_agent_call)
    return application
