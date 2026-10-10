import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


SENSITIVE = "private-value-9f7e"
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class _Repository:
    def __init__(self, snapshot=None):
        self.calls = 0
        self.snapshot = snapshot or {
            "sources": {"idle": 2, "running": 0, "retry": 0, "paused": 0},
            "authorizations": {"active": 2, "revoked": 0, "expired": 0},
            "samples": {"active": 5, "removed": 1},
            "profiles": {"candidate": 0, "active": 2, "retired": 0},
            "receipts": {"processing": 0, "sent": 0, "failed": 0},
            "enabled_groups": 2,
        }

    def status_snapshot(self):
        self.calls += 1
        return self.snapshot


class _Ollama:
    def __init__(self, result=None):
        from qq_openclaw_bridge.runtime.ollama import OllamaHealth

        self.calls = 0
        self.result = result or OllamaHealth(True, True, None)

    def health(self):
        self.calls += 1
        return self.result


class _Bomb:
    def __getattr__(self, name):
        raise AssertionError("disabled check touched a dependency")

    def __call__(self, *args, **kwargs):
        raise AssertionError("disabled check called a dependency")


class _HostileMapping(dict):
    def get(self, key, default=None):
        raise RuntimeError(SENSITIVE)


class _BootstrapAuditMapping(Mapping):
    def __init__(self, value=...):
        self.value = value
        self.accessed = []

    def get(self, key, default=None):
        self.accessed.append(key)
        if key != "ENABLE_GROUP_AGENT":
            raise AssertionError("disabled bootstrap read another key")
        return default if self.value is ... else self.value

    def __getitem__(self, key):
        raise AssertionError("disabled bootstrap used item access")

    def __iter__(self):
        raise AssertionError("disabled bootstrap iterated environment")

    def __len__(self):
        raise AssertionError("disabled bootstrap measured environment")


class _CredentialAuditMapping(Mapping):
    def __init__(self, values):
        self._values = dict(values)
        self.accessed = []

    def get(self, key, default=None):
        if key == "REDDIT_OAUTH_TOKEN_REF":
            raise AssertionError("config-only read a Reddit credential reference")
        self.accessed.append(key)
        return self._values.get(key, default)

    def __getitem__(self, key):
        if key == "REDDIT_OAUTH_TOKEN_REF":
            raise AssertionError("config-only read a Reddit credential reference")
        self.accessed.append(key)
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class CheckStyleAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database = self.root / "state.sqlite3"
        self.config_path = self.root / "community-style-agent.json"
        self.config_value = {
            "enabled": True,
            "ollama": {
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen3:8b",
                "allow_remote": False,
            },
            "sources": [{
                "id": "reddit-synthetic",
                "platform": "reddit",
                "community": "synthetic_community",
                "profile": "synthetic-style",
                "schedule_seconds": 21600,
                "authorizations": [{
                    "subject_type": "author",
                    "subject_key": "synthetic_author",
                    "scope": "all_posts_until_revoke",
                    "evidence_ref": "consent-record-synthetic-001",
                    "granted_at": "2026-09-19T00:00:00Z",
                }],
            }],
            "groups": [{
                "group_id": 100000001,
                "profile": "synthetic-style",
                "cooldown_seconds": 5,
                "max_reply_chars": 800,
            }],
        }
        self._write_config()
        self.valid_env = {
            "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
            "BOT_QQ": "100000009",
            "ALLOWED_QQ_USERS": "100000008",
            "ENABLE_SQLITE_STORE": "true",
            "ENABLE_GROUP_AGENT": "true",
            "STYLE_AGENT_CONFIG_PATH": str(self.config_path),
            "STATE_DATABASE_PATH": str(self.database),
            "REDDIT_OAUTH_TOKEN_REF": "keychain:%s:oauth" % SENSITIVE,
            "UNRELATED_TOKEN": SENSITIVE,
        }

    def _write_config(self):
        self.config_path.write_text(
            json.dumps(self.config_value), encoding="utf-8"
        )

    def _run(self, *, env=None, repository=None, ollama=None, json_output=False,
             repository_factory=None, ollama_factory=None, config_only=False):
        from check_style_agent import run_check

        return run_check(
            self.valid_env if env is None else env,
            repository=repository,
            ollama=ollama,
            json_output=json_output,
            repository_factory=repository_factory,
            ollama_factory=ollama_factory,
            config_only=config_only,
        )

    def test_config_only_validates_enabled_configuration_and_skips_runtime_dependencies(self):
        result = self._run(
            config_only=True,
            repository=_Bomb(),
            ollama=_Bomb(),
            repository_factory=_Bomb(),
            ollama_factory=_Bomb(),
            json_output=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(payload, {
            "schema_version": 1,
            "component": "community_style_agent",
            "enabled": True,
            "status": "config_valid",
            "checks": {
                "config": "ok", "database": "skipped", "ollama": "skipped",
            },
            "counts": {
                "sources": 1,
                "authorizations": 1,
                "samples": 0,
                "profiles": 1,
                "groups": 1,
            },
        })
        for forbidden in (
            SENSITIVE, "synthetic_author", "consent-record", "qwen3:8b",
            "127.0.0.1", str(self.config_path), "reddit-synthetic",
        ):
            self.assertNotIn(forbidden, result.stdout)

    def test_config_only_never_reads_reddit_credential_reference(self):
        env = _CredentialAuditMapping(self.valid_env)

        result = self._run(
            env=env,
            config_only=True,
            repository=_Bomb(), ollama=_Bomb(),
            repository_factory=_Bomb(), ollama_factory=_Bomb(),
            json_output=True,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "config_valid")
        self.assertNotIn("REDDIT_OAUTH_TOKEN_REF", env.accessed)

    def test_config_only_still_rejects_unrelated_invalid_bootstrap_before_json(self):
        missing = self.root / "must-not-open.json"
        env = dict(
            self.valid_env,
            MAX_PENDING_EVENTS="0",
            STYLE_AGENT_CONFIG_PATH=str(missing),
        )

        with mock.patch(
            "check_style_agent.load_community_style_settings",
            side_effect=KeyboardInterrupt(SENSITIVE),
        ):
            result = self._run(
                env=env,
                config_only=True,
                repository=_Bomb(), ollama=_Bomb(),
                repository_factory=_Bomb(), ollama_factory=_Bomb(),
                json_output=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["status"], "invalid_config")
        self.assertEqual(payload["checks"], {
            "config": "invalid", "database": "skipped", "ollama": "skipped",
        })
        self.assertFalse(missing.exists())

    def test_config_only_text_output_is_exact_and_contains_only_safe_counts(self):
        result = self._run(
            config_only=True,
            repository=_Bomb(), ollama=_Bomb(),
            repository_factory=_Bomb(), ollama_factory=_Bomb(),
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, (
            "community_style_agent status=config_valid enabled=true config=ok "
            "database=skipped ollama=skipped sources=1 authorizations=1 "
            "samples=0 profiles=1 groups=1\n"
        ))
        for forbidden in (
            SENSITIVE, "synthetic_author", "consent-record", "qwen3:8b",
            "127.0.0.1", str(self.config_path), "reddit-synthetic",
        ):
            self.assertNotIn(forbidden, result.stdout)

    def test_config_only_preserves_disabled_and_invalid_configuration_semantics(self):
        disabled = self._run(
            env=_BootstrapAuditMapping("false"),
            repository=_Bomb(), ollama=_Bomb(),
            repository_factory=_Bomb(), ollama_factory=_Bomb(),
            config_only=True, json_output=True,
        )
        self.assertEqual(disabled.exit_code, 0)
        self.assertEqual(json.loads(disabled.stdout)["status"], "disabled")

        self.config_value["ollama"] = {
            "base_url": "http://192.0.2.10:11434",
            "model": "qwen3:8b",
            "allow_remote": False,
        }
        self._write_config()
        invalid = self._run(
            repository=_Bomb(), ollama=_Bomb(),
            repository_factory=_Bomb(), ollama_factory=_Bomb(),
            config_only=True, json_output=True,
        )
        payload = json.loads(invalid.stdout)
        self.assertEqual(invalid.exit_code, 2)
        self.assertEqual(payload["status"], "invalid_config")
        self.assertEqual(payload["checks"], {
            "config": "invalid", "database": "skipped", "ollama": "skipped",
        })
        self.assertNotIn("192.0.2.10", invalid.stdout)

    def test_config_only_requires_an_exact_boolean_without_opening_configuration(self):
        class HostileValue:
            def __bool__(self):
                raise AssertionError("hostile config_only was coerced")

        missing = self.root / "must-not-open.json"
        env = dict(self.valid_env, STYLE_AGENT_CONFIG_PATH=str(missing))
        result = self._run(
            env=env, config_only=HostileValue(), json_output=True,
            repository=_Bomb(), ollama=_Bomb(),
            repository_factory=_Bomb(), ollama_factory=_Bomb(),
        )

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "invalid_config")
        self.assertFalse(missing.exists())

    def test_disabled_bootstrap_is_ready_and_touches_no_config_database_or_ollama(self):
        env = {
            "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
            "ENABLE_GROUP_AGENT": "false",
            "STYLE_AGENT_CONFIG_PATH": str(self.root / "must-not-open.json"),
            "STATE_DATABASE_PATH": str(self.root / "must-not-create" / "db.sqlite3"),
        }

        result = self._run(
            env=env,
            repository=_Bomb(),
            ollama=_Bomb(),
            repository_factory=_Bomb(),
            ollama_factory=_Bomb(),
            json_output=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(payload["status"], "disabled")
        self.assertEqual(payload["checks"], {
            "config": "disabled", "database": "skipped", "ollama": "skipped",
        })
        self.assertFalse((self.root / "must-not-open.json").exists())
        self.assertFalse((self.root / "must-not-create").exists())

    def test_disabled_or_default_bootstrap_returns_before_config_and_reads_only_flag(self):
        for value in (..., "", "  ", "0", "false", "NO", "off"):
            with self.subTest(value=value):
                env = _BootstrapAuditMapping(value)
                result = self._run(
                    env=env, repository=_Bomb(), ollama=_Bomb(),
                    repository_factory=_Bomb(), ollama_factory=_Bomb(),
                    json_output=True,
                )
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(json.loads(result.stdout)["status"], "disabled")
                self.assertEqual(env.accessed, ["ENABLE_GROUP_AGENT"])

    def test_invalid_or_hostile_group_flag_is_invalid_config_without_other_access(self):
        hostile = type("HostileFlag", (str,), {
            "strip": lambda self: (_ for _ in ()).throw(RuntimeError(SENSITIVE)),
        })("false")
        for value in ("sometimes", True, hostile):
            with self.subTest(kind=type(value).__name__):
                env = _BootstrapAuditMapping(value)
                result = self._run(env=env, json_output=True)
                payload = json.loads(result.stdout)
                self.assertEqual(result.exit_code, 2)
                self.assertEqual(payload["status"], "invalid_config")
                self.assertEqual(env.accessed, ["ENABLE_GROUP_AGENT"])
                self.assertNotIn(SENSITIVE, result.stdout)

    def test_disabled_json_configuration_skips_database_and_ollama(self):
        self.config_value["enabled"] = False
        self._write_config()

        result = self._run(
            repository=_Bomb(), ollama=_Bomb(),
            repository_factory=_Bomb(), ollama_factory=_Bomb(),
        )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("status=disabled", result.stdout)
        self.assertFalse(self.database.exists())

    def test_invalid_bootstrap_or_json_returns_two_without_values_paths_or_exception_chain(self):
        cases = []
        invalid_bootstrap = dict(self.valid_env, ENABLE_GROUP_AGENT="sometimes")
        cases.append(invalid_bootstrap)
        self.config_path.write_text('{"enabled":', encoding="utf-8")
        cases.append(dict(self.valid_env))

        for index, env in enumerate(cases):
            if index == 1:
                self.config_path.write_text('{"enabled":', encoding="utf-8")
            with self.subTest(index=index):
                result = self._run(env=env, repository=_Bomb(), ollama=_Bomb())
                self.assertEqual(result.exit_code, 2)
                self.assertTrue(result.stdout.startswith(
                    "community_style_agent status=invalid_config enabled="
                ))
                self.assertIn(
                    "config=invalid database=skipped ollama=skipped ",
                    result.stdout,
                )
                self.assertTrue(result.stdout.endswith(
                    "sources=0 authorizations=0 samples=0 profiles=0 groups=0\n"
                ))
                self.assertNotIn(str(self.config_path), result.stdout)
                self.assertNotIn(SENSITIVE, result.stdout)

    def test_missing_database_is_not_ready_and_creates_nothing(self):
        missing = self.root / "missing" / "state.sqlite3"
        env = dict(self.valid_env, STATE_DATABASE_PATH=str(missing))

        result = self._run(env=env, ollama=_Bomb())

        self.assertEqual(result.exit_code, 1)
        self.assertIn("database=missing", result.stdout)
        self.assertIn("ollama=skipped", result.stdout)
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        self.assertNotIn(str(missing), result.stdout)

    def test_ready_text_status_is_fixed_and_never_prints_sensitive_fields(self):
        snapshot = _Repository().snapshot
        snapshot["authorization_subject"] = "authorized_user_" + SENSITIVE
        snapshot["evidence_ref"] = "consent_" + SENSITIVE
        repo = _Repository(snapshot)
        ollama = _Ollama()

        result = self._run(repository=repo, ollama=ollama)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, (
            "community_style_agent status=ready enabled=true config=ok "
            "database=ok ollama=ok sources=2 authorizations=2 samples=5 "
            "profiles=2 groups=2\n"
        ))
        self.assertNotIn("authorized_user", result.stdout)
        self.assertNotIn("evidence", result.stdout)
        self.assertNotIn("keychain", result.stdout)
        self.assertNotIn(SENSITIVE, result.stdout)
        self.assertEqual((repo.calls, ollama.calls), (1, 1))

    def test_json_status_has_fixed_schema_and_builtin_finite_counts(self):
        result = self._run(
            repository=_Repository(), ollama=_Ollama(), json_output=True
        )
        payload = json.loads(result.stdout, parse_constant=lambda value: self.fail(value))

        self.assertEqual(set(payload), {
            "schema_version", "component", "enabled", "status", "checks", "counts",
        })
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["component"], "community_style_agent")
        self.assertEqual(set(payload["checks"]), {"config", "database", "ollama"})
        self.assertEqual(set(payload["counts"]), {
            "sources", "authorizations", "samples", "profiles", "groups",
        })
        self.assertTrue(all(type(value) is int and value >= 0
                            for value in payload["counts"].values()))

    def test_missing_model_and_unreachable_ollama_are_not_ready_with_allowlisted_status(self):
        from qq_openclaw_bridge.runtime.ollama import OllamaHealth

        for health, expected in (
            (OllamaHealth(True, False, "model_missing"), "model_missing"),
            (OllamaHealth(False, False, "connection"), "connection"),
            (OllamaHealth(False, False, "timeout"), "timeout"),
        ):
            with self.subTest(expected=expected):
                result = self._run(repository=_Repository(), ollama=_Ollama(health))
                self.assertEqual(result.exit_code, 1)
                self.assertIn("ollama=%s" % expected, result.stdout)

    def test_hostile_repository_mapping_nan_and_ollama_result_fail_closed_without_leaks(self):
        from qq_openclaw_bridge.runtime.ollama import OllamaHealth

        hostile = {
            "sources": {"idle": math.nan},
            "authorizations": _HostileMapping(active=3),
            "samples": {"active": -1},
            "profiles": {"active": True},
            "enabled_groups": SENSITIVE,
        }
        cases = (
            (_Repository(hostile), _Ollama(), "database=invalid"),
            (_Repository(), _Ollama(OllamaHealth(True, False, SENSITIVE)),
             "ollama=invalid_response"),
        )
        for repository, ollama, expected in cases:
            with self.subTest(expected=expected):
                result = self._run(repository=repository, ollama=ollama, json_output=True)
                self.assertEqual(result.exit_code, 1)
                self.assertIn(expected.split("=")[1], result.stdout)
                self.assertNotIn(SENSITIVE, result.stdout)
                self.assertNotIn("NaN", result.stdout)

    def test_dependency_exceptions_are_sanitized_and_do_not_escape(self):
        class HostileRepository:
            def status_snapshot(self):
                raise type("HostileSystemExit", (SystemExit,), {})(SENSITIVE)

        class HostileOllama:
            def health(self):
                raise type("HostileKeyboardInterrupt", (KeyboardInterrupt,), {})(SENSITIVE)

        for repository, ollama, expected in (
            (HostileRepository(), _Bomb(), "database=invalid"),
            (_Repository(), HostileOllama(), "ollama=invalid_response"),
        ):
            with self.subTest(expected=expected):
                result = self._run(repository=repository, ollama=ollama)
                self.assertEqual(result.exit_code, 1)
                self.assertIn(expected, result.stdout)
                self.assertNotIn(SENSITIVE, result.stdout)

    def test_default_repository_reads_existing_database_without_mutating_files(self):
        from qq_openclaw_bridge.persistence.community_style import CommunityStyleRepository
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.runtime.community_style_config import load_community_style_settings

        database = SqliteDatabase(self.database)
        repository = CommunityStyleRepository(database)
        repository.sync_configuration(
            load_community_style_settings(self.config_path), NOW
        )
        before_names = sorted(path.name for path in self.root.iterdir())
        before_bytes = self.database.read_bytes()
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        before_stat = self.database.stat()

        result = self._run(ollama=_Ollama())

        after_stat = self.database.stat()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(self.database.read_bytes(), before_bytes)
        self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(after_stat.st_mode, before_stat.st_mode)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before_names)

    def test_active_wal_never_returns_stale_ready_or_mutates_source_files(self):
        from qq_openclaw_bridge.persistence.community_style import CommunityStyleRepository
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.runtime.community_style_config import load_community_style_settings

        database = SqliteDatabase(self.database)
        repository = CommunityStyleRepository(database)
        repository.sync_configuration(
            load_community_style_settings(self.config_path), NOW
        )
        writer = sqlite3.connect(str(self.database))
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("DROP TABLE community_style_profiles")
        writer.commit()
        wal = Path(str(self.database) + "-wal")
        self.assertTrue(wal.is_file())
        self.assertGreater(wal.stat().st_size, 0)

        def state():
            result = {}
            for path in (self.database, wal, Path(str(self.database) + "-shm")):
                if path.exists():
                    details = path.stat()
                    result[path.name] = (
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        details.st_size,
                        details.st_mtime_ns,
                        details.st_mode,
                    )
            return result

        before_names = sorted(path.name for path in self.root.iterdir())
        before = state()

        result = self._run(ollama=_Bomb())

        self.assertEqual(result.exit_code, 1)
        self.assertIn("status=not_ready", result.stdout)
        self.assertNotIn("status=ready", result.stdout)
        self.assertTrue(
            "database=busy" in result.stdout or "database=invalid" in result.stdout
        )
        self.assertIn("ollama=skipped", result.stdout)
        self.assertEqual(state(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before_names)

    def test_snapshot_component_limits_reject_before_temporary_copy(self):
        from check_style_agent import (
            _DatabaseSnapshotUnavailable, _SnapshotLimits, _read_only_copy,
        )

        limits = _SnapshotLimits(
            main_bytes=100, wal_bytes=60, shm_bytes=40,
            total_bytes=150, deadline_seconds=1.0,
        )
        cases = (
            ("main", 101, 0, 0),
            ("wal", 1, 61, 0),
            ("shm", 1, 0, 41),
        )
        for name, main_size, wal_size, shm_size in cases:
            with self.subTest(name=name):
                folder = self.root / name
                folder.mkdir()
                database = folder / "state.sqlite3"
                with database.open("wb") as stream:
                    stream.truncate(main_size)
                if wal_size:
                    with Path(str(database) + "-wal").open("wb") as stream:
                        stream.truncate(wal_size)
                if shm_size:
                    with Path(str(database) + "-shm").open("wb") as stream:
                        stream.truncate(shm_size)
                with mock.patch(
                    "check_style_agent.tempfile.TemporaryDirectory",
                    side_effect=AssertionError("temporary copy started"),
                ):
                    with self.assertRaises(_DatabaseSnapshotUnavailable):
                        with _read_only_copy(database, limits=limits):
                            self.fail("oversized snapshot opened")

    def test_sparse_one_tib_database_is_rejected_before_copy_or_write(self):
        from check_style_agent import (
            _DatabaseSnapshotUnavailable, _read_only_copy,
        )

        with self.database.open("wb") as stream:
            stream.truncate(1024 ** 4)
        before = self.database.stat()
        with mock.patch(
            "check_style_agent.tempfile.TemporaryDirectory",
            side_effect=AssertionError("temporary copy started"),
        ):
            with self.assertRaises(_DatabaseSnapshotUnavailable):
                with _read_only_copy(self.database):
                    self.fail("sparse database opened")
        result = self._run(ollama=_Bomb())
        after = self.database.stat()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("database=snapshot_unavailable", result.stdout)
        self.assertIn("ollama=skipped", result.stdout)
        self.assertNotIn(str(self.database), result.stdout)
        self.assertEqual((after.st_size, after.st_mtime_ns),
                         (before.st_size, before.st_mtime_ns))

    def test_snapshot_total_limit_counts_db_wal_and_shm(self):
        from check_style_agent import (
            _DatabaseSnapshotUnavailable, _SnapshotLimits, _read_only_copy,
        )

        limits = _SnapshotLimits(
            main_bytes=100, wal_bytes=60, shm_bytes=40,
            total_bytes=150, deadline_seconds=1.0,
        )
        for path, size in (
            (self.database, 80),
            (Path(str(self.database) + "-wal"), 50),
            (Path(str(self.database) + "-shm"), 30),
        ):
            path.write_bytes(b"x" * size)
        with mock.patch(
            "check_style_agent.tempfile.TemporaryDirectory",
            side_effect=AssertionError("temporary copy started"),
        ):
            with self.assertRaises(_DatabaseSnapshotUnavailable):
                with _read_only_copy(self.database, limits=limits):
                    self.fail("over-total snapshot opened")

    def test_snapshot_limits_allow_exact_boundary(self):
        from check_style_agent import _SnapshotLimits, _read_only_copy

        limits = _SnapshotLimits(
            main_bytes=80, wal_bytes=40, shm_bytes=30,
            total_bytes=150, deadline_seconds=1.0,
        )
        self.database.write_bytes(b"d" * 80)
        Path(str(self.database) + "-wal").write_bytes(b"w" * 40)
        Path(str(self.database) + "-shm").write_bytes(b"s" * 30)

        with _read_only_copy(self.database, limits=limits) as snapshot:
            self.assertEqual(snapshot.stat().st_size, 80)
            self.assertEqual(Path(str(snapshot) + "-wal").stat().st_size, 40)

    def test_bounded_copy_rejects_stream_growth_and_slow_clock(self):
        from check_style_agent import (
            _DatabaseSnapshotUnavailable, _copy_bounded,
        )

        growing_output = io.BytesIO()
        with self.assertRaises(_DatabaseSnapshotUnavailable):
            _copy_bounded(
                io.BytesIO(b"abc"), growing_output,
                expected_size=2, byte_limit=8, deadline=10.0,
                clock=lambda: 0.0,
            )
        self.assertLessEqual(len(growing_output.getvalue()), 2)

        moments = iter((0.0, 0.5, 1.1))
        slow_output = io.BytesIO()
        with self.assertRaises(_DatabaseSnapshotUnavailable):
            _copy_bounded(
                io.BytesIO(b"ab"), slow_output,
                expected_size=2, byte_limit=8, deadline=1.0,
                clock=lambda: next(moments), chunk_bytes=1,
            )
        self.assertLessEqual(len(slow_output.getvalue()), 1)

    def test_snapshot_timeout_removes_partial_temporary_directory(self):
        from check_style_agent import (
            _DatabaseSnapshotUnavailable, _SnapshotLimits, _read_only_copy,
        )

        self.database.write_bytes(b"x" * (128 * 1024))
        temp_parent = self.root / "snapshots"
        temp_parent.mkdir()
        moments = iter((0.0, 0.0, 2.0))
        limits = _SnapshotLimits(
            main_bytes=256 * 1024, wal_bytes=1, shm_bytes=1,
            total_bytes=256 * 1024, deadline_seconds=1.0,
        )

        with self.assertRaises(_DatabaseSnapshotUnavailable):
            with _read_only_copy(
                self.database, limits=limits,
                clock=lambda: next(moments), temp_parent=temp_parent,
            ):
                self.fail("timed-out snapshot opened")

        self.assertEqual(list(temp_parent.iterdir()), [])

    def test_snapshot_exact_control_is_sanitized_before_propagation(self):
        from qq_openclaw_bridge.persistence.community_style import CommunityStyleRepository
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.runtime.community_style_config import load_community_style_settings

        database = SqliteDatabase(self.database)
        CommunityStyleRepository(database).sync_configuration(
            load_community_style_settings(self.config_path), NOW
        )
        original = KeyboardInterrupt(SENSITIVE)
        with mock.patch(
            "check_style_agent.time.monotonic", side_effect=original
        ), self.assertRaises(KeyboardInterrupt) as caught:
            self._run(ollama=_Bomb())

        self.assertEqual(caught.exception.args, ())
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_confirmed_source_toctou_variants_are_database_busy(self):
        from check_style_agent import (
            _DatabaseBusy, _SnapshotLimits, _read_only_copy,
        )

        limits = _SnapshotLimits(
            main_bytes=1024, wal_bytes=1024, shm_bytes=1024,
            total_bytes=2048, deadline_seconds=1.0,
        )

        def exercise(name, prepare, mutate):
            folder = self.root / name
            folder.mkdir()
            database = folder / "state.sqlite3"
            database.write_bytes(b"main")
            prepare(database)
            calls = 0

            def clock():
                nonlocal calls
                calls += 1
                if calls == 3:
                    mutate(database)
                return 0.0

            with self.assertRaises(_DatabaseBusy):
                with _read_only_copy(
                    database, limits=limits, clock=clock,
                    temp_parent=folder,
                ):
                    self.fail("changed source was accepted")
            self.assertFalse(any(path.name.startswith("qq-style-readiness-")
                                 for path in folder.iterdir()))

        no_prepare = lambda database: None

        def grow(database):
            with database.open("ab") as stream:
                stream.write(b"more")

        def swap_wal(database):
            replacement = database.parent / "replacement-wal"
            replacement.write_bytes(b"new")
            os.replace(replacement, Path(str(database) + "-wal"))

        cases = (
            (
                "mtime", no_prepare,
                lambda database: os.utime(
                    database,
                    ns=(database.stat().st_atime_ns,
                        database.stat().st_mtime_ns + 1_000_000),
                ),
            ),
            (
                "growth", no_prepare,
                grow,
            ),
            (
                "shrink", no_prepare,
                lambda database: os.truncate(database, 1),
            ),
            (
                "sidecar-add", no_prepare,
                lambda database: Path(str(database) + "-wal").write_bytes(b"new"),
            ),
            (
                "sidecar-delete",
                lambda database: Path(str(database) + "-wal").write_bytes(b"old"),
                lambda database: Path(str(database) + "-wal").unlink(),
            ),
            (
                "sidecar-swap",
                lambda database: Path(str(database) + "-wal").write_bytes(b"old"),
                swap_wal,
            ),
        )
        for name, prepare, mutate in cases:
            with self.subTest(name=name):
                exercise(name, prepare, mutate)

    def test_confirmed_toctou_maps_to_busy_exit_without_details(self):
        from qq_openclaw_bridge.persistence.community_style import CommunityStyleRepository
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.runtime.community_style_config import load_community_style_settings

        database = SqliteDatabase(self.database)
        CommunityStyleRepository(database).sync_configuration(
            load_community_style_settings(self.config_path), NOW
        )
        calls = 0

        def clock():
            nonlocal calls
            calls += 1
            if calls == 3:
                details = self.database.stat()
                os.utime(
                    self.database,
                    ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000),
                )
            return 0.0

        with mock.patch("check_style_agent.time.monotonic", side_effect=clock):
            result = self._run(ollama=_Bomb())

        self.assertEqual(result.exit_code, 1)
        self.assertIn("database=busy", result.stdout)
        self.assertIn("ollama=skipped", result.stdout)
        self.assertNotIn(str(self.database), result.stdout)

    def test_ambiguous_snapshot_io_and_temporary_failures_are_unavailable(self):
        from qq_openclaw_bridge.persistence.community_style import CommunityStyleRepository
        from qq_openclaw_bridge.persistence.database import SqliteDatabase
        from qq_openclaw_bridge.runtime.community_style_config import load_community_style_settings

        database = SqliteDatabase(self.database)
        CommunityStyleRepository(database).sync_configuration(
            load_community_style_settings(self.config_path), NOW
        )
        cases = (
            ("source-open", "check_style_agent.os.open"),
            ("source-close", "check_style_agent.os.close"),
            ("temporary", "check_style_agent.tempfile.TemporaryDirectory"),
        )
        for name, target in cases:
            with self.subTest(name=name), mock.patch(
                target, side_effect=OSError(SENSITIVE)
            ):
                result = self._run(ollama=_Bomb())
                self.assertEqual(result.exit_code, 1)
                self.assertIn("database=snapshot_unavailable", result.stdout)
                self.assertIn("ollama=skipped", result.stdout)
                self.assertNotIn(SENSITIVE, result.stdout)
                self.assertNotIn(str(self.database), result.stdout)

    def _run_first_snapshot_close_fault(self, close_error, copy_error=None):
        from check_style_agent import _copy_bounded, _SourceStreamMismatch

        self.database.write_bytes(b"synthetic snapshot bytes")
        temp_parent = self.root / "close-fault-snapshots"
        temp_parent.mkdir(exist_ok=True)
        real_close = os.close
        real_temporary_directory = tempfile.TemporaryDirectory
        close_calls = []
        temporary_paths = []

        def source_state():
            details = self.database.stat()
            return (
                self.database.read_bytes(), details.st_size,
                details.st_mtime_ns, details.st_ctime_ns, details.st_mode,
                sorted(path.name for path in self.root.iterdir()),
            )

        expected_source = source_state()

        def close_once(descriptor):
            close_calls.append(descriptor)
            # A failed close can already have released its descriptor.
            real_close(descriptor)
            if len(close_calls) == 1 and close_error is not None:
                raise close_error

        def temporary_directory(*args, **kwargs):
            kwargs["dir"] = temp_parent
            manager = real_temporary_directory(*args, **kwargs)
            temporary_paths.append(Path(manager.name))
            return manager

        def copy_body(*args, **kwargs):
            nonlocal expected_source
            if copy_error == "confirmed-busy":
                details = self.database.stat()
                os.utime(self.database, ns=(
                    details.st_atime_ns, details.st_mtime_ns + 1_000_000,
                ))
                expected_source = source_state()
                raise _SourceStreamMismatch(SENSITIVE)
            if copy_error is not None:
                raise copy_error
            return _copy_bounded(*args, **kwargs)

        with mock.patch("check_style_agent.os.close", side_effect=close_once), \
                mock.patch("check_style_agent.tempfile.TemporaryDirectory",
                           side_effect=temporary_directory), \
                mock.patch("check_style_agent._copy_bounded", side_effect=copy_body):
            try:
                result = self._run(
                    repository_factory=lambda path: _Repository(), ollama=_Ollama()
                )
            except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
                result = error

        self.assertEqual(len(close_calls), 2)
        self.assertEqual(len(temporary_paths), 1)
        self.assertFalse(temporary_paths[0].exists())
        self.assertEqual(list(temp_parent.iterdir()), [])
        self.assertEqual(source_state(), expected_source)
        return result

    def test_first_source_close_failure_is_unavailable_when_cleanup_succeeds(self):
        failures = [OSError, RuntimeError, BaseException]
        failures.extend(type("Hostile" + kind.__name__, (kind,), {})
                        for kind in (KeyboardInterrupt, SystemExit, GeneratorExit))
        for kind in failures:
            with self.subTest(kind=kind.__name__):
                result = self._run_first_snapshot_close_fault(kind(SENSITIVE))
                self.assertEqual(result.exit_code, 1)
                self.assertIn("database=snapshot_unavailable", result.stdout)
                self.assertIn("ollama=skipped", result.stdout)
                self.assertNotIn(SENSITIVE, result.stdout)
                self.assertNotIn(str(self.database), result.stdout)

    def test_copy_and_close_failure_priority_preserves_confirmed_busy_and_controls(self):
        cases = [
            (RuntimeError(SENSITIVE), OSError(SENSITIVE), "snapshot_unavailable"),
            ("confirmed-busy", OSError(SENSITIVE), "busy"),
            ("confirmed-busy", None, "busy"),
        ]
        for control in (KeyboardInterrupt, SystemExit, GeneratorExit):
            cases.extend((
                (None, control(SENSITIVE), control),
                (RuntimeError(SENSITIVE), control(SENSITIVE), control),
                ("confirmed-busy", control(SENSITIVE), control),
                (control(SENSITIVE), OSError(SENSITIVE), control),
                (control(SENSITIVE), None, control),
            ))
        for copy_error, close_error, expected in cases:
            with self.subTest(copy_type=type(copy_error).__name__,
                              close_type=type(close_error).__name__, expected=expected):
                result = self._run_first_snapshot_close_fault(close_error, copy_error)
                if isinstance(expected, str):
                    self.assertEqual(result.exit_code, 1)
                    self.assertIn("database=" + expected, result.stdout)
                    self.assertIn("ollama=skipped", result.stdout)
                    self.assertNotIn(SENSITIVE, result.stdout)
                    self.assertNotIn(str(self.database), result.stdout)
                else:
                    self.assertIs(type(result), expected)
                    self.assertIsNot(result, close_error)
                    self.assertIsNot(result, copy_error)
                    self.assertEqual(result.args, ())
                    self.assertIsNone(result.__cause__)
                    self.assertIsNone(result.__context__)

    def test_successful_source_close_still_returns_ready_and_cleans_snapshot(self):
        result = self._run_first_snapshot_close_fault(None)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("database=ok", result.stdout)
        self.assertIn("status=ready", result.stdout)

    def test_corrupt_database_is_not_ready_and_never_echoes_bytes_or_path(self):
        self.database.write_bytes(("not sqlite " + SENSITIVE).encode("utf-8"))
        before = self.database.read_bytes()
        before_mtime = self.database.stat().st_mtime_ns

        result = self._run(ollama=_Bomb())

        self.assertEqual(result.exit_code, 1)
        self.assertIn("database=invalid", result.stdout)
        self.assertIn("ollama=skipped", result.stdout)
        self.assertNotIn(SENSITIVE, result.stdout)
        self.assertNotIn(str(self.database), result.stdout)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.database.stat().st_mtime_ns, before_mtime)

    def test_default_ollama_factory_receives_task4_settings_and_strict_timeout(self):
        captured = []

        def factory(settings, *, timeout_seconds):
            captured.append((settings, timeout_seconds))
            return _Ollama()

        result = self._run(repository=_Repository(), ollama_factory=factory)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(captured), 1)
        settings, timeout = captured[0]
        self.assertEqual(settings.base_url, "http://127.0.0.1:11434")
        self.assertEqual(settings.model, "qwen3:8b")
        self.assertIs(settings.allow_remote, False)
        self.assertEqual(timeout, 5.0)

    def test_explicit_https_remote_opt_in_uses_task4_health_boundary(self):
        self.config_value["ollama"] = {
            "base_url": "https://192.0.2.10:11434",
            "model": "qwen3:8b",
            "allow_remote": True,
        }
        self._write_config()
        captured = []

        def factory(settings, *, timeout_seconds):
            captured.append((settings, timeout_seconds))
            return _Ollama()

        result = self._run(repository=_Repository(), ollama_factory=factory)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("status=ready", result.stdout)
        self.assertEqual(len(captured), 1)
        settings, timeout = captured[0]
        self.assertTrue(settings.allow_remote)
        self.assertEqual(settings.base_url, "https://192.0.2.10:11434")
        self.assertEqual(timeout, 5.0)

    def test_example_is_loadable_synthetic_and_covers_both_platforms_and_scopes(self):
        from qq_openclaw_bridge.runtime.community_style_config import (
            load_community_style_settings,
        )

        example = ROOT / "config" / "community-style-agent.example.json"
        settings = load_community_style_settings(example)

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.ollama.base_url, "http://127.0.0.1:11434")
        self.assertEqual(settings.ollama.model, "qwen3:8b")
        self.assertFalse(settings.ollama.allow_remote)
        self.assertEqual({source.platform for source in settings.sources}, {"reddit", "tieba"})
        self.assertTrue(all(source.schedule_seconds == 21600 for source in settings.sources))
        self.assertEqual(
            {grant.scope for source in settings.sources for grant in source.authorizations},
            {"all_posts_until_revoke", "single_post"},
        )
        self.assertTrue(all(group.cooldown_seconds == 5 for group in settings.groups))
        self.assertTrue(all(group.max_reply_chars == 800 for group in settings.groups))
        raw = example.read_text(encoding="utf-8").casefold()
        for forbidden in ("client_secret", "refresh_token", "access_token", "cookie"):
            self.assertNotIn(forbidden, raw)

    def test_cli_disabled_json_command_is_machine_readable_and_does_not_open_paths(self):
        missing = self.root / "missing" / "state.sqlite3"
        environment = os.environ.copy()
        environment.update({
            "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
            "ENABLE_GROUP_AGENT": "false",
            "STYLE_AGENT_CONFIG_PATH": str(self.root / "not-there.json"),
            "STATE_DATABASE_PATH": str(missing),
        })

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_style_agent.py"), "--json"],
            env=environment,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "disabled")
        self.assertEqual(result.stderr, "")
        self.assertFalse(missing.parent.exists())

    def test_cli_config_only_combines_with_json_and_project_dir_without_opening_database(self):
        project = self.root / "project"
        project.mkdir()
        relative_config = Path("config") / "style.json"
        config_path = project / relative_config
        config_path.parent.mkdir()
        config_path.write_text(json.dumps(self.config_value), encoding="utf-8")
        missing_database = project / "state" / "missing.sqlite3"
        environment = os.environ.copy()
        environment.update(dict(
            self.valid_env,
            STYLE_AGENT_CONFIG_PATH=str(relative_config),
            STATE_DATABASE_PATH=str(missing_database.relative_to(project)),
        ))

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "check_style_agent.py"),
                "--config-only",
                "--json",
                "--project-dir",
                str(project),
            ],
            env=environment,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "config_valid")
        self.assertEqual(payload["checks"], {
            "config": "ok", "database": "skipped", "ollama": "skipped",
        })
        self.assertEqual(result.stderr, "")
        self.assertFalse(missing_database.parent.exists())
        self.assertNotIn(str(project), result.stdout)
        self.assertNotIn(SENSITIVE, result.stdout)

    def test_help_documents_exit_codes_without_loading_configuration(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_style_agent.py"), "--help"],
            env={}, capture_output=True, text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0=ready", result.stdout)
        self.assertIn("config-valid", result.stdout)
        self.assertIn("1=not-ready", result.stdout)
        self.assertIn("2=invalid-config", result.stdout)
        self.assertIn("--config-only", result.stdout)


if __name__ == "__main__":
    unittest.main()
