import asyncio
import logging
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class EventDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_returns_task_before_operation_finishes(self):
        from qq_openclaw_bridge.application.dispatch import EventDispatcher

        started = asyncio.Event()
        release = asyncio.Event()

        async def operation():
            started.set()
            await release.wait()
            return "done"

        dispatcher = EventDispatcher(2, logging.getLogger("test.dispatch"))
        task = await dispatcher.dispatch(1, operation)
        await started.wait()

        self.assertFalse(task.done())
        release.set()
        await dispatcher.wait()
        self.assertEqual(task.result(), "done")

    async def test_same_user_serializes_and_different_users_overlap(self):
        from qq_openclaw_bridge.application.dispatch import EventDispatcher

        dispatcher = EventDispatcher(4, logging.getLogger("test.dispatch"))
        active = {}
        max_by_user = {}
        total_active = 0
        max_total = 0

        async def operation(user_id):
            nonlocal total_active, max_total
            active[user_id] = active.get(user_id, 0) + 1
            max_by_user[user_id] = max(
                max_by_user.get(user_id, 0), active[user_id]
            )
            total_active += 1
            max_total = max(max_total, total_active)
            await asyncio.sleep(0.03)
            active[user_id] -= 1
            total_active -= 1

        await asyncio.gather(
            dispatcher.dispatch(1, lambda: operation(1)),
            dispatcher.dispatch(1, lambda: operation(1)),
            dispatcher.dispatch(2, lambda: operation(2)),
        )
        await dispatcher.wait()

        self.assertEqual(max_by_user, {1: 1, 2: 1})
        self.assertEqual(max_total, 2)

    async def test_exception_releases_capacity_for_next_operation(self):
        from qq_openclaw_bridge.application.dispatch import EventDispatcher

        dispatcher = EventDispatcher(1, logging.getLogger("test.dispatch"))

        async def broken():
            raise RuntimeError("broken event")

        first = await dispatcher.dispatch(1, broken)
        with self.assertRaisesRegex(RuntimeError, "broken event"):
            await first

        second = await asyncio.wait_for(
            dispatcher.dispatch(2, lambda: _return("next")), timeout=0.2
        )
        self.assertEqual(await second, "next")


async def _return(value):
    return value


if __name__ == "__main__":
    unittest.main()
