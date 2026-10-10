"""Runtime composition for the opt-in shadow-routing observer."""

from ..agent import OpenAIResponsesAgentAdapter, OpenClawCliAgentAdapter
from ..application.shadow_routing import (
    LightweightRouteClassifier,
    ShadowRoutingObserver,
)
from ..persistence.routing import validate_routing_config


_DEFAULT_ROUTING_CONFIG = {
    "enabled": True,
    "classifier_enabled": False,
    "classifier_model": "openai/gpt-5.4-nano",
    "confidence_threshold_percent": 75,
    "classifier_timeout_seconds": 15,
    "max_daily_calls": 100,
    "failure_threshold": 3,
    "cooldown_seconds": 300,
    "rule_version": "f1-v1",
}


def bootstrap_shadow_routing_config(state_store):
    """Return the active config, creating the conservative default if absent."""
    repository = getattr(state_store, "routing_repository", None)
    if repository is None:
        raise RuntimeError("shadow routing requires a SQLite routing repository")
    active = repository.active_config()
    if active is None:
        active = repository.create_config(
            dict(_DEFAULT_ROUTING_CONFIG), activate=True
        )
    validate_routing_config(
        active.get("config"), active.get("config_sha256")
    )
    return active


def create_shadow_routing_runtime(
    config, state_store, adapter_factory=OpenClawCliAgentAdapter,
    classifier_concurrency_limit: int = 2,
    responses_adapter_factory=OpenAIResponsesAgentAdapter,
):
    """Return an observer only for opt-in runtimes with SQLite routing storage."""
    if not getattr(config, "enable_shadow_routing", False):
        return None
    repository = getattr(state_store, "routing_repository", None)
    if repository is None:
        return None
    bootstrap_shadow_routing_config(state_store)

    transport = getattr(config, "routing_classifier_transport", "openclaw")
    if transport == "responses_api":
        def create_adapter(routing_config):
            return responses_adapter_factory(
                timeout_seconds=routing_config["classifier_timeout_seconds"],
                model=routing_config["classifier_model"],
                routing_agent_id=getattr(config, "routing_agent_id", "routing"),
                cancellable=True,
            )
    elif transport == "openclaw":
        def create_adapter(routing_config):
            return adapter_factory(
                command=getattr(config, "openclaw_command", "scripts/openclaw.sh"),
                timeout_seconds=routing_config["classifier_timeout_seconds"],
                model=routing_config["classifier_model"],
                agent_id=getattr(config, "routing_agent_id", "routing"),
                session_prefix="agent:routing:qq",
                use_gateway=getattr(config, "openclaw_use_gateway", True),
                cancellable=True,
            )
    else:
        raise ValueError("unsupported routing classifier transport")
    return ShadowRoutingObserver(
        repository,
        classifier=LightweightRouteClassifier(adapter_factory=create_adapter),
        classifier_concurrency_limit=classifier_concurrency_limit,
    )


__all__ = ["bootstrap_shadow_routing_config", "create_shadow_routing_runtime"]
