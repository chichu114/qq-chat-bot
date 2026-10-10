from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import sqlite3
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class WorkspaceStateStoreTests(unittest.TestCase):
    def test_imports_markdown_into_wal_database_and_preserves_export(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            markdown = root / "MEMORY.md"
            database = root / "chichu.sqlite3"
            markdown.write_text("# MEMORY\n\n- existing\n", encoding="utf-8")
            store = WorkspaceStateStore(database)

            content = store.read_document("memory", markdown, "# MEMORY\n")

            self.assertEqual(content, "# MEMORY\n\n- existing\n")
            self.assertTrue(database.is_file())
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(database) as connection:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            self.assertEqual(version, "7")

    def test_database_is_canonical_and_writes_atomic_markdown_export(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            markdown = root / "TODO.md"
            database = root / "chichu.sqlite3"
            store = WorkspaceStateStore(database)
            store.write_document("todo", markdown, "# TODO\n\n- first\n")
            markdown.write_text("tampered outside database\n", encoding="utf-8")

            content = store.read_document("todo", markdown, "# TODO\n")

            self.assertEqual(content, "# TODO\n\n- first\n")
            self.assertEqual(markdown.read_text(encoding="utf-8"), content)
            self.assertFalse(markdown.with_suffix(".md.tmp").exists())

    def test_initialization_creates_missing_markdown_documents(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            markdown = root / "MEMORY.md"
            store = WorkspaceStateStore(root / "chichu.sqlite3")

            store.ensure_documents({"memory": (markdown, "# chichu MEMORY\n")})

            self.assertEqual(markdown.read_text(encoding="utf-8"), "# chichu MEMORY\n")

    def test_persists_pending_approval_records(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            store.save_approval(
                approval_id="A1B2C3",
                user_id=10001,
                command="删除记忆 1",
                description="删除第 1 条长期记忆",
                expires_at="2026-07-14T20:02:00+08:00",
                created_at="2026-07-14T20:00:00+08:00",
            )

            approval = store.get_pending_approval(10001)
            store.delete_approval("A1B2C3")

            self.assertEqual(approval["approval_id"], "A1B2C3")
            self.assertEqual(approval["command"], "删除记忆 1")
            self.assertIsNone(store.get_pending_approval(10001))

    def test_housekeeper_and_style_use_database_as_canonical_state(self):
        from qq_openclaw_bridge.housekeeper import Housekeeper
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_bridge.style_memory import StyleMemory

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memory = root / "MEMORY.md"
            todo = root / "TODO.md"
            style = root / "STYLE.md"
            store = WorkspaceStateStore(root / "chichu.sqlite3")
            housekeeper = Housekeeper(memory, todo, state_store=store)
            style_memory = StyleMemory(style, state_store=store)

            housekeeper.handle("记住：我喜欢先给结论")
            style_memory.record_if_feedback("少废话")
            memory.write_text("external stale content\n", encoding="utf-8")
            style.write_text("external stale content\n", encoding="utf-8")

            memory_reply = housekeeper.handle("查看记忆")
            style_reply = style_memory.handle_command("查看风格")

            self.assertIn("我喜欢先给结论", memory_reply)
            self.assertIn("少废话", style_reply)
            self.assertIn("我喜欢先给结论", memory.read_text(encoding="utf-8"))
            self.assertIn("少废话", style.read_text(encoding="utf-8"))

    def test_reminder_updates_shared_sqlite_todo_document(self):
        from qq_openclaw_bridge.housekeeper import Housekeeper
        from qq_openclaw_bridge.reminders import ReminderStore
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memory = root / "MEMORY.md"
            todo = root / "TODO.md"
            state = root / "REMINDER_STATE.md"
            now = datetime(2026, 7, 15, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            store = WorkspaceStateStore(root / "chichu.sqlite3")
            housekeeper = Housekeeper(
                memory,
                todo,
                reminder_state_path=state,
                date_provider=lambda: date(2026, 7, 15),
                now_provider=lambda: now,
                state_store=store,
            )
            reminders = ReminderStore(todo, state_path=state, state_store=store)
            housekeeper.handle("2026-07-15 09:00提醒我开会", user_id=10001)

            due = reminders.collect_due(now)
            reminders.mark_sent(due[0], now)
            content = store.read_document("todo", todo, "# chichu TODO\n")

            self.assertEqual(len(due), 1)
            self.assertIn("reminded=2026-07-15T09:00:00+08:00", content)

    def test_inbound_claim_suppresses_active_and_completed_duplicates(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "chichu.sqlite3"
            now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
            store = WorkspaceStateStore(database)

            self.assertTrue(store.claim_inbound("m-1", 10001, now=now))
            self.assertFalse(
                store.claim_inbound("m-1", 10001, now=now + timedelta(seconds=30))
            )

            restarted = WorkspaceStateStore(database)
            self.assertTrue(
                restarted.claim_inbound(
                    "m-1", 10001, now=now + timedelta(seconds=301)
                )
            )
            restarted.complete_inbound("m-1", now=now + timedelta(seconds=302))
            self.assertFalse(
                restarted.claim_inbound(
                    "m-1", 10001, now=now + timedelta(seconds=700)
                )
            )

    def test_legacy_sent_final_reconciles_before_stale_duplicate_or_claim(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "chichu.sqlite3"
            old = datetime(2026, 7, 30, tzinfo=timezone.utc)
            store = WorkspaceStateStore(database)
            store.claim_inbound("m-legacy", 10001, now=old)
            store.enqueue_outbox(
                kind="reply",
                user_id=10001,
                message="already sent",
                dedupe_key="reply:m-legacy",
                echo="reply:m-legacy",
                now=old,
            )
            with store._connect() as connection:
                connection.execute(
                    """
                    UPDATE outbox SET status='sent', attempts=1, sent_at=?
                    WHERE dedupe_key='reply:m-legacy'
                    """,
                    (old.isoformat(),),
                )

            restarted = WorkspaceStateStore(database)
            stale = old + timedelta(seconds=301)
            self.assertTrue(
                restarted.is_duplicate_inbound("m-legacy", now=stale)
            )
            self.assertFalse(
                restarted.claim_inbound("m-legacy", 10001, now=stale)
            )
            with restarted._connect() as connection:
                inbound = connection.execute(
                    """
                    SELECT status, attempts FROM inbound_messages
                    WHERE message_id='m-legacy'
                    """
                ).fetchone()
            self.assertEqual(inbound, ("completed", 1))

    def test_legacy_sent_final_reconciliation_rejects_cross_user_row(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "chichu.sqlite3"
            old = datetime(2026, 7, 30, tzinfo=timezone.utc)
            store = WorkspaceStateStore(database)
            store.claim_inbound("m-cross", 10001, now=old)
            store.enqueue_outbox(
                kind="reply",
                user_id=20002,
                message="other owner",
                dedupe_key="reply:m-cross",
                echo="reply:m-cross",
                now=old,
            )
            with store._connect() as connection:
                connection.execute(
                    """
                    UPDATE outbox SET status='sent', attempts=1, sent_at=?
                    WHERE dedupe_key='reply:m-cross'
                    """,
                    (old.isoformat(),),
                )

            restarted = WorkspaceStateStore(database)
            stale = old + timedelta(seconds=301)
            self.assertFalse(
                restarted.is_duplicate_inbound("m-cross", now=stale)
            )
            self.assertTrue(
                restarted.claim_inbound("m-cross", 10001, now=stale)
            )
            with restarted._connect() as connection:
                inbound = connection.execute(
                    """
                    SELECT status, attempts FROM inbound_messages
                    WHERE message_id='m-cross'
                    """
                ).fetchone()
            self.assertEqual(inbound, ("processing", 2))

    def test_controlled_task7_write_apis_reject_malformed_ownership(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        invalid_values = (
            True, False, "10001", 10001.0, 0, -1, 10**20,
        )
        api_names = (
            "inbound", "outbox", "final_pair", "task", "media",
            "task_notice", "task_final_commit",
        )
        for value in invalid_values:
            for api_name in api_names:
                with self.subTest(value=value, api=api_name):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        store = WorkspaceStateStore(
                            Path(temp_dir) / "chichu.sqlite3"
                        )
                        if api_name in ("task_notice", "task_final_commit"):
                            task = store.e4_repository.create_task(
                                10001, "m-owner", "long_task", task_id="t-owner"
                            )
                            store.e4_repository.transition_task(
                                task["id"], "queued", "running", "running"
                            )

                        with self.assertRaises(ValueError):
                            if api_name == "inbound":
                                store.claim_inbound("m-owner", value)
                            elif api_name == "outbox":
                                store.enqueue_outbox(
                                    kind="command", user_id=value, message="x",
                                    dedupe_key="command:owner", echo="command:owner",
                                )
                            elif api_name == "final_pair":
                                store.enqueue_final_with_observation(
                                    kind="reply", user_id=value, message="x",
                                    dedupe_key="reply:m-owner", echo="reply:m-owner",
                                    observation_message="{}",
                                    observation_key="observation:m-owner",
                                )
                            elif api_name == "task":
                                store.e4_repository.create_task(
                                    value, "m-owner", "long_task", task_id="t-owner"
                                )
                            elif api_name == "media":
                                store.e4_repository.create_media_asset(
                                    value, "m-owner", "image", asset_id="a-owner"
                                )
                            elif api_name == "task_notice":
                                store.enqueue_task_notice(
                                    task_id="t-owner", user_id=value, message="x",
                                    role="ack",
                                )
                            else:
                                store.commit_task_final(
                                    task_id="t-owner", user_id=value, message="x",
                                    final_key="reply:m-owner",
                                    final_echo="task:t-owner:final",
                                    observation_message="{}",
                                    observation_key="observation:m-owner",
                                )

    def test_sql_true_has_integer_one_provenance_contract(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            row = store.enqueue_outbox(
                kind="command", user_id=10001, message="x",
                dedupe_key="command:sql-true", echo="command:sql-true",
            )
            with store._connect() as connection:
                connection.execute(
                    "UPDATE outbox SET user_id=TRUE WHERE id=?", (row["id"],)
                )
                stored = connection.execute(
                    "SELECT user_id, typeof(user_id) FROM outbox WHERE id=?",
                    (row["id"],),
                ).fetchone()

            self.assertEqual(stored, (1, "integer"))
            claimed = store.claim_due_outbox()
            self.assertEqual(claimed["user_id"], 1)

    def test_outbox_dedupes_retries_and_persists_sent_state(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "chichu.sqlite3"
            now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
            store = WorkspaceStateStore(database)

            first = store.enqueue_outbox(
                kind="reply",
                user_id=10001,
                message="第一条回复",
                dedupe_key="reply:m-1",
                echo="reply:m-1",
                now=now,
            )
            duplicate = store.enqueue_outbox(
                kind="reply",
                user_id=10001,
                message="不应覆盖",
                dedupe_key="reply:m-1",
                now=now,
            )

            self.assertEqual(first["id"], duplicate["id"])
            self.assertEqual(duplicate["message"], "第一条回复")

            claimed = store.claim_due_outbox(now=now, max_attempts=8)
            self.assertEqual(claimed["status"], "sending")
            self.assertEqual(claimed["attempts"], 1)
            store.mark_outbox_retry(
                claimed["id"],
                claimed["claim_token"],
                "socket closed",
                retry_base_seconds=2,
                max_attempts=8,
                now=now,
            )
            self.assertIsNone(
                store.claim_due_outbox(
                    now=now + timedelta(seconds=1), max_attempts=8
                )
            )

            restarted = WorkspaceStateStore(database)
            retried = restarted.claim_due_outbox(
                now=now + timedelta(seconds=2), max_attempts=8
            )
            self.assertEqual(retried["attempts"], 2)
            restarted.mark_outbox_sent(
                retried["id"], retried["claim_token"],
                now=now + timedelta(seconds=3)
            )

            sent = restarted.get_outbox("reply:m-1")
            self.assertEqual(sent["status"], "sent")
            self.assertEqual(sent["sent_at"], "2026-07-16T12:00:03+00:00")
            self.assertIsNone(
                restarted.claim_due_outbox(
                    now=now + timedelta(seconds=4), max_attempts=8
                )
            )

    def test_outbox_exhaustion_and_runtime_metrics_are_reported(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            row = store.enqueue_outbox(
                kind="reminder",
                user_id=10001,
                message="提醒",
                dedupe_key="reminder:r-1",
                now=now,
            )
            claimed = store.claim_due_outbox(now=now, max_attempts=1)
            store.mark_outbox_retry(
                claimed["id"],
                claimed["claim_token"],
                "failed",
                retry_base_seconds=2,
                max_attempts=1,
                now=now,
            )
            store.set_runtime_metric("last_agent_success_at", now.isoformat())

            stats = store.transport_stats()

            self.assertEqual(row["status"], "pending")
            self.assertEqual(stats["outbox_failed"], 1)
            self.assertEqual(stats["outbox_pending"], 0)
            self.assertEqual(
                stats["last_agent_success_at"], "2026-07-16T12:00:00+00:00"
            )

    def test_store_composition_leaves_routing_pending_for_opt_in_runtime(self):
        from types import SimpleNamespace
        from qq_openclaw_bridge.storage import (
            WorkspaceStateStore,
            create_workspace_state_store,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.sqlite3"
            direct_store = WorkspaceStateStore(database)
            pending = direct_store.routing_repository.start_decision(
                user_id=10001,
                source_message_sha256="a" * 64,
                message_sha256="b" * 64,
                message_preview="pending",
                routing_config_version=1,
                rule_version="rules-v1",
                dispatch_overhead_ms=1,
            )
            self.assertEqual(
                direct_store.routing_repository.recent_decisions(1)[0]["status"],
                "pending",
            )
            config = SimpleNamespace(
                enable_sqlite_store=True,
                state_database_path=str(database),
                housekeeper_memory_path=str(root / "MEMORY.md"),
                housekeeper_todo_path=str(root / "TODO.md"),
                style_memory_path=str(root / "STYLE.md"),
                reminder_state_path=str(root / "reminder.state"),
            )
            composed = create_workspace_state_store(config)
            decision = composed.routing_repository.recent_decisions(1)[0]
            self.assertEqual(decision["id"], pending["id"])
            self.assertEqual(decision["status"], "pending")


if __name__ == "__main__":
    unittest.main()
