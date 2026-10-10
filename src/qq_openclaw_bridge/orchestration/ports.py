from typing import Protocol

from .models import ExecutionPlan, ExecutionResult, TaskContract, VerificationReport


class PlanningStrategy(Protocol):
    def plan(self, contract: TaskContract) -> ExecutionPlan:
        ...


class ExecutionEngine(Protocol):
    async def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        ...


class VerificationStrategy(Protocol):
    def verify(
        self, contract: TaskContract, result: ExecutionResult
    ) -> VerificationReport:
        ...


class BudgetPolicy(Protocol):
    def allows(self, contract: TaskContract) -> bool:
        ...


class CapabilityRegistry(Protocol):
    def select(self, contract: TaskContract) -> str:
        ...


class BrainstormGate(Protocol):
    def mode_for(self, contract: TaskContract) -> str:
        ...


class OrchestrationTraceSink(Protocol):
    def start(
        self, contract: TaskContract, mode: str, config_version: int
    ) -> str:
        ...

    def transition(
        self, run_id: str, stage: str, detail: str = ""
    ) -> None:
        ...

    def finish(
        self, run_id: str, status: str, detail: str = ""
    ) -> None:
        ...


class Runtime(Protocol):
    async def execute(self, contract: TaskContract) -> ExecutionResult:
        ...
