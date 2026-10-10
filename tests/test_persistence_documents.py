from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class DocumentRepositoryTests(unittest.TestCase):
    def test_wal_v5_and_atomic_markdown_export(self):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.documents import DocumentRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "state.sqlite3"
            export_path = root / "MEMORY.md"
            database = SqliteDatabase(database_path)
            repository = DocumentRepository(database)

            repository.write("memory", export_path, "# MEMORY\n")

            self.assertEqual(
                repository.read("memory", export_path, "unused"), "# MEMORY\n"
            )
            self.assertEqual(database_path.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(database_path) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()[0],
                    "7",
                )


if __name__ == "__main__":
    unittest.main()
