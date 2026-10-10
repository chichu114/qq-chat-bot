import logging
from typing import Optional

from .config import OrchestrationConfig
from .models import ExecutionResult, TaskContract
from .ports import (
    BudgetPolicy,
    CapabilityRegistry,
    ExecutionEngine,
    OrchestrationTraceSink,
    PlanningStrategy,
    VerificationStrategy,
)


class BudgetRejected(RuntimeError):
    pass


class VerificationRejected(RuntimeError):
    pass


class OrchestrationRuntime:
    def __init__(
        self,
        planner: PlanningStrategy,
        executor: ExecutionEngine,
        verifier: VerificationStrategy,
        budget_policy: BudgetPolicy,
        capability_registry: CapabilityRegistry,
        trace_sink: OrchestrationTraceSink,
        config: OrchestrationConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._verifier = verifier
        self._budget_policy = budget_policy
        self._capability_registry = capability_registry
        self._trace_sink = trace_sink
        self._config = config
        self._logger = logger or logging.getLogger(__name__)

    async def execute(self, contract: TaskContract) -> ExecutionResult:
        run_id = self._safe_start(contract)
        try:
            allowed = self._budget_policy.allows(contract)
        except Exception as error:
            self._safe_finish(run_id, "failed", error.__class__.__name__)
            raise
        if not allowed:
            self._safe_finish(run_id, "rejected", "budget_rejected")
            raise BudgetRejected("orchestration budget rejected")

        try:
            self._capability_registry.select(contract)
            self._safe_transition(run_id, "planned")
            plan = self._planner.plan(contract)
            self._safe_transition(run_id, "executing")
            result = await self._executor.execute(plan)
            self._safe_transition(run_id, "verifying")
            report = self._verifier.verify(contract, result)
        except Exception as error:
            self._safe_finish(run_id, "failed", error.__class__.__name__)
            raise

        if not report.accepted:
            self._safe_finish(run_id, "rejected", report.reason)
            raise VerificationRejected(report.reason or "result rejected")

        self._safe_finish(run_id, "accepted", "")
        return result

    def _safe_start(self, contract: TaskContract) -> Optional[str]:
        try:
            return self._trace_sink.start(
                contract,
                self._config.mode,
                self._config.version,
            )
        except Exception as error:
            self._trace_degraded(error)
            return None

    def _safe_transition(
        self, run_id: Optional[str], stage: str, detail: str = ""
    ) -> None:
        if run_id is None:
            return
        try:
            self._trace_sink.transition(run_id, stage, detail)
        except Exception as error:
            self._trace_degraded(error)

    def _safe_finish(
        self, run_id: Optional[str], status: str, detail: str = ""
    ) -> None:
        if run_id is None:
            return
        try:
            self._trace_sink.finish(run_id, status, detail)
        except Exception as error:
            self._trace_degraded(error)

    def _trace_degraded(self, error: Exception) -> None:
        try:
            self._logger.warning(
                "orchestration_trace_degraded error_class=%s",
                error.__class__.__name__,
            )
        except Exception:
            pass
