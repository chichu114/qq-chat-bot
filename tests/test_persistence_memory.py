import tempfile
import unittest
from pathlib import Path


class MemoryRepositoryTests(unittest.TestCase):
    def _repository(self, root):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.memory import MemoryRepository

        database = SqliteDatabase(Path(root) / "state.sqlite3")
        return database, MemoryRepository(database)

    def test_schema_v5_preserves_existing_documents(self):
        from qq_openclaw_bridge.persistence.database import SCHEMA_VERSION

        with tempfile.TemporaryDirectory() as temp_dir:
            database, repository = self._repository(temp_dir)
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO documents(name, content, updated_at) VALUES (?, ?, ?)",
                    ("memory", "legacy", "2026-07-17T00:00:00+00:00"),
                )

            repository.add_confirmed_fact(1, "profile", "用户不吃香菜", "explicit")

            with database.connect() as connection:
                schema = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                legacy = connection.execute(
                    "SELECT content FROM documents WHERE name='memory'"
                ).fetchone()[0]
            self.assertEqual(schema, SCHEMA_VERSION)
            self.assertEqual(schema, "7")
            self.assertEqual(legacy, "legacy")

    def test_candidate_must_be_confirmed_before_retrieval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            candidate = repository.add_candidate(
                user_id=42,
                layer="profile",
                category="preference",
                content="用户不吃香菜",
                confidence=0.92,
                source_message_id="m-1",
            )

            self.assertEqual(repository.search_confirmed(42, "香菜"), [])
            confirmed = repository.confirm_fact(42, candidate["id"])

            self.assertEqual(confirmed["status"], "confirmed")
            self.assertEqual(repository.search_confirmed(42, "香菜")[0]["id"], candidate["id"])

    def test_rejected_candidate_stays_out_of_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            candidate = repository.add_candidate(
                user_id=42,
                layer="long_term",
                category="project",
                content="用户决定删除整个项目",
                confidence=0.8,
                source_message_id="m-2",
            )

            rejected = repository.reject_fact(42, candidate["id"])

            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(repository.search_confirmed(42, "项目"), [])

            repeated = repository.add_candidate(
                user_id=42,
                layer="long_term",
                category="project",
                content="用户决定删除整个项目",
                confidence=0.95,
                source_message_id="m-3",
            )
            self.assertEqual(repeated["status"], "rejected")

    def test_turns_summary_and_stats_are_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            repository.record_turn(7, "user", "你好", "m-3:user")
            repository.record_turn(7, "assistant", "又见面了。", "m-3:assistant")
            turns = repository.unsummarized_turns(7, limit=10)

            summary = repository.save_summary(
                user_id=7,
                start_turn_id=turns[0]["id"],
                end_turn_id=turns[-1]["id"],
                content="用户和 chichu 打了招呼。",
                model="local",
            )

            self.assertEqual(summary["content"], "用户和 chichu 打了招呼。")
            self.assertEqual(repository.unsummarized_turns(7, limit=10), [])
            stats = repository.stats(7)
            self.assertEqual(stats["summaries"], 1)
            self.assertEqual(stats["turns"], 2)


if __name__ == "__main__":
    unittest.main()
