from pathlib import Path
from datetime import datetime, timezone
import inspect
import sqlite3
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class OrchestrationSchemaTests(unittest.TestCase):
    def _seed_v4(self, path):
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                INSERT INTO metadata(key, value)
                  VALUES ('schema_version', '4');
                CREATE TABLE documents (
                  name TEXT PRIMARY KEY,
                  content TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                INSERT INTO documents(name, content, updated_at)
                  VALUES ('memory', 'legacy-memory',
                          '2026-08-02T00:00:00+00:00');
                """
            )

    def test_schema_v5_preserves_v4_and_creates_orchestration_tables(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.sqlite3"
            self._seed_v4(database_path)
            store = WorkspaceStateStore(database_path)
            with store._connect() as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                content = connection.execute(
                    "SELECT content FROM documents WHERE name='memory'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                action_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(action_intents)"
                    )
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
            self.assertEqual(version, "7")
            self.assertEqual(content, "legacy-memory")
            self.assertTrue(
                {
                    "orchestration_events",
                    "action_intents",
                    "orchestration_runs",
                    "orchestration_config_versions",
                }.issubset(tables)
            )
            self.assertIn("payload_sha256", action_columns)
            self.assertIn("approval_id", action_columns)
            self.assertIn("orchestration_runs_status_started", indexes)
            self.assertIn("outbox_final_reconciliation", indexes)
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(database_path) + suffix)
                if path.exists():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_database_prevents_two_active_orchestration_configs(self):
        from qq_openclaw_bridge.persistence.database import SqliteDatabase

        with tempfile.TemporaryDirectory() as temp_dir:
            database = SqliteDatabase(Path(temp_dir) / "state.sqlite3")
            with database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO orchestration_config_versions(
                      config_json, config_sha256, status, created_at
                    ) VALUES (?, ?, 'active', ?)
                    """,
                    (
                        '{"mode":"legacy_direct"}',
                        "a" * 64,
                        "2026-08-02T00:00:00+00:00",
                    ),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO orchestration_config_versions(
                          config_json, config_sha256, status, created_at
                        ) VALUES (?, ?, 'active', ?)
                        """,
                        (
                            '{"mode":"direct"}',
                            "b" * 64,
                            "2026-08-02T00:00:01+00:00",
                        ),
                    )

    def test_failed_v5_migration_rolls_back_all_new_schema_objects(self):
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
                    INSERT INTO metadata(key, value)
                      VALUES ('schema_version', '4');
                    CREATE TABLE orchestration_config_versions (
                      version INTEGER PRIMARY KEY,
                      config_json TEXT NOT NULL
                    );
                    """
                )
            with self.assertRaises(sqlite3.OperationalError):
                SqliteDatabase(database_path)
            with sqlite3.connect(database_path) as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(version, "4")
            self.assertIn("orchestration_config_versions", tables)
            self.assertNotIn("orchestration_runs", tables)
            self.assertNotIn("orchestration_events", tables)
            self.assertNotIn("action_intents", tables)


class OrchestrationRepositoryTests(unittest.TestCase):
    def setUp(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WorkspaceStateStore(
            Path(self.temp_dir.name) / "state.sqlite3"
        )
        self.repository = self.store.orchestration_repository
        self.now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, message_id="m1", run_id=None):
        return self.repository.create_run(
            user_id=7,
            source_message_id=message_id,
            session_key="agent:main:qq-7",
            mode="direct",
            risk_level="normal",
            config_version=1,
            now=self.now,
            run_id=run_id,
        )

    def test_create_run_appends_initial_event_and_store_reuses_repository(self):
        run = self._run()
        self.assertEqual(run["status"], "received")
        self.assertIs(self.repository, self.store.orchestration_repository)
        self.assertEqual(
            [
                (event["event_type"], event["stage"])
                for event in self.repository.events(run["run_id"])
            ],
            [("received", "received")],
        )

    def test_transition_is_compare_and_swap_and_terminal_is_irreversible(self):
        run = self._run()
        with self.assertRaises(ValueError):
            self.repository.transition_run(
                run["run_id"], "received", "executing", "executing"
            )
        planned = self.repository.transition_run(
            run["run_id"], "received", "planned", "planned", now=self.now
        )
        self.assertEqual(planned["status"], "planned")
        with self.assertRaises(ValueError):
            self.repository.transition_run(
                run["run_id"], "received", "executing", "executing"
            )
        failed = self.repository.transition_run(
            run["run_id"], "planned", "failed", "failed", now=self.now
        )
        self.assertIsNotNone(failed["finished_at"])
        with self.assertRaises(ValueError):
            self.repository.transition_run(
                run["run_id"], "failed", "planned", "planned"
            )

    def test_event_failure_rolls_back_status_update(self):
        run = self._run()
        with self.store._connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_exploding_orchestration_event
                BEFORE INSERT ON orchestration_events
                WHEN NEW.event_type='explode'
                BEGIN
                  SELECT RAISE(ABORT, 'event rejected');
                END
                """
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.repository.transition_run(
                run["run_id"], "received", "planned", "explode"
            )
        self.assertEqual(
            self.repository.get_run(run["run_id"])["status"], "received"
        )
        self.assertEqual(len(self.repository.events(run["run_id"])), 1)

    def test_run_and_event_query_limits_are_bounded(self):
        first = self._run("m1", run_id="zzzz")
        second = self._run("m2", run_id="aaaa")
        self.repository.append_event(first["run_id"], "note", "received")
        self.repository.append_event(first["run_id"], "note", "received")
        self.assertEqual(len(self.repository.list_runs(limit=True)), 1)
        self.assertEqual(len(self.repository.events(first["run_id"], limit=0)), 1)
        self.assertEqual(
            len(self.repository.events(first["run_id"], limit=999)), 3
        )
        self.assertEqual(self.repository.latest_run(7)["run_id"], second["run_id"])
        self.assertEqual(
            self.repository.list_runs(limit=2)[0]["run_id"], second["run_id"]
        )

    def test_complete_delivery_is_owned_atomic_and_idempotent(self):
        import hashlib

        message_id = "m-delivery"
        source_hash = "sha256:%s" % hashlib.sha256(
            message_id.encode("utf-8")
        ).hexdigest()
        run = self.repository.create_run(
            user_id=7,
            source_message_id=source_hash,
            session_key="agent:main:qq-7",
            mode="direct",
            risk_level="normal",
            config_version=1,
            now=self.now,
        )
        current = "received"
        for target in ("planned", "executing", "verifying", "accepted"):
            self.repository.transition_run(
                run["run_id"], current, target, target, now=self.now
            )
            current = target

        self.assertFalse(self.repository.complete_delivery(8, message_id, self.now))
        self.assertFalse(self.repository.complete_delivery(7, "other", self.now))
        self.assertEqual(
            self.repository.get_run(run["run_id"])["status"], "accepted"
        )

        self.assertTrue(self.repository.complete_delivery(7, message_id, self.now))
        self.assertFalse(self.repository.complete_delivery(7, message_id, self.now))
        delivered = self.repository.get_run(run["run_id"])
        self.assertEqual(delivered["status"], "delivered")
        self.assertIsNotNone(delivered["finished_at"])
        self.assertEqual(
            [event["stage"] for event in self.repository.events(run["run_id"])][-1],
            "delivered",
        )

    def test_reconcile_deliveries_repairs_sent_outbox_after_callback_loss(self):
        import hashlib

        missing_hash = "sha256:%s" % hashlib.sha256(
            b"m-missing"
        ).hexdigest()
        missing = self.repository.create_run(
            user_id=7,
            source_message_id=missing_hash,
            session_key="agent:main:qq-7",
            mode="direct",
            risk_level="normal",
            config_version=1,
            now=self.now,
            run_id="run-1",
        )
        current = "received"
        for target in ("planned", "executing", "verifying", "accepted"):
            self.repository.transition_run(
                missing["run_id"], current, target, target, now=self.now
            )
            current = target

        message_id = "m-reconcile"
        source_hash = "sha256:%s" % hashlib.sha256(
            message_id.encode("utf-8")
        ).hexdigest()
        run = self.repository.create_run(
            user_id=7,
            source_message_id=source_hash,
            session_key="agent:main:qq-7",
            mode="direct",
            risk_level="normal",
            config_version=1,
            now=self.now,
            run_id="run-2",
        )
        current = "received"
        for target in ("planned", "executing", "verifying", "accepted"):
            self.repository.transition_run(
                run["run_id"], current, target, target, now=self.now
            )
            current = target
        timestamp = self.now.isoformat()
        with self.store._connect() as connection:
            connection.execute(
                """
                INSERT INTO outbox(
                  kind, user_id, message, echo, dedupe_key, status,
                  attempts, next_attempt_at, created_at, updated_at, sent_at
                ) VALUES (
                  'reply', 7, 'sent reply', 'reply:m-reconcile',
                  'reply:m-reconcile', 'sent', 1, ?, ?, ?, ?
                )
                """,
                (timestamp, timestamp, timestamp, timestamp),
            )

        self.assertEqual(self.repository.reconcile_deliveries(limit=1), 0)
        self.assertEqual(self.repository.reconcile_deliveries(limit=1), 1)
        self.assertEqual(self.repository.reconcile_deliveries(limit=1), 0)
        self.assertEqual(
            self.repository.get_run(run["run_id"])["status"], "delivered"
        )
        self.assertEqual(
            self.repository.get_run(missing["run_id"])["status"], "accepted"
        )

    def test_reconciliation_cursor_matches_index_without_temporary_sort(self):
        source = inspect.getsource(type(self.repository).reconcile_deliveries)
        self.assertIn("(started_at, run_id) > (?, ?)", source)
        with self.store._connect() as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT run_id, user_id, source_message_id, started_at "
                "FROM orchestration_runs "
                "WHERE status='accepted' AND (started_at, run_id) > (?, ?) "
                "ORDER BY started_at ASC, run_id ASC LIMIT ?",
                ("", "", 50),
            ).fetchall()
        detail = " ".join(str(row[3]) for row in plan)
        self.assertIn("orchestration_runs_status_started", detail)
        self.assertNotIn("TEMP B-TREE", detail)

    def test_config_versions_are_canonical_deduplicated_and_atomically_activated(self):
        legacy = self.repository.create_config_version(
            {"mode": "legacy_direct"}, activate=True, now=self.now
        )
        direct = self.repository.create_config_version(
            {"mode": "direct"}, activate=True, now=self.now
        )
        self.assertEqual(self.repository.active_config()["version"], direct["version"])
        self.assertEqual(
            self.repository.get_config(legacy["version"])["status"], "retired"
        )
        self.assertEqual(legacy["config_json"], '{"mode":"legacy_direct"}')
        self.assertEqual(
            legacy["config_sha256"],
            "66a27e701337ca8fb4a2123769094e7ae00275b9ed02f403d9b546507f3fd17b",
        )
        same = self.repository.create_config_version(
            {"mode": "legacy_direct"}, activate=False, now=self.now
        )
        self.assertEqual(same["version"], legacy["version"])
        reactivated = self.repository.create_config_version(
            {"mode": "legacy_direct"}, activate=True, now=self.now
        )
        self.assertEqual(reactivated["status"], "active")
        self.assertEqual(
            self.repository.get_config(direct["version"])["status"], "retired"
        )

    def test_config_versions_reject_unknown_sensitive_and_malformed_inputs(self):
        class ConfigSubclass(dict):
            pass

        invalid = (
            {},
            {"mode": "shadow"},
            {"mode": "direct", "extra": True},
            {"OPENAI_API_KEY": "secret"},
            ConfigSubclass(mode="direct"),
        )
        for config in invalid:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    self.repository.create_config_version(config, activate=False)


class SqliteOrchestrationTraceSinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WorkspaceStateStore(
            Path(self.temp_dir.name) / "state.sqlite3"
        )
        self.repository = self.store.orchestration_repository

    def tearDown(self):
        self.temp_dir.cleanup()

    def _contract(
        self,
        text="不要写入数据库的原始消息",
        message_id="m1",
        session_key=None,
    ):
        from qq_openclaw_bridge.models import AgentRequest
        from qq_openclaw_bridge.orchestration.models import TaskContract

        return TaskContract.from_request(
            AgentRequest(7, message_id, text, 10, session_key=session_key)
        )

    async def test_runtime_persists_bounded_stage_sequence_without_message_text(self):
        from qq_openclaw_bridge.application.coordinator_factory import create_direct_runtime
        from qq_openclaw_bridge.models import AgentResponse
        from qq_openclaw_bridge.persistence.orchestration import SqliteOrchestrationTraceSink

        async def reply(request):
            return AgentResponse("不要写入数据库的模型回复")

        runtime = create_direct_runtime(
            reply, trace_sink=SqliteOrchestrationTraceSink(self.repository)
        )
        result = await runtime.execute(self._contract())
        run = self.repository.latest_run(7)
        self.assertEqual(result.text, "不要写入数据库的模型回复")
        self.assertEqual(run["status"], "accepted")
        events = self.repository.events(run["run_id"])
        self.assertEqual(
            [event["stage"] for event in events],
            ["received", "planned", "executing", "verifying", "accepted"],
        )
        persisted = repr(run) + repr(events)
        self.assertNotIn("原始消息", persisted)
        self.assertNotIn("模型回复", persisted)
        self.assertEqual(run["session_key"], "agent:main:qq-7")

    async def test_local_command_creates_no_orchestration_run(self):
        from qq_openclaw_bridge.application.coordinator import MessageCoordinator
        from qq_openclaw_bridge.application.coordinator_factory import create_direct_runtime
        from qq_openclaw_bridge.models import AgentResponse
        from qq_openclaw_bridge.persistence.orchestration import SqliteOrchestrationTraceSink

        class LocalCommand:
            def handle(self, request):
                return "本地回复"

        async def reply(request):
            return AgentResponse("不应调用")

        coordinator = MessageCoordinator(
            command_chain=LocalCommand(),
            fallback_mapper=lambda exc: "fallback",
            truncator=lambda text: text,
            orchestration_runtime=create_direct_runtime(
                reply,
                trace_sink=SqliteOrchestrationTraceSink(self.repository),
            ),
        )
        resolution = await coordinator.resolve_prepared(
            coordinator.prepare(self._contract().request)
        )
        self.assertEqual(resolution.reply, "本地回复")
        self.assertEqual(self.repository.list_runs(limit=10), [])

    def test_failure_detail_is_reduced_to_safe_error_class(self):
        from qq_openclaw_bridge.persistence.orchestration import SqliteOrchestrationTraceSink

        sink = SqliteOrchestrationTraceSink(self.repository)
        run_id = sink.start(self._contract(), "direct", 1)
        sink.transition(run_id, "planned")
        sink.finish(run_id, "failed", "../../secret prompt=hello")
        run = self.repository.get_run(run_id)
        event = self.repository.events(run_id)[-1]
        self.assertEqual(run["safe_error_class"], "Error")
        self.assertEqual(event["detail"], "Error")
        self.assertNotIn("secret", repr(run) + repr(event))

    def test_user_controlled_identifiers_cannot_persist_paths_urls_or_text(self):
        from qq_openclaw_bridge.persistence.orchestration import SqliteOrchestrationTraceSink

        contract = self._contract(
            "summarize-private-data", message_id="prompt", session_key="prompt"
        )
        run_id = SqliteOrchestrationTraceSink(self.repository).start(
            contract, "direct", 1
        )
        run = self.repository.get_run(run_id)
        self.assertRegex(run["source_message_id"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(run["session_key"], "agent:main:qq-7")
        self.assertNotIn("private-data", repr(run))
        self.assertNotIn("prompt", repr(run))

    async def test_transient_intermediate_write_failure_catches_up_to_accepted(self):
        from qq_openclaw_bridge.application.coordinator_factory import create_direct_runtime
        from qq_openclaw_bridge.models import AgentResponse
        from qq_openclaw_bridge.persistence.orchestration import SqliteOrchestrationTraceSink

        repository = self.repository

        class FailsExecutingOnce:
            def __init__(self):
                self.failed = False

            def __getattr__(self, name):
                return getattr(repository, name)

            def transition_run(
                self, run_id, expected, target, event_type, detail="", now=None
            ):
                if target == "executing" and not self.failed:
                    self.failed = True
                    raise OSError("temporary database failure")
                return repository.transition_run(
                    run_id, expected, target, event_type, detail, now
                )

        async def reply(request):
            return AgentResponse("正常回复")

        runtime = create_direct_runtime(
            reply, trace_sink=SqliteOrchestrationTraceSink(FailsExecutingOnce())
        )
        result = await runtime.execute(self._contract())
        run = self.repository.latest_run(7)
        self.assertEqual(result.text, "正常回复")
        self.assertEqual(run["status"], "accepted")
        self.assertEqual(
            [event["stage"] for event in self.repository.events(run["run_id"])],
            ["received", "planned", "executing", "verifying", "accepted"],
        )

    def test_real_sink_persists_fixed_rejection_reasons(self):
        from qq_openclaw_bridge.persistence.orchestration import SqliteOrchestrationTraceSink

        sink = SqliteOrchestrationTraceSink(self.repository)
        budget_run = sink.start(self._contract(message_id="m-budget"), "direct", 1)
        sink.finish(budget_run, "rejected", "budget_rejected")
        verify_run = sink.start(self._contract(message_id="m-verify"), "direct", 1)
        sink.transition(verify_run, "planned")
        sink.transition(verify_run, "executing")
        sink.transition(verify_run, "verifying")
        sink.finish(verify_run, "rejected", "private verifier message")
        self.assertEqual(
            self.repository.get_run(budget_run)["fallback_reason"],
            "budget_rejected",
        )
        self.assertEqual(
            self.repository.get_run(verify_run)["fallback_reason"],
            "verification_rejected",
        )
        self.assertEqual(
            self.repository.events(verify_run)[-1]["detail"],
            "verification_rejected",
        )


if __name__ == "__main__":
    unittest.main()
