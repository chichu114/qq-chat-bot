import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Set


class EventDispatcher:
    """Owns event capacity, task lifecycle and per-user ordering."""

    def __init__(self, max_pending_events: int, logger: logging.Logger) -> None:
        self._capacity = asyncio.Semaphore(max_pending_events)
        self._logger = logger
        self._user_locks: Dict[int, asyncio.Lock] = {}
        self._tasks: Set[asyncio.Task] = set()

    async def run(
        self, user_id: int, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        user_lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        async with user_lock:
            return await operation()

    async def dispatch(
        self, user_id: int, operation: Callable[[], Awaitable[Any]]
    ) -> asyncio.Task:
        await self._capacity.acquire()

        async def run_operation() -> Any:
            try:
                return await self.run(user_id, operation)
            finally:
                self._capacity.release()

        task = asyncio.create_task(run_operation())
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error(
                "event task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def wait(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
