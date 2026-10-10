from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class E4RepositoryTests(unittest.TestCase):
    def test_task_final_commit_rejects_pair_owned_by_another_task_atomically(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            first = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t1"
            )
            store.e4_repository.transition_task(
                first["id"], "queued", "running", "delivering"
            )
            self.assertIsNotNone(
                store.commit_task_final(
                    task_id="t1",
                    user_id=10001,
                    message="first",
                    final_key="reply:m1",
                    final_echo="task:t1:final",
                    observation_message='{"first":true}',
                    observation_key="observation:m1",
                )
            )
            store.e4_repository.finish_task_delivery("t1", succeeded=False)

            second = store.e4_repository.create_task(
                10001, "m1", "long_task", task_id="t2"
            )
            store.e4_repository.transition_task(
                second["id"], "queued", "running", "delivering"
            )

            committed = store.commit_task_final(
                task_id="t2",
                user_id=10001,
                message="second",
                final_key="reply:m1",
                final_echo="task:t2:final",
                observation_message='{"second":true}',
                observation_key="observation:m1",
            )

            self.assertIsNone(committed)
            self.assertEqual(
                store.e4_repository.get_task("t2")["stage"], "delivering"
            )
            self.assertEqual(store.get_outbox("reply:m1")["echo"], "task:t1:final")
            self.assertEqual(
                store.get_outbox("observation:m1")["message"], '{"first":true}'
            )

    def _repository(self, root):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        database = SqliteDatabase(Path(root) / "state.sqlite3")
        return database, E4Repository(database)

    def test_owner_cancel_terminalizes_source_inbound_but_interruption_does_not(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            now = datetime(2026, 7, 27, tzinfo=timezone.utc)
            store.claim_inbound("owner-cancel", 10001, now=now)
            owner_task = store.e4_repository.create_task(
                10001, "owner-cancel", "long_task", now=now, task_id="owner-task"
            )
            store.e4_repository.transition_task(
                owner_task["id"], "queued", "running", "running", now=now
            )

            self.assertIsNotNone(
                store.e4_repository.request_cancel("owner-task", 10001, now=now)
            )
            self.assertFalse(
                store.claim_inbound(
                    "owner-cancel", 10001, now=now + timedelta(seconds=301)
                )
            )

            store.claim_inbound("interrupted", 10001, now=now)
            interrupted = store.e4_repository.create_task(
                10001, "interrupted", "long_task", now=now, task_id="interrupted-task"
            )
            store.e4_repository.transition_task(
                interrupted["id"], "queued", "running", "running", now=now
            )
            store.e4_repository.transition_task(
                interrupted["id"], "running", "failed", "interrupted", now=now
            )
            self.assertTrue(
                store.claim_inbound(
                    "interrupted", 10001, now=now + timedelta(seconds=301)
                )
            )

    def test_schema_v4_preserves_v3_data_and_creates_e4_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata (
                      key TEXT PRIMARY KEY,
                      value TEXT NOT NULL
                    );
                    INSERT INTO metadata(key, value) VALUES ('schema_version', '3');
                    CREATE TABLE memory_facts (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER NOT NULL,
                      layer TEXT NOT NULL,
                      category TEXT NOT NULL,
                      content TEXT NOT NULL,
                      normalized_content TEXT NOT NULL,
                      status TEXT NOT NULL,
                      confidence REAL NOT NULL,
                      source_kind TEXT NOT NULL,
                      source_message_id TEXT,
                      keywords_json TEXT NOT NULL,
                      vector_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      confirmed_at TEXT,
                      UNIQUE(user_id, normalized_content)
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO memory_facts("
                    "user_id, layer, category, content, normalized_content, "
                    "status, confidence, source_kind, keywords_json, vector_json, "
                    "created_at, updated_at"
                    ") VALUES (1, 'profile', 'preference', '不吃香菜', "
                    "'不吃香菜', 'confirmed', 1, 'test', '[]', '[]', "
                    "'2026-07-18T00:00:00+00:00', "
                    "'2026-07-18T00:00:00+00:00')"
                )
            database, repository = self._repository(temp_dir)

            self.assertEqual(repository.stats()["tasks"], 0)

            with database.connect() as connection:
                schema = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                content = connection.execute(
                    "SELECT content FROM memory_facts"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                media_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(media_assets)")
                }
                health_index_columns = [
                    row[2]
                    for row in connection.execute(
                        "PRAGMA index_info(health_events_component_created_at)"
                    )
                ]
                media_expiry_index_columns = [
                    row[2]
                    for row in connection.execute(
                        "PRAGMA index_info(media_assets_expires_at)"
                    )
                ]
                model_call_time_index_columns = [
                    row[2]
                    for row in connection.execute(
                        "PRAGMA index_info(model_calls_created_at)"
                    )
                ]
                indexes = {
                    table: {
                        row[1]
                        for row in connection.execute("PRAGMA index_list(%s)" % table)
                    }
                    for table in (
                        "media_assets",
                        "long_tasks",
                        "task_events",
                        "model_calls",
                        "health_events",
                    )
                }
            self.assertEqual(schema, "7")
            self.assertEqual(content, "不吃香菜")
            self.assertIn("cache_path", media_columns)
            self.assertNotIn("media_cache_paths", tables)
            self.assertTrue(
                {
                    "media_assets",
                    "long_tasks",
                    "task_events",
                    "model_calls",
                    "health_events",
                }.issubset(tables)
            )
            self.assertEqual(health_index_columns, ["component", "created_at"])
            self.assertEqual(media_expiry_index_columns, ["expires_at"])
            self.assertEqual(model_call_time_index_columns, ["created_at"])
            self.assertTrue(
                {
                    "media_assets_message_id",
                    "media_assets_status_expires",
                    "media_assets_expires_at",
                }.issubset(indexes["media_assets"])
            )
            self.assertTrue(
                {
                    "long_tasks_user_status",
                    "long_tasks_status_created",
                }.issubset(indexes["long_tasks"])
            )
            self.assertIn("task_events_task_id", indexes["task_events"])
            self.assertTrue(
                {"model_calls_stats", "model_calls_created_at"}.issubset(
                    indexes["model_calls"]
                )
            )
            self.assertIn("health_events_component_created_at", indexes["health_events"])

    def test_existing_v4_media_assets_gets_cache_path_column_during_v5_migration(self):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata (
                      key TEXT PRIMARY KEY,
                      value TEXT NOT NULL
                    );
                    INSERT INTO metadata(key, value) VALUES ('schema_version', '4');
                    CREATE TABLE media_assets (
                      id TEXT PRIMARY KEY,
                      user_id INTEGER NOT NULL,
                      message_id TEXT NOT NULL,
                      kind TEXT NOT NULL,
                      display_name TEXT NOT NULL,
                      mime TEXT,
                      byte_size INTEGER,
                      sha256 TEXT,
                      status TEXT NOT NULL,
                      error_detail TEXT,
                      created_at TEXT NOT NULL,
                      expires_at TEXT
                    );
                    INSERT INTO media_assets(
                      id, user_id, message_id, kind, display_name, status, created_at
                    ) VALUES ('old', 1, 'm-old', 'file', 'old.txt', 'ready',
                              '2026-07-19T00:00:00+00:00');
                    """
                )

            database = SqliteDatabase(database_path)

            with database.connect() as connection:
                columns = [
                    row[1]
                    for row in connection.execute("PRAGMA table_info(media_assets)")
                ]
                schema = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                old_row = connection.execute(
                    "SELECT id, cache_path FROM media_assets WHERE id='old'"
                ).fetchone()
            self.assertIn("cache_path", columns)
            self.assertEqual(schema, "7")
            self.assertEqual(old_row, ("old", None))

    def test_task_terminal_state_is_irreversible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            task = repository.create_task(1, "m1", "media")

            repository.transition_task(
                task["id"], "queued", "running", "validating"
            )
            repository.transition_task(
                task["id"], "running", "succeeded", "delivering"
            )

            with self.assertRaisesRegex(ValueError, "terminal"):
                repository.transition_task(
                    task["id"], "succeeded", "running", "reasoning"
                )

    def test_queued_task_cannot_transition_directly_to_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            task = repository.create_task(1, "m-queued-failed", "media")

            with self.assertRaisesRegex(ValueError, "invalid task transition"):
                repository.transition_task(task["id"], "queued", "failed", "interrupted")

    def test_transition_uses_compare_and_swap_and_appends_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            task = repository.create_task(7, "m-cas", "media")

            running = repository.transition_task(
                task["id"], "queued", "running", "downloading", "开始下载"
            )

            self.assertEqual(running["status"], "running")
            self.assertEqual(running["stage"], "downloading")
            with self.assertRaisesRegex(ValueError, "state changed"):
                repository.transition_task(
                    task["id"], "queued", "cancelled", "cancelled"
                )
            events = repository.task_events(task["id"])
            self.assertEqual(events[-1]["event_type"], "transition")
            self.assertEqual(events[-1]["detail"], "开始下载")

    def test_stale_expected_state_fails_across_independent_repositories(self):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.sqlite3"
            first = E4Repository(SqliteDatabase(database_path))
            second = E4Repository(SqliteDatabase(database_path))
            task = first.create_task(7, "m-cas-two", "media")

            first.transition_task(task["id"], "queued", "running", "downloading")

            with self.assertRaisesRegex(ValueError, "state changed"):
                second.transition_task(task["id"], "queued", "cancelled", "cancelled")
            self.assertEqual(first.task_events(task["id"])[-1]["event_type"], "transition")

    def test_success_cas_rejects_cross_repository_cancel_request(self):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.persistence.e4 import E4Repository

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.sqlite3"
            owner = E4Repository(SqliteDatabase(database_path))
            canceller = E4Repository(SqliteDatabase(database_path))
            task = owner.create_task(7, "m-cancel-race", "media")
            owner.transition_task(
                task["id"], "queued", "running", "working", require_not_cancelled=True
            )
            self.assertIsNotNone(canceller.request_cancel(task["id"], 7))

            with self.assertRaisesRegex(ValueError, "state changed"):
                owner.transition_task(
                    task["id"],
                    "running",
                    "succeeded",
                    "succeeded",
                    require_not_cancelled=True,
                )

            self.assertEqual(owner.get_task(task["id"])["status"], "running")

    def test_notification_sent_is_a_terminal_safe_atomic_audit_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            task = repository.create_task(7, "m-notice", "media")
            repository.transition_task(task["id"], "queued", "running", "working")
            repository.transition_task(task["id"], "running", "succeeded", "done")

            updated = repository.record_task_notification_sent(
                task["id"], "ack", "通知已发送"
            )

            self.assertEqual(updated["status"], "succeeded")
            self.assertIsNotNone(updated["last_notified_at"])
            self.assertEqual(repository.task_events(task["id"])[-1]["event_type"], "notification_sent")

    def test_notification_sent_preserves_microsecond_precision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            now = datetime(
                2026, 7, 29, 0, 0, 0, 900000, tzinfo=timezone.utc
            )
            task = repository.create_task(
                10001, "m1", "long_task", task_id="t1", now=now
            )
            repository.transition_task(
                task["id"], "queued", "running", "running", now=now
            )

            updated = repository.record_task_notification_sent(
                task["id"], "running", "50%", now=now
            )

            expected = now.isoformat(timespec="microseconds")
            self.assertEqual(updated["last_notified_at"], expected)
            event = repository.task_events(task["id"])[-1]
            self.assertEqual(event["created_at"], expected)

    def test_cancel_request_is_scoped_to_task_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            task = repository.create_task(42, "m-owner", "media")

            self.assertIsNone(repository.request_cancel(task["id"], 99))
            cancelled = repository.request_cancel(task["id"], 42)

            self.assertEqual(cancelled["cancel_requested"], 1)
            self.assertEqual(cancelled["user_id"], 42)

    def test_duplicate_cancel_request_returns_none_and_appends_one_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            task = repository.create_task(42, "m-cancel", "media")

            self.assertIsNotNone(repository.request_cancel(task["id"], 42))
            self.assertIsNone(repository.request_cancel(task["id"], 42))
            self.assertEqual(
                [event["event_type"] for event in repository.task_events(task["id"])],
                ["cancel_requested"],
            )

    def test_append_task_event_rejects_unknown_task_without_orphan_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database, repository = self._repository(temp_dir)

            with self.assertRaisesRegex(KeyError, "task not found"):
                repository.append_task_event("missing", "progress", "validating")
            with database.connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
            self.assertEqual(count, 0)

    def test_terminal_task_allows_audit_event_but_rejects_late_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            task = repository.create_task(1, "m-terminal-event", "media")
            repository.transition_task(task["id"], "queued", "running", "working")
            repository.transition_task(task["id"], "running", "succeeded", "done")
            audit = repository.append_task_event(task["id"], "audit", "late")
            self.assertEqual(audit["event_type"], "audit")
            before = len(repository.task_events(task["id"]))

            self.assertIsNone(repository.record_task_progress(task["id"], "late"))
            self.assertEqual(len(repository.task_events(task["id"])), before)

    def test_transition_rolls_back_when_event_insert_trigger_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database, repository = self._repository(temp_dir)
            task = repository.create_task(1, "m-trigger", "media")
            with database.connect() as connection:
                connection.execute(
                    """
                    CREATE TRIGGER fail_transition_event
                    BEFORE INSERT ON task_events
                    WHEN NEW.event_type = 'transition'
                    BEGIN
                      SELECT RAISE(FAIL, 'event insertion failed');
                    END;
                    """
                )

            with self.assertRaisesRegex(sqlite3.IntegrityError, "event insertion failed"):
                repository.transition_task(task["id"], "queued", "running", "downloading")
            self.assertEqual(repository.list_tasks()[0]["status"], "queued")
            self.assertEqual(repository.task_events(task["id"]), [])

    def test_health_events_are_only_recorded_on_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            earlier = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
            middle = earlier + timedelta(minutes=1)
            later = middle + timedelta(minutes=1)

            first = repository.record_health_change(
                "media_api", "healthy", "probe ok", now=middle
            )
            duplicate = repository.record_health_change(
                "media_api", "healthy", "still ok", now=later
            )
            stale = repository.record_health_change(
                "media_api", "unhealthy", "timeout", now=earlier
            )
            recovered = repository.record_health_change(
                "media_api", "degraded", "recovered", now=later
            )

            self.assertIsNotNone(first)
            self.assertIsNone(duplicate)
            self.assertIsNone(stale)
            self.assertIsNotNone(recovered)
            timeline = repository.health_timeline(component="media_api")
            self.assertEqual(
                [item["status"] for item in timeline],
                ["degraded", "healthy"],
            )

    def test_workspace_state_store_exposes_e4_repository(self):
        from qq_openclaw_bridge.persistence.e4 import E4Repository
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")

            self.assertIsInstance(store.e4_repository, E4Repository)
            self.assertIs(store.e4_repository, store.e4_repository)

    def test_media_model_and_task_queries_return_structured_stats(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            now = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
            asset = repository.create_media_asset(
                user_id=5,
                message_id="m-media",
                kind="image",
                display_name="screen.png",
                mime="image/png",
                byte_size=128,
                sha256="a" * 64,
                status="ready",
                expires_at=now + timedelta(hours=1),
                now=now,
            )
            task = repository.create_task(5, "m-media", "media", now=now)
            repository.append_task_event(
                task["id"], "progress", "validating", "已校验", now=now
            )
            repository.record_model_call(
                call_kind="image",
                provider="compatible",
                model="vision-small",
                success=True,
                user_id=5,
                task_id=task["id"],
                input_tokens=100,
                output_tokens=20,
                duration_ms=250,
                estimated_cost=0.012,
                now=now,
            )

            self.assertEqual(asset["display_name"], "screen.png")
            self.assertIsNone(asset["cache_path"])
            self.assertEqual(
                repository.list_tasks(user_id=5)[0]["id"], task["id"]
            )
            self.assertEqual(
                repository.task_events(task["id"])[0]["stage"],
                "validating",
            )
            model_stats = repository.model_stats()
            self.assertEqual(model_stats[0]["calls"], 1)
            self.assertEqual(model_stats[0]["input_tokens"], 100)
            self.assertEqual(repository.media_stats()["ready"], 1)

    def test_expired_media_query_only_returns_registered_due_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
            due = repository.create_media_asset(
                user_id=5,
                message_id="m-due",
                kind="file",
                display_name="due.pdf",
                status="ready",
                expires_at=now,
                now=now - timedelta(hours=1),
                cache_path=str(Path(temp_dir) / "cache" / "due.bin"),
            )
            repository.create_media_asset(
                user_id=5,
                message_id="m-future",
                kind="file",
                display_name="future.pdf",
                status="ready",
                expires_at=now + timedelta(hours=1),
                now=now,
                cache_path=str(Path(temp_dir) / "cache" / "future.bin"),
            )
            repository.create_media_asset(
                user_id=5,
                message_id="m-untracked",
                kind="file",
                display_name="untracked.pdf",
                status="ready",
                expires_at=now,
                now=now,
            )

            expired = repository.list_expired_media(now)

            self.assertEqual([item["id"] for item in expired], [due["id"]])
            self.assertEqual(
                due["cache_path"],
                str(Path(temp_dir) / "cache" / "due.bin"),
            )
            self.assertEqual(
                expired[0]["cache_path"],
                str(Path(temp_dir) / "cache" / "due.bin"),
            )

    def test_mark_media_expired_updates_status_and_detail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            asset = repository.create_media_asset(
                user_id=5,
                message_id="m-expire",
                kind="image",
                display_name="image.png",
                status="ready",
                cache_path=str(Path(temp_dir) / "cache" / "image.bin"),
            )

            updated = repository.mark_media_expired(asset["id"], "ttl elapsed")

            self.assertEqual(updated["status"], "expired")
            self.assertEqual(updated["error_detail"], "ttl elapsed")
            self.assertIsNone(repository.mark_media_expired("missing"))

    def test_create_media_asset_rolls_back_when_hardening_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database, repository = self._repository(temp_dir)

            with mock.patch.object(
                database,
                "harden_files",
                side_effect=OSError("chmod failed"),
            ):
                with self.assertRaisesRegex(OSError, "chmod failed"):
                    repository.create_media_asset(
                        user_id=5,
                        message_id="m-harden",
                        kind="image",
                        display_name="image.png",
                        status="ready",
                    )

            with database.connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0]
            self.assertEqual(count, 0)

    def test_cleanup_error_is_recorded_without_expiring_retryable_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
            asset = repository.create_media_asset(
                user_id=5,
                message_id="m-retry",
                kind="file",
                display_name="retry.txt",
                status="ready",
                expires_at=now,
                cache_path=str(Path(temp_dir) / "cache" / "retry.bin"),
            )

            quarantine_path = str(Path(temp_dir) / "cache" / ".quarantine-retry")
            updated = repository.record_media_cleanup_error(
                asset["id"],
                "cache unlink failed",
                now=now,
                cache_path=quarantine_path,
            )

            self.assertEqual(updated["status"], "ready")
            self.assertIn("cleanup_retry_attempt=1", updated["error_detail"])
            self.assertEqual(updated["cache_path"], quarantine_path)
            self.assertEqual(repository.list_expired_media(now), [])
            self.assertEqual(
                [item["id"] for item in repository.list_expired_media(now + timedelta(minutes=5))],
                [asset["id"]],
            )

    def test_cleanup_error_update_rolls_back_when_hardening_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database, repository = self._repository(temp_dir)
            now = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
            original_path = str(Path(temp_dir) / "cache" / "registered.bin")
            retained_path = str(Path(temp_dir) / "cache" / ".delete-retained")
            asset = repository.create_media_asset(
                user_id=5,
                message_id="m-cleanup-harden",
                kind="file",
                display_name="registered.bin",
                status="ready",
                expires_at=now,
                cache_path=original_path,
            )

            with mock.patch.object(
                database,
                "harden_files",
                side_effect=OSError("chmod failed"),
            ):
                with self.assertRaisesRegex(OSError, "chmod failed"):
                    repository.record_media_cleanup_error(
                        asset["id"],
                        "cache unlink failed",
                        now=now,
                        cache_path=retained_path,
                    )

            with database.connect() as connection:
                stored = connection.execute(
                    "SELECT status, error_detail, expires_at, cache_path "
                    "FROM media_assets WHERE id=?",
                    (asset["id"],),
                ).fetchone()
            self.assertEqual(
                stored,
                (
                    "ready",
                    None,
                    asset["expires_at"],
                    original_path,
                ),
            )

    def test_cleanup_retry_state_survives_restart_and_becomes_terminal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database, repository = self._repository(temp_dir)
            now = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
            asset = repository.create_media_asset(
                user_id=5,
                message_id="m-restart",
                kind="file",
                display_name="retry.txt",
                status="ready",
                expires_at=now,
                cache_path=str(Path(temp_dir) / "cache" / "retry.bin"),
            )

            first = repository.record_media_cleanup_error(
                asset["id"], "cache unlink failed", now=now
            )
            from qq_openclaw_bridge.persistence.e4 import E4Repository

            restarted = E4Repository(database)
            second_at = now + timedelta(minutes=5)
            second = restarted.record_media_cleanup_error(
                asset["id"], "cache unlink failed", now=second_at
            )
            third_at = second_at + timedelta(minutes=15)
            third = restarted.record_media_cleanup_error(
                asset["id"], "cache unlink failed", now=third_at
            )
            terminal_at = third_at + timedelta(hours=1)
            terminal_path = str(
                Path(temp_dir) / "cache" / ".delete-terminal-retained"
            )
            terminal = restarted.record_media_cleanup_error(
                asset["id"],
                "cache unlink failed",
                now=terminal_at,
                cache_path=terminal_path,
            )

            self.assertIn("cleanup_retry_attempt=1", first["error_detail"])
            self.assertEqual(
                datetime.fromisoformat(first["expires_at"]),
                now + timedelta(minutes=5),
            )
            self.assertIn("cleanup_retry_attempt=2", second["error_detail"])
            self.assertEqual(
                datetime.fromisoformat(second["expires_at"]),
                second_at + timedelta(minutes=15),
            )
            self.assertIn("cleanup_retry_attempt=3", third["error_detail"])
            self.assertEqual(
                datetime.fromisoformat(third["expires_at"]),
                third_at + timedelta(hours=1),
            )
            self.assertEqual(terminal["status"], "expired")
            self.assertIn("cleanup abandoned after 3 attempts", terminal["error_detail"])
            self.assertEqual(terminal["cache_path"], terminal_path)

            unchanged = terminal["error_detail"]
            self.assertIsNone(
                restarted.record_media_cleanup_error(
                    asset["id"], "must not rewrite", now=terminal_at + timedelta(hours=1)
                )
            )
            with database.connect() as connection:
                stored = connection.execute(
                    "SELECT status, error_detail, cache_path "
                    "FROM media_assets WHERE id=?",
                    (asset["id"],),
                ).fetchone()
            self.assertEqual(stored, ("expired", unchanged, terminal_path))

    def test_model_stats_marks_mixed_known_and_unknown_metrics_as_unknown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)

            repository.record_model_call(
                call_kind="image",
                provider="compatible",
                model="vision-small",
                success=True,
                input_tokens=100,
                output_tokens=20,
                duration_ms=250,
                estimated_cost=0.012,
            )
            repository.record_model_call(
                call_kind="image",
                provider="compatible",
                model="vision-small",
                success=True,
            )

            stats = repository.model_stats()[0]

            self.assertEqual(stats["calls"], 2)
            self.assertIsNone(stats["input_tokens"])
            self.assertIsNone(stats["output_tokens"])
            self.assertIsNone(stats["average_duration_ms"])
            self.assertIsNone(stats["estimated_cost"])
            self.assertEqual(stats["known_input_token_calls"], 1)
            self.assertEqual(stats["known_output_token_calls"], 1)
            self.assertEqual(stats["known_duration_calls"], 1)
            self.assertEqual(stats["known_cost_calls"], 1)

    def test_model_stats_p95_handles_unknown_one_and_small_samples(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
            repository.record_model_call(
                "chat", "provider", "unknown", True, now=now
            )
            repository.record_model_call(
                "chat", "provider", "single", True, duration_ms=42, now=now
            )
            for duration in (100, 200):
                repository.record_model_call(
                    "chat",
                    "provider",
                    "small",
                    True,
                    duration_ms=duration,
                    now=now,
                )

            stats = {
                item["model"]: item
                for item in repository.model_stats(
                    since=now - timedelta(hours=24)
                )
            }

            self.assertIsNone(stats["unknown"]["p95_latency_ms"])
            self.assertEqual(stats["single"]["p95_latency_ms"], 42)
            self.assertEqual(stats["small"]["p95_latency_ms"], 200)

    def test_consumed_model_cost_for_day_uses_only_known_costs_in_utc_day(self):
        from decimal import Decimal

        with tempfile.TemporaryDirectory() as temp_dir:
            _, repository = self._repository(temp_dir)
            day = datetime(2026, 7, 23, tzinfo=timezone.utc)
            before = day - timedelta(seconds=1)
            after = day + timedelta(days=1)

            repository.record_model_call(
                "image", "compatible", "vision", True, estimated_cost=0.10, now=day
            )
            repository.record_model_call(
                "image",
                "compatible",
                "vision",
                False,
                estimated_cost=None,
                now=day + timedelta(hours=1),
            )
            repository.record_model_call(
                "chat",
                "openclaw",
                "main-model",
                True,
                estimated_cost=0.90,
                now=day + timedelta(hours=2),
            )
            repository.record_model_call(
                "image", "compatible", "vision", True, estimated_cost=9.0, now=before
            )
            repository.record_model_call(
                "image", "compatible", "vision", True, estimated_cost=9.0, now=after
            )

            self.assertEqual(
                repository.consumed_model_cost(day.date()),
                Decimal("1.0"),
            )
            self.assertEqual(
                repository.consumed_model_cost(
                    day.date(), provider="compatible"
                ),
                Decimal("0.1"),
            )
            self.assertEqual(
                repository.consumed_model_cost(day.date(), provider="openclaw"),
                Decimal("0.9"),
            )


if __name__ == "__main__":
    unittest.main()
