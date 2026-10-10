from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ApprovalRepositoryTests(unittest.TestCase):
    def test_new_pending_approval_replaces_previous_for_user(self):
        from qq_openclaw_bridge.persistence.approvals import ApprovalRepository
        from qq_openclaw_bridge.persistence.database import SqliteDatabase

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ApprovalRepository(
                SqliteDatabase(Path(temp_dir) / "state.sqlite3")
            )
            repository.save("A1", 1, "旧命令", "旧", "later", "now")
            repository.save("A2", 1, "新命令", "新", "later", "now2")

            pending = repository.get_pending(1)
            self.assertEqual(pending["approval_id"], "A2")
            repository.delete("A2")
            self.assertIsNone(repository.get_pending(1))

    def test_recent_approvals_are_deterministic_and_clamped_to_one_hundred(self):
        from qq_openclaw_bridge.persistence.approvals import ApprovalRepository
        from qq_openclaw_bridge.persistence.database import SqliteDatabase

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ApprovalRepository(
                SqliteDatabase(Path(temp_dir) / "state.sqlite3")
            )
            for index in range(105):
                repository.save(
                    "A%03d" % index,
                    index,
                    "command-%03d" % index,
                    "description-%03d" % index,
                    "later",
                    "2026-07-30T12:00:00+00:00",
                )

            recent = repository.list_recent(limit=1000)

            self.assertEqual(len(recent), 100)
            self.assertEqual(recent[0]["approval_id"], "A104")
            self.assertEqual(recent[-1]["approval_id"], "A005")
            self.assertEqual(
                repository.list_recent(limit=0)[0]["approval_id"], "A104"
            )


if __name__ == "__main__":
    unittest.main()
