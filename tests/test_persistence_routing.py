from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class RoutingRepositoryTests(unittest.TestCase):
    def setUp(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "state.sqlite3"
        self.store = WorkspaceStateStore(self.path)
        self.repository = self.store.routing_repository
        self.now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    def pending_values(self, source="a"):
        return {
            "user_id": 363129743,
            "source_message_sha256": source * 64,
            "message_sha256": "b" * 64,
            "message_preview": "route this message",
            "routing_config_version": 1,
            "rule_version": "rules-v1",
            "dispatch_overhead_ms": 14,
            "now": self.now,
        }

    def finish(self, decision, **overrides):
        values = {
            "status": "classified",
            "rule_level": None,
            "model_level": "L2",
            "final_level": "L2",
            "confidence": 0.75,
            "task_kind": "analysis",
            "risk_flags": (),
            "reasons": ("complex", "needs_context"),
            "brainstorm_required": False,
            "brainstorm_mode": "standard",
            "classifier_provider": "openai",
            "classifier_model": "gpt-5-mini",
            "classifier_duration_ms": 40,
            "now": self.now + timedelta(seconds=1),
        }
        values.update(overrides)
        return self.repository.finish_decision(
            decision["id"], decision["updated_at"], **values
        )

    def test_schema_v6_creates_routing_tables_and_one_active_config(self):
        with sqlite3.connect(str(self.path)) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            indexes = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )}
        self.assertEqual(version, "7")
        self.assertTrue(
            {"route_decisions", "route_labels", "routing_config_versions"}
            <= tables
        )
        self.assertTrue(
            {
                "routing_one_active_config",
                "route_decisions_owner_created",
                "route_decisions_status_created",
                "route_labels_decision_owner",
            }
            <= indexes
        )
        with self.store._connect() as connection:
            decision_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(route_decisions)"
                )
            }
        self.assertIn("classifier_attempted", decision_columns)
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO routing_config_versions("
                "config_json, config_sha256, status, created_at"
                ") VALUES (?, ?, 'active', ?)",
                ("{}", "a" * 64, self.now.isoformat()),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO routing_config_versions("
                    "config_json, config_sha256, status, created_at"
                    ") VALUES (?, ?, 'active', ?)",
                    ("{\"x\":1}", "b" * 64, self.now.isoformat()),
                )
            connection.execute(
                "UPDATE routing_config_versions SET status='retired' WHERE version=1"
            )
            connection.execute(
                "INSERT INTO routing_config_versions("
                "config_json, config_sha256, status, created_at"
                ") VALUES (?, ?, 'draft', ?)",
                ("{}", "a" * 64, self.now.isoformat()),
            )

    def test_existing_v6_unique_hash_migration_rehashes_versions_and_preserves_references(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            config = {
                "enabled": True,
                "classifier_enabled": True,
                "classifier_model": "gpt-5-mini",
                "confidence_threshold_percent": 75,
                "classifier_timeout_seconds": 20,
                "max_daily_calls": 300,
                "failure_threshold": 3,
                "cooldown_seconds": 60,
                "rule_version": "rules-v1",
            }
            canonical = json.dumps(
                config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            with sqlite3.connect(str(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO metadata VALUES ('schema_version', '6');
                    CREATE TABLE routing_config_versions (
                      version INTEGER PRIMARY KEY AUTOINCREMENT,
                      config_json TEXT NOT NULL,
                      config_sha256 TEXT NOT NULL UNIQUE,
                      status TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      activated_at TEXT
                    );
                    CREATE UNIQUE INDEX routing_one_active_config
                      ON routing_config_versions(status) WHERE status='active';
                    CREATE TABLE route_decisions (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER NOT NULL,
                      source_message_sha256 TEXT NOT NULL,
                      message_sha256 TEXT NOT NULL,
                      message_preview TEXT NOT NULL,
                      routing_config_version INTEGER NOT NULL,
                      rule_version TEXT NOT NULL,
                      dispatch_overhead_ms INTEGER NOT NULL,
                      status TEXT NOT NULL,
                      risk_flags_json TEXT NOT NULL,
                      reasons_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
                    CREATE TABLE route_labels (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      decision_id INTEGER NOT NULL,
                      user_id INTEGER NOT NULL,
                      verdict TEXT NOT NULL,
                      correct_level TEXT NOT NULL,
                      note TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
                    """
                )
                for version, stored_hash, status in (
                    (1, digest, "retired"),
                    (
                        2,
                        hashlib.sha256((canonical + ":2").encode("utf-8")).hexdigest(),
                        "retired",
                    ),
                    (
                        3,
                        hashlib.sha256((canonical + ":3").encode("utf-8")).hexdigest(),
                        "active",
                    ),
                ):
                    connection.execute(
                        "INSERT INTO routing_config_versions("
                        "version, config_json, config_sha256, status, created_at, activated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            version,
                            canonical,
                            stored_hash,
                            status,
                            self.now.isoformat(),
                            self.now.isoformat(),
                        ),
                    )
                connection.execute(
                    "INSERT INTO route_decisions("
                    "id, user_id, source_message_sha256, message_sha256, message_preview, "
                    "routing_config_version, rule_version, dispatch_overhead_ms, status, "
                    "risk_flags_json, reasons_json, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        10,
                        10001,
                        "a" * 64,
                        "b" * 64,
                        "safe",
                        2,
                        "rules-v1",
                        1,
                        "rule_only",
                        "[]",
                        "[]",
                        self.now.isoformat(),
                        self.now.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO route_labels("
                    "id, decision_id, user_id, verdict, correct_level, note, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (20, 10, 10001, "correct", "L2", "", self.now.isoformat(), self.now.isoformat()),
                )

            store = WorkspaceStateStore(path)
            with store._connect() as connection:
                migrated = connection.execute(
                    "SELECT version, config_json, config_sha256, status, created_at, activated_at "
                    "FROM routing_config_versions ORDER BY version"
                ).fetchall()
                decision = connection.execute(
                    "SELECT id, routing_config_version FROM route_decisions WHERE id=10"
                ).fetchone()
                label = connection.execute(
                    "SELECT id, decision_id FROM route_labels WHERE id=20"
                ).fetchone()
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                hash_migration = connection.execute(
                    "SELECT value FROM metadata "
                    "WHERE key='routing_config_hash_compat_v1'"
                ).fetchone()
                circuits = connection.execute(
                    "SELECT config_version, state, opened_at "
                    "FROM routing_classifier_circuit ORDER BY config_version"
                ).fetchall()
            self.assertEqual([row[0] for row in migrated], [1, 2, 3])
            self.assertEqual([row[1] for row in migrated], [canonical, canonical, canonical])
            self.assertEqual([row[3] for row in migrated], ["retired", "retired", "active"])
            self.assertTrue(
                all(
                    row[2] == hashlib.sha256(row[1].encode("utf-8")).hexdigest()
                    for row in migrated
                )
            )
            self.assertEqual(decision, (10, 2))
            self.assertEqual(label, (20, 10))
            self.assertEqual(version, "7")
            self.assertEqual(hash_migration, ("1",))
            self.assertEqual(
                circuits,
                [(1, "closed", None), (2, "closed", None), (3, "closed", None)],
            )

            appended = store.routing_repository.create_config(
                config, activate=True, now=self.now, force_new=True
            )
            self.assertEqual(appended["version"], 4)
            self.assertEqual(appended["config_sha256"], digest)
            self.assertEqual(
                store.routing_repository.classifier_circuit(appended["version"])["state"],
                "closed",
            )

    def test_existing_nonunique_v6_rehashes_salted_versions_in_place(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.sqlite3"
            config = {
                "enabled": False,
                "classifier_enabled": True,
                "classifier_model": "gpt-5-mini",
                "confidence_threshold_percent": 75,
                "classifier_timeout_seconds": 20,
                "max_daily_calls": 300,
                "failure_threshold": 3,
                "cooldown_seconds": 60,
                "rule_version": "rules-v1",
            }
            canonical = json.dumps(
                config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            with sqlite3.connect(str(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO metadata VALUES ('schema_version', '6');
                    CREATE TABLE routing_config_versions (
                      version INTEGER PRIMARY KEY AUTOINCREMENT,
                      config_json TEXT NOT NULL,
                      config_sha256 TEXT NOT NULL,
                      status TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      activated_at TEXT
                    );
                    CREATE INDEX routing_config_sha256
                      ON routing_config_versions(config_sha256);
                    CREATE UNIQUE INDEX routing_one_active_config
                      ON routing_config_versions(status) WHERE status='active';
                    CREATE TABLE route_decisions (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER NOT NULL,
                      source_message_sha256 TEXT NOT NULL,
                      message_sha256 TEXT NOT NULL,
                      message_preview TEXT NOT NULL,
                      routing_config_version INTEGER NOT NULL,
                      rule_version TEXT NOT NULL,
                      dispatch_overhead_ms INTEGER NOT NULL,
                      status TEXT NOT NULL,
                      risk_flags_json TEXT NOT NULL,
                      reasons_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
                    CREATE TABLE route_labels (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      decision_id INTEGER NOT NULL,
                      user_id INTEGER NOT NULL,
                      verdict TEXT NOT NULL,
                      correct_level TEXT NOT NULL,
                      note TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
                    """
                )
                for version, stored_hash, status in (
                    (1, digest, "retired"),
                    (
                        2,
                        hashlib.sha256((canonical + ":2").encode("utf-8")).hexdigest(),
                        "retired",
                    ),
                    (
                        3,
                        hashlib.sha256((canonical + ":3").encode("utf-8")).hexdigest(),
                        "active",
                    ),
                ):
                    connection.execute(
                        "INSERT INTO routing_config_versions("
                        "version, config_json, config_sha256, status, created_at, activated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            version,
                            canonical,
                            stored_hash,
                            status,
                            self.now.isoformat(),
                            self.now.isoformat(),
                        ),
                    )
                connection.execute(
                    "INSERT INTO route_decisions("
                    "id, user_id, source_message_sha256, message_sha256, message_preview, "
                    "routing_config_version, rule_version, dispatch_overhead_ms, status, "
                    "risk_flags_json, reasons_json, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        10,
                        10001,
                        "a" * 64,
                        "b" * 64,
                        "safe",
                        2,
                        "rules-v1",
                        1,
                        "rule_only",
                        "[]",
                        "[]",
                        self.now.isoformat(),
                        self.now.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO route_labels("
                    "id, decision_id, user_id, verdict, correct_level, note, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (20, 10, 10001, "correct", "L2", "", self.now.isoformat(), self.now.isoformat()),
                )

            store = WorkspaceStateStore(path)
            with store._connect() as connection:
                rows = connection.execute(
                    "SELECT version, config_json, config_sha256, status, created_at, activated_at "
                    "FROM routing_config_versions ORDER BY version"
                ).fetchall()
                decision = connection.execute(
                    "SELECT id, routing_config_version FROM route_decisions WHERE id=10"
                ).fetchone()
                label = connection.execute(
                    "SELECT id, decision_id FROM route_labels WHERE id=20"
                ).fetchone()
                schema_version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                hash_migration = connection.execute(
                    "SELECT value FROM metadata "
                    "WHERE key='routing_config_hash_compat_v1'"
                ).fetchone()
                circuits = connection.execute(
                    "SELECT config_version, state, opened_at "
                    "FROM routing_classifier_circuit ORDER BY config_version"
                ).fetchall()
            self.assertEqual([row[0] for row in rows], [1, 2, 3])
            self.assertEqual([row[1] for row in rows], [canonical, canonical, canonical])
            self.assertEqual([row[3] for row in rows], ["retired", "retired", "active"])
            self.assertTrue(
                all(
                    row[2] == hashlib.sha256(row[1].encode("utf-8")).hexdigest()
                    for row in rows
                )
            )
            self.assertEqual(decision, (10, 2))
            self.assertEqual(label, (20, 10))
            self.assertEqual(schema_version, "7")
            self.assertEqual(hash_migration, ("1",))
            self.assertEqual(
                circuits,
                [(1, "closed", None), (2, "closed", None), (3, "closed", None)],
            )

            appended = store.routing_repository.create_config(
                config, activate=True, now=self.now, force_new=True
            )
            self.assertEqual(appended["version"], 4)
            self.assertEqual(
                store.routing_repository.classifier_circuit(appended["version"])["state"],
                "closed",
            )
            self.assertEqual(appended["config_sha256"], digest)

    def test_classifier_circuit_is_persisted_for_each_routing_config(self):
        config = {
            "enabled": True,
            "classifier_enabled": True,
            "classifier_model": "gpt-5-mini",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 20,
            "max_daily_calls": 300,
            "failure_threshold": 3,
            "cooldown_seconds": 60,
            "rule_version": "rules-v1",
        }

        active = self.repository.create_config(config, activate=True, now=self.now)

        self.assertEqual(
            self.repository.classifier_circuit(active["version"]),
            {
                "state": "closed",
                "opened_at": None,
                "updated_at": self.now.isoformat(timespec="microseconds"),
            },
        )
        cooling = self.repository.record_classifier_circuit(
            active["version"], "cooling", now=self.now + timedelta(seconds=1)
        )

        self.assertEqual(cooling["state"], "cooling")
        self.assertEqual(cooling["opened_at"], cooling["updated_at"])

    def test_v6_migration_backfills_closed_circuits_for_existing_configs(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        first = self.repository.create_config(
            {
                "enabled": True,
                "classifier_enabled": True,
                "classifier_model": "gpt-5-mini",
                "confidence_threshold_percent": 75,
                "classifier_timeout_seconds": 20,
                "max_daily_calls": 300,
                "failure_threshold": 3,
                "cooldown_seconds": 60,
                "rule_version": "rules-v1",
            },
            activate=True,
            now=self.now,
        )
        second = self.repository.create_config(
            {
                "enabled": True,
                "classifier_enabled": True,
                "classifier_model": "gpt-5-mini",
                "confidence_threshold_percent": 75,
                "classifier_timeout_seconds": 20,
                "max_daily_calls": 300,
                "failure_threshold": 3,
                "cooldown_seconds": 60,
                "rule_version": "rules-v2",
            },
            now=self.now + timedelta(seconds=1),
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE routing_classifier_circuit")

        upgraded = WorkspaceStateStore(self.path).routing_repository

        self.assertEqual(upgraded.classifier_circuit(first["version"])["state"], "closed")
        self.assertEqual(upgraded.classifier_circuit(second["version"])["state"], "closed")

    def test_config_versions_are_canonical_deduplicated_and_activated(self):
        config = {
            "enabled": True,
            "classifier_enabled": True,
            "classifier_model": "gpt-5-mini",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 20,
            "max_daily_calls": 300,
            "failure_threshold": 3,
            "cooldown_seconds": 60,
            "rule_version": "rules-v1",
        }
        first = self.repository.create_config(config, activate=True, now=self.now)
        duplicate = self.repository.create_config(
            dict(reversed(tuple(config.items()))), activate=False, now=self.now
        )
        self.assertEqual(first["version"], duplicate["version"])
        self.assertEqual(first["config"], config)
        self.assertEqual(
            first["config_json"],
            '{"classifier_enabled":true,"classifier_model":"gpt-5-mini",'
            '"classifier_timeout_seconds":20,"confidence_threshold_percent":75,'
            '"cooldown_seconds":60,"enabled":true,"failure_threshold":3,'
            '"max_daily_calls":300,"rule_version":"rules-v1"}',
        )
        forced = self.repository.create_config(
            config, activate=True, now=self.now, force_new=True
        )
        self.assertGreater(forced["version"], first["version"])
        self.assertEqual(
            forced["config_sha256"],
            hashlib.sha256(forced["config_json"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            self.repository.create_config(config, activate=False, now=self.now)["version"],
            forced["version"],
        )
        second = self.repository.create_config(
            dict(config, classifier_model="gpt-5"), activate=False, now=self.now
        )
        active = self.repository.activate_config(second["version"], now=self.now)
        self.assertEqual(active["version"], second["version"])
        self.assertEqual(self.repository.active_config()["version"], second["version"])
        self.assertEqual(self.repository.get_config(first["version"])["status"], "retired")
        rollback = self.repository.create_config(
            first["config"], activate=True, now=self.now, force_new=True
        )
        self.assertEqual(self.repository.active_config()["version"], rollback["version"])
        for version in (first["version"], forced["version"], second["version"], rollback["version"]):
            with self.subTest(version=version):
                self.assertEqual(
                    self.repository.classifier_circuit(version)["state"], "closed"
                )
        with self.assertRaises(ValueError):
            self.repository.create_config(
                dict(config, confidence_threshold_percent=49), activate=False
            )
        with self.assertRaises(ValueError):
            self.repository.create_config(
                dict(config, classifier_model="../secret"), activate=False
            )

    def test_append_active_config_preserves_enabled_across_repository_race(self):
        from qq_openclaw_bridge.persistence.routing import RoutingRepository

        base = {
            "enabled": True,
            "classifier_enabled": True,
            "classifier_model": "gpt-5-mini",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 20,
            "max_daily_calls": 300,
            "failure_threshold": 3,
            "cooldown_seconds": 60,
            "rule_version": "rules-v1",
        }
        target = self.repository.create_config(
            dict(base, enabled=False), activate=False, now=self.now
        )
        self.repository.create_config(base, activate=True, now=self.now)
        second_repository = RoutingRepository(self.store._database)
        barrier = threading.Barrier(2)
        failures = []

        def update():
            try:
                barrier.wait()
                self.repository.append_active_config(
                    {
                        "classifier_enabled": True,
                        "classifier_model": "gpt-5-mini",
                        "confidence_threshold_percent": 80,
                        "classifier_timeout_seconds": 20,
                        "max_daily_calls": 300,
                        "failure_threshold": 3,
                        "cooldown_seconds": 60,
                        "rule_version": "rules-v2",
                    },
                    now=self.now,
                )
            except BaseException as exc:
                failures.append(exc)

        def rollback():
            try:
                barrier.wait()
                second_repository.create_config(
                    target["config"], activate=True, now=self.now, force_new=True
                )
            except BaseException as exc:
                failures.append(exc)

        workers = [threading.Thread(target=update), threading.Thread(target=rollback)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertFalse(self.repository.active_config()["config"]["enabled"])

    def test_decision_is_idempotent_per_owner_source_and_config(self):
        first = self.repository.start_decision(**self.pending_values())
        second = self.repository.start_decision(**self.pending_values())
        self.assertEqual(first["id"], second["id"])
        other_config = self.repository.start_decision(
            **dict(self.pending_values(), routing_config_version=2)
        )
        self.assertNotEqual(first["id"], other_config["id"])
        self.assertEqual(first["status"], "pending")

    def test_classifier_attempt_reservation_is_atomic_and_persists_daily_budget(self):
        from qq_openclaw_bridge.persistence.routing import RoutingRepository

        decision = self.repository.start_decision(**self.pending_values())
        second_repository = RoutingRepository(self.store._database)
        barrier = threading.Barrier(2)
        outcomes = []

        def reserve(repository):
            barrier.wait()
            outcomes.append(
                repository.reserve_classifier_attempt(
                    decision["id"],
                    decision["updated_at"],
                    max_daily_calls=1,
                    now=self.now,
                )
            )

        first_thread = threading.Thread(target=reserve, args=(self.repository,))
        second_thread = threading.Thread(target=reserve, args=(second_repository,))
        first_thread.start()
        second_thread.start()
        first_thread.join()
        second_thread.join()

        self.assertEqual(
            sorted(result["outcome"] for result in outcomes),
            ["duplicate_or_terminal", "reserved"],
        )
        reserved = next(
            result["decision"]
            for result in outcomes
            if result["outcome"] == "reserved"
        )
        self.assertTrue(reserved["classifier_attempted"])
        self.assertIsNotNone(reserved["classifier_attempted_at"])

        after_restart = RoutingRepository(self.store._database)
        other = after_restart.start_decision(**self.pending_values("c"))
        exhausted = after_restart.reserve_classifier_attempt(
            other["id"], other["updated_at"], max_daily_calls=1, now=self.now
        )
        self.assertEqual(exhausted["outcome"], "budget_exhausted")

    def test_recovery_keeps_reserved_attempt_in_daily_budget(self):
        decision = self.repository.start_decision(**self.pending_values())
        reserved = self.repository.reserve_classifier_attempt(
            decision["id"], decision["updated_at"], max_daily_calls=1, now=self.now
        )
        self.assertEqual(self.repository.recover_pending_as_interrupted(), 1)

        other = self.repository.start_decision(**self.pending_values("d"))
        outcome = self.repository.reserve_classifier_attempt(
            other["id"], other["updated_at"], max_daily_calls=1, now=self.now
        )

        self.assertEqual(reserved["outcome"], "reserved")
        self.assertEqual(outcome["outcome"], "budget_exhausted")

    def test_finishing_cannot_clear_a_reserved_classifier_attempt(self):
        decision = self.repository.start_decision(**self.pending_values())
        reserved = self.repository.reserve_classifier_attempt(
            decision["id"], decision["updated_at"], max_daily_calls=2, now=self.now
        )["decision"]
        finished = self.finish(
            reserved,
            status="rule_only",
            rule_level="L2",
            model_level=None,
            final_level="L2",
            confidence=0.0,
            task_kind="other",
            classifier_provider=None,
            classifier_model=None,
            classifier_duration_ms=None,
        )

        self.assertTrue(finished["classifier_attempted"])
        self.assertEqual(
            finished["classifier_attempted_at"], reserved["classifier_attempted_at"]
        )

    def test_finish_is_pending_only_cas_and_exposes_canonical_tuples(self):
        decision = self.repository.start_decision(**self.pending_values())
        finished = self.finish(
            decision, risk_flags=("credentials",), reasons=("first", "second")
        )
        self.assertEqual(finished["status"], "classified")
        self.assertEqual(finished["risk_flags"], ("credentials",))
        self.assertEqual(finished["reasons"], ("first", "second"))
        with self.assertRaises(ValueError):
            self.finish(finished)
        second = self.repository.start_decision(**self.pending_values("c"))
        with self.assertRaises(ValueError):
            self.repository.finish_decision(
                second["id"], "stale", status="classified", rule_level=None,
                model_level="L2", final_level="L2", confidence=0.5,
                task_kind="analysis", risk_flags=(), reasons=(),
                brainstorm_required=False, brainstorm_mode="standard",
                classifier_provider="openai", classifier_model="gpt-5-mini",
                classifier_duration_ms=10,
            )
        with self.assertRaises(ValueError):
            self.repository.finish_decision(
                second["id"], second["updated_at"], status="interrupted",
                rule_level=None, model_level=None, final_level="L2", confidence=0,
                task_kind="other", risk_flags=(), reasons=(),
                brainstorm_required=False, brainstorm_mode="standard",
                classifier_provider=None, classifier_model=None,
                classifier_duration_ms=None,
            )

    def test_fail_and_recovery_use_terminal_interrupted_state(self):
        pending = self.repository.start_decision(**self.pending_values())
        errored = self.repository.fail_decision(
            pending["id"], pending["updated_at"], "TimeoutError", now=self.now
        )
        self.assertEqual(errored["status"], "error")
        self.assertEqual(errored["safe_error_class"], "TimeoutError")
        waiting = self.repository.start_decision(**self.pending_values("1"))
        self.assertEqual(self.repository.recover_pending_as_interrupted(), 1)
        self.assertEqual(
            self.repository.recent_decisions(10)[0 if waiting["id"] > errored["id"] else 1]["status"],
            "interrupted",
        )

    def test_label_uses_owner_and_compare_and_swap(self):
        decision = self.repository.start_decision(**self.pending_values())
        original_updated_at = decision["updated_at"]
        with self.assertRaises(ValueError):
            self.repository.label(
                decision["id"], 363129743, "correct", "L2", "", original_updated_at
            )
        unchanged = self.repository.recent_decisions(1)[0]
        self.assertEqual(unchanged["status"], "pending")
        self.assertEqual(unchanged["updated_at"], original_updated_at)
        decision = self.finish(decision)
        label = self.repository.label(
            decision["id"], 363129743, "correct", "L2", "", decision["updated_at"]
        )
        self.assertEqual(label["verdict"], "correct")
        with self.assertRaises(ValueError):
            self.repository.label(
                decision["id"], 363129743, "upgrade", "L3", "", decision["updated_at"]
            )
        with self.assertRaises(ValueError):
            self.repository.label(
                decision["id"], 99, "correct", "L2", "", label["updated_at"]
            )

    def test_metrics_uses_one_snapshot_when_label_commits_mid_read(self):
        from qq_openclaw_bridge.persistence.routing import RoutingRepository

        decision = self.finish(
            self.repository.start_decision(**self.pending_values("6"))
        )

        class SnapshotReader(RoutingRepository):
            def __init__(self, database, writer, pending_label):
                super().__init__(database)
                self._writer = writer
                self._pending_label = pending_label

            def _load_metric_labels(self, connection):
                self._writer.label(*self._pending_label)
                return super()._load_metric_labels(connection)

        reader = SnapshotReader(
            self.store._database,
            self.repository,
            (
                decision["id"], 363129743, "correct", "L2", "",
                decision["updated_at"],
            ),
        )
        snapshot = reader.metrics(self.now)
        committed = self.repository.metrics(self.now)

        self.assertEqual(snapshot["decision_count"], 1)
        self.assertEqual(snapshot["labeled_count"], 0)
        self.assertEqual(committed["labeled_count"], 1)

    def test_classifier_error_rate_counts_only_attempted_classifications(self):
        rule_error = self.repository.start_decision(**self.pending_values("7"))
        rule_error = self.repository.fail_decision(
            rule_error["id"], rule_error["updated_at"], "BudgetError",
            classifier_attempted=False, now=self.now,
        )
        budget = self.repository.start_decision(**self.pending_values("8"))
        budget = self.finish(
            budget, status="rule_only", rule_level="L2", model_level=None,
            final_level="L2", confidence=0.0, task_kind="other",
            classifier_provider=None, classifier_model=None,
            classifier_duration_ms=None,
        )
        success = self.finish(
            self.repository.start_decision(**self.pending_values("9"))
        )
        classifier_error = self.repository.start_decision(**self.pending_values("a"))
        classifier_error = self.repository.fail_decision(
            classifier_error["id"], classifier_error["updated_at"], "TimeoutError",
            now=self.now,
        )

        metrics = self.repository.metrics(self.now)

        self.assertFalse(rule_error["classifier_attempted"])
        self.assertFalse(budget["classifier_attempted"])
        self.assertTrue(success["classifier_attempted"])
        self.assertTrue(classifier_error["classifier_attempted"])
        self.assertEqual(metrics["classifier_error_rate"], 0.5)

    def test_metrics_excludes_unlabeled_accuracy_and_returns_matrix_and_percentiles(self):
        first = self.repository.start_decision(**self.pending_values("2"))
        first = self.finish(
            first, rule_level="L3", final_level="L3", model_level="L3",
            confidence=0.9, risk_flags=("credentials",),
            classifier_duration_ms=100,
        )
        self.repository.label(
            first["id"], 363129743, "downgrade", "L2", "", first["updated_at"]
        )
        second = self.repository.start_decision(
            **dict(self.pending_values("3"), dispatch_overhead_ms=30)
        )
        second = self.finish(second, classifier_duration_ms=200)
        self.repository.label(
            second["id"], 363129743, "upgrade", "L3", "", second["updated_at"]
        )
        rule_only = self.repository.start_decision(**self.pending_values("4"))
        self.finish(
            rule_only, status="rule_only", rule_level="L0", model_level=None,
            final_level="L0", confidence=1.0, task_kind="local_command",
            classifier_provider=None, classifier_model=None,
            classifier_duration_ms=None,
        )
        failed = self.repository.start_decision(**self.pending_values("5"))
        self.repository.fail_decision(
            failed["id"], failed["updated_at"], "TimeoutError", now=self.now
        )
        metrics = self.repository.metrics(self.now + timedelta(days=1))
        self.assertEqual(metrics["decision_count"], 4)
        self.assertEqual(metrics["labeled_count"], 2)
        self.assertEqual(metrics["label_accuracy"], 0.0)
        self.assertEqual(metrics["classifier_error_rate"], 1 / 3)
        self.assertEqual(metrics["classifier_duration_p50_ms"], 100)
        self.assertEqual(metrics["classifier_duration_p95_ms"], 200)
        self.assertEqual(metrics["dispatch_overhead_p95_ms"], 30)
        self.assertEqual(metrics["per_level_counts"]["L3"], 1)
        self.assertEqual(metrics["confusion_matrix"]["L3"]["L2"], 1)
        self.assertEqual(metrics["forced_risk_misses"], 0)


if __name__ == "__main__":
    unittest.main()
