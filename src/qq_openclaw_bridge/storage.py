from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .persistence.approvals import ApprovalRepository
from .persistence.actions import ActionIntentRepository
from .persistence.database import SCHEMA_VERSION, SqliteDatabase
from .persistence.documents import DocumentRepository
from .persistence.e4 import E4Repository
from .persistence.messaging import (
    InboundRepository,
    OutboxRepository,
    RuntimeMetricRepository,
)
from .persistence.memory import MemoryRepository
from .persistence.orchestration import OrchestrationRepository
from .persistence.routing import RoutingRepository
from .persistence.community_style import CommunityStyleRepository


class WorkspaceStateStore:
    """Compatibility facade for the modular SQLite repositories."""

    def __init__(self, database_path: Path) -> None:
        self._database = SqliteDatabase(database_path)
        self.database_path = self._database.path
        self.lock_path = self._database.lock_path
        self._documents = DocumentRepository(self._database)
        self._approvals = ApprovalRepository(self._database)
        self._inbound = InboundRepository(self._database)
        self._outbox = OutboxRepository(self._database)
        self._metrics = RuntimeMetricRepository(self._database)
        self._memory = MemoryRepository(self._database)
        self._e4 = E4Repository(self._database)
        self._orchestration = OrchestrationRepository(self._database)
        self._action_intents = ActionIntentRepository(self._database)
        self._routing = RoutingRepository(self._database)
        self._community_style = CommunityStyleRepository(self._database)

    def ensure_documents(
        self, documents: Mapping[str, Tuple[Path, str]]
    ) -> None:
        self._documents.ensure(documents)

    def read_document(self, name: str, export_path: Path, default: str) -> str:
        return self._documents.read(name, export_path, default)

    def write_document(self, name: str, export_path: Path, content: str) -> None:
        self._documents.write(name, export_path, content)

    def save_approval(
        self,
        approval_id: str,
        user_id: int,
        command: str,
        description: str,
        expires_at: str,
        created_at: str,
    ) -> None:
        self._approvals.save(
            approval_id,
            user_id,
            command,
            description,
            expires_at,
            created_at,
        )

    def get_pending_approval(self, user_id: int) -> Optional[Dict[str, object]]:
        return self._approvals.get_pending(user_id)

    def delete_approval(self, approval_id: str) -> None:
        self._approvals.delete(approval_id)

    def list_recent_approvals(
        self, limit: int = 100
    ) -> List[Dict[str, object]]:
        return self._approvals.list_recent(limit)

    def claim_inbound(
        self,
        message_id: str,
        user_id: int,
        stale_after_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> bool:
        return self._inbound.claim(
            message_id, user_id, stale_after_seconds, now
        )

    def complete_inbound(
        self, message_id: str, now: Optional[datetime] = None
    ) -> None:
        self._inbound.complete(message_id, now)

    def is_duplicate_inbound(
        self,
        message_id: str,
        stale_after_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> bool:
        return self._inbound.is_duplicate(message_id, stale_after_seconds, now)

    def fail_inbound(
        self, message_id: str, detail: str, now: Optional[datetime] = None
    ) -> None:
        self._inbound.fail(message_id, detail, now)

    def enqueue_outbox(
        self,
        kind: str,
        user_id: int,
        message: str,
        dedupe_key: str,
        echo: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        return self._outbox.enqueue(
            kind, user_id, message, dedupe_key, echo, now
        )

    def enqueue_final_with_observation(self, **kwargs) -> Dict[str, object]:
        return self._outbox.enqueue_final_with_observation(**kwargs)

    def enqueue_task_notice(self, **kwargs) -> Dict[str, object]:
        return self._outbox.enqueue_task_notice(**kwargs)

    def commit_task_final(self, **kwargs) -> Optional[Dict[str, object]]:
        return self._e4.commit_task_final(**kwargs)

    def get_outbox(self, dedupe_key: str) -> Optional[Dict[str, object]]:
        return self._outbox.get(dedupe_key)

    def validate_reserved_outbox(self, outbox_id: int) -> bool:
        return self._outbox.validate_reserved_claim(outbox_id)

    def quarantine_reserved_outbox(
        self, outbox_id: int, now: Optional[datetime] = None
    ) -> None:
        self._outbox.quarantine_reserved(outbox_id, now)

    def claim_due_outbox(
        self,
        now: Optional[datetime] = None,
        max_attempts: int = 8,
        lease_seconds: int = 300,
        task_notice_interval_seconds: int = 60,
    ) -> Optional[Dict[str, object]]:
        return self._outbox.claim_due(
            now, max_attempts, lease_seconds, task_notice_interval_seconds
        )

    def mark_outbox_sent(
        self,
        outbox_id: int,
        claim_token: object,
        now: Optional[datetime] = None,
    ) -> bool:
        return self._outbox.mark_sent(outbox_id, claim_token, now)

    def mark_final_outbox_sent(
        self,
        outbox_id: int,
        claim_token: object,
        now: Optional[datetime] = None,
    ) -> bool:
        return self._outbox.mark_final_sent(outbox_id, claim_token, now)

    def mark_outbox_retry(
        self,
        outbox_id: int,
        claim_token: object,
        detail: str,
        retry_base_seconds: int,
        max_attempts: int,
        now: Optional[datetime] = None,
    ) -> bool:
        return self._outbox.mark_retry(
            outbox_id,
            claim_token,
            detail,
            retry_base_seconds,
            max_attempts,
            now,
        )

    def set_runtime_metric(self, name: str, value: str) -> None:
        self._metrics.set(name, value)

    def transport_stats(self) -> Dict[str, object]:
        return self._metrics.transport_stats()

    @property
    def memory_repository(self) -> MemoryRepository:
        return self._memory

    @property
    def e4_repository(self) -> E4Repository:
        return self._e4

    @property
    def orchestration_repository(self) -> OrchestrationRepository:
        return self._orchestration

    @property
    def action_intent_repository(self) -> ActionIntentRepository:
        return self._action_intents

    @property
    def community_style_repository(self) -> CommunityStyleRepository:
        return self._community_style

    @property
    def routing_repository(self) -> RoutingRepository:
        return self._routing

    def add_confirmed_memory(
        self,
        user_id: int,
        content: str,
        source_kind: str = "explicit",
        source_message_id: Optional[str] = None,
    ) -> Dict[str, object]:
        return self._memory.add_confirmed_fact(
            user_id=user_id,
            layer="long_term",
            content=content,
            source_kind=source_kind,
            source_message_id=source_message_id,
        )

    def delete_confirmed_memory(self, user_id: int, content: str) -> bool:
        return self._memory.delete_confirmed_content(user_id, content)

    def clear_confirmed_memories(self, user_id: int) -> int:
        return self._memory.clear_confirmed(user_id)

    def locked(self):
        return self._database.locked()

    def _connect(self):
        return self._database.connect()

    def _harden_database_files(self) -> None:
        self._database.harden_files()


def create_workspace_state_store(config) -> Optional[WorkspaceStateStore]:
    if not config.enable_sqlite_store:
        return None
    store = WorkspaceStateStore(Path(config.state_database_path))
    store.ensure_documents(
        {
            "memory": (Path(config.housekeeper_memory_path), "# chichu MEMORY\n"),
            "todo": (
                Path(config.housekeeper_todo_path),
                "# chichu TODO\n\n## 待办\n",
            ),
            "style": (Path(config.style_memory_path), "# chichu STYLE\n"),
            "reminder_state": (
                Path(config.reminder_state_path),
                "status=active\n",
            ),
        }
    )
    return store


__all__ = ["SCHEMA_VERSION", "WorkspaceStateStore", "create_workspace_state_store"]
