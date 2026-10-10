from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Callable, Optional, Union


Number = Union[str, int, float, Decimal]


class BudgetExceeded(ValueError):
    pass


def _cost(value: Number) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("media cost must be a non-negative number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("media cost must be a non-negative number")
    return result


def _utc_day() -> date:
    return datetime.now(timezone.utc).date()


class BudgetReservation:
    def __init__(
        self,
        policy: "MediaBudgetPolicy",
        estimated_cost: Decimal,
        reservation_day: date,
    ) -> None:
        self._policy = policy
        self._estimated_cost = estimated_cost
        self._day = reservation_day
        self._finished = False
        self._within_budget: Optional[bool] = None

    def reconcile(self, actual_cost: Number) -> bool:
        return self._policy._reconcile(self, _cost(actual_cost))

    def release(self) -> None:
        self._policy._release(self)


class MediaBudgetPolicy:
    """In-process daily budget protection; reservations do not span processes."""

    def __init__(
        self,
        daily_budget: Number,
        consumed_cost: Number = 0,
        day_provider: Optional[Callable[[], date]] = None,
        consumed_cost_provider: Optional[Callable[[date], Number]] = None,
    ) -> None:
        self._daily_budget = _cost(daily_budget)
        self._day_provider = _utc_day if day_provider is None else day_provider
        self._consumed_cost_provider = consumed_cost_provider
        self._lock = Lock()
        self._day = self._get_day()
        self._consumed = self._load_consumed(self._day, consumed_cost)
        self._reserved = Decimal("0")

    @property
    def consumed_cost(self) -> Decimal:
        with self._lock:
            self._refresh_day_locked()
            return self._consumed

    @property
    def reserved_cost(self) -> Decimal:
        with self._lock:
            self._refresh_day_locked()
            return self._reserved

    @property
    def over_budget(self) -> bool:
        with self._lock:
            self._refresh_day_locked()
            return self._consumed > self._daily_budget

    def reserve(self, kind: str, estimated_cost: Number) -> BudgetReservation:
        del kind
        estimate = _cost(estimated_cost)
        with self._lock:
            self._refresh_day_locked()
            projected = self._consumed + self._reserved + estimate
            if self._daily_budget == 0 or projected > self._daily_budget:
                raise BudgetExceeded("media daily budget exceeded")
            self._reserved += estimate
            reservation_day = self._day
        return BudgetReservation(self, estimate, reservation_day)

    def _reconcile(
        self, reservation: BudgetReservation, actual_cost: Decimal
    ) -> bool:
        with self._lock:
            self._refresh_day_locked()
            if reservation._finished:
                assert reservation._within_budget is not None
                return reservation._within_budget
            if reservation._day == self._day:
                self._reserved -= reservation._estimated_cost
            self._consumed += actual_cost
            reservation._finished = True
            reservation._within_budget = self._consumed <= self._daily_budget
            return reservation._within_budget

    def _release(self, reservation: BudgetReservation) -> None:
        with self._lock:
            self._refresh_day_locked()
            if reservation._finished:
                return
            if reservation._day == self._day:
                self._reserved -= reservation._estimated_cost
            reservation._finished = True

    def _get_day(self) -> date:
        current = self._day_provider()
        if not isinstance(current, date):
            raise ValueError("media budget day provider must return a date")
        return current

    def _load_consumed(self, day: date, fallback: Number = 0) -> Decimal:
        if self._consumed_cost_provider is None:
            return _cost(fallback)
        return _cost(self._consumed_cost_provider(day))

    def _refresh_day_locked(self) -> None:
        current = self._get_day()
        if current == self._day:
            return
        consumed = self._load_consumed(current)
        self._day = current
        self._consumed = consumed
        self._reserved = Decimal("0")


__all__ = ["BudgetExceeded", "BudgetReservation", "MediaBudgetPolicy"]
