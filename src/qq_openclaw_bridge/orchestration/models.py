from dataclasses import dataclass
from typing import Tuple

from ..models import AgentRequest


@dataclass(frozen=True)
class TaskContract:
    request: AgentRequest
    goal: str
    constraints: Tuple[str, ...] = ()
    acceptance_criteria: Tuple[str, ...] = ()
    risk_level: str = "normal"

    @classmethod
    def from_request(cls, request: AgentRequest) -> "TaskContract":
        return cls(request=request, goal=request.text)


@dataclass(frozen=True)
class WorkItem:
    item_id: str
    request: AgentRequest


@dataclass(frozen=True)
class ExecutionPlan:
    contract: TaskContract
    items: Tuple[WorkItem, ...]


@dataclass(frozen=True)
class ExecutionResult:
    text: str


@dataclass(frozen=True)
class VerificationReport:
    accepted: bool
    reason: str = ""
