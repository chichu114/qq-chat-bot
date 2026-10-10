from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class TaskCommandHandlerTests(unittest.TestCase):
    def _request(self, user_id, text):
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(user_id, "message", text, 1.0)

    def _handler(self, root):
        from qq_openclaw_bridge.application.task_commands import TaskCommandHandler
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        repository = E4Repository(SqliteDatabase(Path(root) / "state.sqlite3"))

        class Service:
            def __init__(self):
                self.calls = []

            def cancel(self, task_id, user_id):
                self.calls.append((task_id, user_id))
                return True

        service = Service()
        return repository, service, TaskCommandHandler(repository, service)

    def test_list_status_and_cancel_are_exact_and_owner_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service, handler = self._handler(temp_dir)
            mine = repository.create_task(7, "m1", "media", task_id="12")
            repository.create_task(8, "m2", "media", task_id="opaque-id")

            self.assertIn("12", handler.handle(self._request(7, "  查看任务  ")))
            self.assertIn("queued", handler.handle(self._request(7, "任务状态 12")))
            self.assertEqual(
                handler.handle(self._request(7, "取消任务 12")), "任务 #12 已请求取消。"
            )
            self.assertEqual(service.calls, [(mine["id"], 7)])

            missing = handler.handle(self._request(7, "任务状态 opaque-id"))
            non_owner = handler.handle(self._request(8, "任务状态 12"))
            self.assertEqual(missing, non_owner)
            self.assertIsNone(handler.handle(self._request(7, "任务状态")))
            self.assertIsNone(handler.handle(self._request(7, "取消任务 12 多余")))
            self.assertIsNone(handler.handle(self._request(7, "查看任务 12")))
            self.assertIsNone(handler.handle(self._request(7, "交给模型处理")))

    def test_opaque_id_is_accepted_and_inactive_cancel_does_not_call_service(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, service, handler = self._handler(temp_dir)
            task = repository.create_task(7, "m1", "media", task_id="opaque-id")
            repository.transition_task(task["id"], "queued", "running", "working")
            repository.transition_task(task["id"], "running", "succeeded", "done")

            self.assertIn("succeeded", handler.handle(self._request(7, "任务状态 opaque-id")))
            self.assertEqual(
                handler.handle(self._request(7, "取消任务 opaque-id")), "没找到这项任务。"
            )
            self.assertEqual(service.calls, [])

    def test_restart_list_limit_and_command_chain_routing_remain_owner_scoped(self):
        from qq_openclaw_bridge.application.commands import (
            CallableCommandHandler,
            CommandChain,
        )
        from qq_openclaw_bridge.application.task_commands import TaskCommandHandler
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            repository = E4Repository(SqliteDatabase(path))
            repository.create_task(7, "m1", "media", task_id="one")
            repository.create_task(7, "m2", "media", task_id="two")
            repository.create_task(8, "m3", "media", task_id="other")

            class Service:
                def cancel(self, task_id, user_id):
                    return True

            restarted = TaskCommandHandler(
                E4Repository(SqliteDatabase(path)), Service(), list_limit=1
            )
            listed = restarted.handle(self._request(7, "查看任务"))
            self.assertIn("two", listed)
            self.assertNotIn("one", listed)
            self.assertNotIn("other", listed)
            self.assertEqual(
                restarted.handle(self._request(8, "取消任务 two")), "没找到这项任务。"
            )
            chain = CommandChain((
                restarted,
                CallableCommandHandler(lambda request: "后续处理"),
            ))
            self.assertIn("two", chain.handle(self._request(7, "任务状态 two")))
            self.assertEqual(chain.handle(self._request(7, "未知命令")), "后续处理")


if __name__ == "__main__":
    unittest.main()
