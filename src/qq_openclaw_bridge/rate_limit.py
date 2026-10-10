import time
from collections import defaultdict, deque
from contextlib import ExitStack
from threading import Lock
from typing import Deque, DefaultDict


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        if limit < 1:
            raise ValueError("rate limit must be at least 1")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: DefaultDict[str, Deque[float]] = defaultdict(deque)
        self._lock = Lock()

    @staticmethod
    def _current_time(now: float = None) -> float:
        return time.time() if now is None else float(now)

    def _events_for_check(self, key: str, current: float) -> Deque[float]:
        events = self._events[key]
        cutoff = current - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def allow(self, key: str, now: float = None) -> bool:
        current = self._current_time(now)
        with self._lock:
            events = self._events_for_check(key, current)
            if len(events) >= self.limit:
                return False
            events.append(current)
            return True

    def allow_pair(
        self,
        key: str,
        other: "RateLimiter",
        other_key: str,
        now: float = None,
    ) -> str:
        """Atomically charge this limiter and ``other``, or charge neither."""
        if type(other) is not RateLimiter:
            raise TypeError("other rate limiter is invalid")
        current = self._current_time(now)
        ordered = (self,) if self is other else tuple(
            sorted((self, other), key=lambda limiter: id(limiter._lock))
        )
        with ExitStack() as stack:
            for limiter in ordered:
                stack.enter_context(limiter._lock)

            first_events = self._events_for_check(key, current)
            if len(first_events) >= self.limit:
                return "first_limited"

            second_events = other._events_for_check(other_key, current)
            if self is other and key == other_key:
                if len(second_events) + 1 >= other.limit:
                    return "second_limited"
            elif len(second_events) >= other.limit:
                return "second_limited"

            first_events.append(current)
            second_events.append(current)
            return "allowed"
