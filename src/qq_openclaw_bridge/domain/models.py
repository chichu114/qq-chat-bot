from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class TodoItem:
    status: str
    item_id: int
    created_at: str
    text: str
    metadata: Dict[str, str]

    @property
    def done(self) -> bool:
        return self.status == "x"


@dataclass(frozen=True)
class TodoDraft:
    text: str
    due_hint: Optional[str] = None
    category: Optional[str] = None


@dataclass(frozen=True)
class MemoryItem:
    created_at: str
    text: str
