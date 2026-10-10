from typing import Awaitable, Callable

from ..models import AgentRequest, AgentResponse
from .models import (
    ExecutionPlan,
    ExecutionResult,
    TaskContract,
    VerificationReport,
    WorkItem,
)


class DirectPlanningStrategy:
    def plan(self, contract: TaskContract) -> ExecutionPlan:
        return ExecutionPlan(
            contract=contract,
            items=(WorkItem(item_id="direct", request=contract.request),),
        )


class SingleAgentExecutionEngine:
    def __init__(
        self,
        reply_call: Callable[[AgentRequest], Awaitable[AgentResponse]],
    ) -> None:
        self._reply_call = reply_call

    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        if len(plan.items) != 1:
            raise ValueError("direct execution requires exactly one work item")
        response = await self._reply_call(plan.items[0].request)
        return ExecutionResult(text=response.text)


class CompatibilityVerificationStrategy:
    def verify(
        self, contract: TaskContract, result: ExecutionResult
    ) -> VerificationReport:
        if not result.text.strip():
            return VerificationReport(accepted=False, reason="empty agent reply")
        return VerificationReport(accepted=True)


class ExistingLimitBudgetPolicy:
    def allows(self, contract: TaskContract) -> bool:
        return True


class StaticCapabilityRegistry:
    def __init__(self, capability_id: str) -> None:
        self.capability_id = capability_id

    def select(self, contract: TaskContract) -> str:
        return self.capability_id


class DisabledBrainstormGate:
    def mode_for(self, contract: TaskContract) -> str:
        return "disabled"
