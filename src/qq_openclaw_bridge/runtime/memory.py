from dataclasses import dataclass
from pathlib import Path

from ..agent import OpenClawCliAgentAdapter
from ..application.memory import E3MemoryCommandHandler, MemoryContextService
from ..application.memory_learning import (
    LocalMemoryAnalyzer,
    MemoryLearningService,
    OpenClawMemoryAnalyzer,
)
from ..domain.memory import extract_memory_items
from ..persona import PersonaRuleProvider


@dataclass(frozen=True)
class MemoryRuntime:
    command_handler: E3MemoryCommandHandler
    context_service: MemoryContextService
    learning_service: MemoryLearningService


def create_memory_runtime(config, state_store, housekeeper):
    if (
        not config.enable_memory2
        or state_store is None
        or not hasattr(state_store, "memory_repository")
    ):
        return None

    repository = state_store.memory_repository
    _import_legacy_memories(config, state_store, repository)
    workspace = Path(config.housekeeper_memory_path).expanduser().parent
    persona_provider = PersonaRuleProvider(workspace)

    analyzer = LocalMemoryAnalyzer()
    if config.agent_adapter == "openclaw-cli" and config.memory_light_model:
        adapter = OpenClawCliAgentAdapter(
            config.openclaw_command,
            timeout_seconds=config.memory_light_timeout_seconds,
            model=config.memory_light_model,
            agent_id=config.openclaw_agent_id,
            session_prefix=config.memory_light_session_prefix,
            use_gateway=config.openclaw_use_gateway,
        )
        analyzer = OpenClawMemoryAnalyzer(
            adapter,
            model_name=config.memory_light_model,
            min_confidence=config.memory_candidate_min_confidence,
            session_prefix=config.memory_light_session_prefix,
        )

    def export_confirmed(user_id: int, text: str) -> None:
        if housekeeper is not None:
            housekeeper.handle("记住：%s" % text, user_id=user_id)

    return MemoryRuntime(
        command_handler=E3MemoryCommandHandler(
            repository, confirmed_exporter=export_confirmed
        ),
        context_service=MemoryContextService(
            repository,
            persona_rules_provider=persona_provider,
            max_chars=config.memory_context_max_chars,
            fact_limit=config.memory_fact_limit,
            summary_limit=config.memory_summary_limit,
        ),
        learning_service=MemoryLearningService(
            repository,
            analyzer,
            summary_trigger_turns=config.memory_summary_trigger_turns,
            summary_batch_turns=config.memory_summary_batch_turns,
            metric_setter=state_store.set_runtime_metric,
        ),
    )


def _import_legacy_memories(config, state_store, repository) -> None:
    path = Path(config.housekeeper_memory_path).expanduser()
    content = state_store.read_document("memory", path, "# chichu MEMORY\n")
    for item in extract_memory_items(content):
        repository.add_confirmed_fact(
            user_id=0,
            layer="long_term",
            content=item.text,
            source_kind="legacy-import",
            category="legacy",
        )
