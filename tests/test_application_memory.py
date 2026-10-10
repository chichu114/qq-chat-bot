import tempfile
import unittest
from pathlib import Path


class MemoryApplicationTests(unittest.TestCase):
    def _service(self, root):
        from qq_openclaw_bridge.application.memory import (
            E3MemoryCommandHandler,
            MemoryContextService,
        )
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.memory import MemoryRepository

        repository = MemoryRepository(SqliteDatabase(Path(root) / "state.sqlite3"))
        return (
            repository,
            E3MemoryCommandHandler(repository),
            MemoryContextService(
                repository,
                persona_rules_provider=lambda: ("你叫 chichu", "默认中文"),
                max_chars=400,
            ),
        )

    @staticmethod
    def _request(text, user_id=42):
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(
            user_id=user_id,
            message_id="m-1",
            text=text,
            timestamp=1.0,
        )

    def test_pending_confirm_reject_and_status_commands_are_local(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, handler, _ = self._service(temp_dir)
            first = repository.add_candidate(
                42, "profile", "preference", "用户不吃香菜", 0.9, "m-1"
            )
            second = repository.add_candidate(
                42, "long_term", "project", "项目叫 chichu", 0.8, "m-2"
            )

            pending = handler.handle(self._request("待确认记忆"))
            confirmed = handler.handle(self._request("确认记忆 %d" % first["id"]))
            rejected = handler.handle(self._request("拒绝记忆 %d" % second["id"]))
            status = handler.handle(self._request("记忆状态"))

            self.assertIn("不吃香菜", pending)
            self.assertIn("已确认", confirmed)
            self.assertIn("已拒绝", rejected)
            self.assertIn("已确认 1", status)
            self.assertIn("待确认 0", status)

    def test_unconfirmed_fact_is_not_injected_but_confirmed_fact_is(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, _, context_service = self._service(temp_dir)
            candidate = repository.add_candidate(
                42, "profile", "preference", "用户不吃香菜", 0.95, "m-1"
            )

            before = context_service.enrich(self._request("今晚吃什么"))
            repository.confirm_fact(42, candidate["id"])
            after = context_service.enrich(self._request("今晚点餐照顾我的忌口"))

            self.assertNotIn("不吃香菜", before.context)
            self.assertIn("不吃香菜", after.context)
            self.assertEqual(after.text, "今晚点餐照顾我的忌口")

    def test_confirm_callback_exports_legacy_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, _, _ = self._service(temp_dir)
            candidate = repository.add_candidate(
                42, "long_term", "project", "项目使用 Python", 0.88, "m-1"
            )
            exported = []
            from qq_openclaw_bridge.application.memory import E3MemoryCommandHandler

            handler = E3MemoryCommandHandler(
                repository,
                confirmed_exporter=lambda user_id, text: exported.append((user_id, text)),
            )

            handler.handle(self._request("确认记忆 %d" % candidate["id"]))

            self.assertEqual(exported, [(42, "项目使用 Python")])


if __name__ == "__main__":
    unittest.main()

