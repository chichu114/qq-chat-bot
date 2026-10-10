from datetime import datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class BackupManagerTests(unittest.TestCase):
    KEY = bytes(range(32))

    class StaticKeyProvider:
        def get_or_create(self):
            return BackupManagerTests.KEY

    def _manager(self, root, now, max_auto=30):
        from qq_openclaw_bridge.backups import BackupManager

        base = Path(root)
        project = base / "project"
        workspace = base / "workspace"
        project.mkdir()
        workspace.mkdir()
        env_path = project / ".env"
        memory_path = workspace / "MEMORY.md"
        env_path.write_text("ONEBOT_WS_URL=ws://127.0.0.1:3001\n", encoding="utf-8")
        memory_path.write_text("memory-before\n", encoding="utf-8")
        manager = BackupManager(
            backup_dir=project / "backups",
            sources={"project/.env": env_path, "workspace/MEMORY.md": memory_path},
            now_provider=lambda: now[0],
            interval_hours=24,
            retention_days=30,
            max_auto_backups=max_auto,
            key_provider=self.StaticKeyProvider(),
        )
        return manager, env_path, memory_path

    def _open_encrypted_zip(self, manager, name):
        from qq_openclaw_bridge.runtime.backup_crypto import BackupEnvelope

        encrypted = (manager.backup_dir / name).read_bytes()
        return zipfile.ZipFile(io.BytesIO(BackupEnvelope.decrypt(encrypted, self.KEY)))

    def _write_legacy_backup(
        self, manager, name, kind, created_at, content=b"legacy-memory\n"
    ):
        path = manager.backup_dir / name
        manager.backup_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "version": 1,
            "created_at": created_at,
            "kind": kind,
            "files": {
                "workspace/MEMORY.md": {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            },
        }
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("workspace/MEMORY.md", content)
            archive.writestr("manifest.json", json.dumps(manifest))
        return path

    def test_backup_contains_manifest_and_verifies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now)

            backup = manager.create_backup("manual")
            verification = manager.verify_backup(backup["name"])

            self.assertTrue(verification["valid"])
            self.assertEqual(verification["file_count"], 2)
            self.assertTrue(backup["name"].endswith(".chichu-backup"))
            self.assertEqual(backup["encryption"], "encrypted")
            self.assertNotEqual(
                (manager.backup_dir / backup["name"]).read_bytes()[:2], b"PK"
            )
            self.assertEqual(
                (manager.backup_dir / backup["name"]).stat().st_mode & 0o777,
                0o600,
            )
            with self._open_encrypted_zip(manager, backup["name"]) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["version"], 1)
            self.assertIn("workspace/MEMORY.md", manifest["files"])

    def test_auto_backup_runs_only_when_due(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now)

            first = manager.run_if_due()
            immediate = manager.run_if_due()
            now[0] += timedelta(hours=24, seconds=1)
            next_day = manager.run_if_due()

            self.assertIsNotNone(first)
            self.assertIsNone(immediate)
            self.assertIsNotNone(next_day)
            self.assertEqual(
                len(
                    [
                        item
                        for item in manager.list_backups()
                        if item["kind"] == "auto"
                    ]
                ),
                2,
            )

    def test_retention_prunes_old_auto_backups_but_keeps_manual(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 6, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now, max_auto=2)
            manual = manager.create_backup("manual")

            for _ in range(4):
                manager.create_backup("auto")
                now[0] += timedelta(days=1)
            manager.prune()
            backups = manager.list_backups()

            self.assertIn(manual["name"], [item["name"] for item in backups])
            self.assertEqual(len([item for item in backups if item["kind"] == "auto"]), 2)

    def test_corrupt_backup_is_rejected_before_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, memory_path = self._manager(temp_dir, now)
            corrupt_path = manager.backup_dir / "manual-corrupt.zip"
            manager.backup_dir.mkdir(parents=True)
            manifest = {
                "version": 1,
                "created_at": now[0].isoformat(),
                "kind": "manual",
                "files": {"workspace/MEMORY.md": {"sha256": "wrong", "size": 7}},
            }
            with zipfile.ZipFile(corrupt_path, "w") as archive:
                archive.writestr("workspace/MEMORY.md", "damaged")
                archive.writestr("manifest.json", json.dumps(manifest))
            memory_path.write_text("current-safe-data\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "校验失败"):
                manager.restore_backup(corrupt_path.name)

            self.assertEqual(memory_path.read_text(encoding="utf-8"), "current-safe-data\n")

    def test_malformed_legacy_manifest_is_listed_as_invalid_without_crashing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now)
            malformed = manager.backup_dir / "manual-malformed.zip"
            manager.backup_dir.mkdir(parents=True)
            manifest = {
                "version": 1,
                "created_at": now[0].isoformat(),
                "kind": "manual",
                "files": {"workspace/MEMORY.md": "not-metadata"},
            }
            with zipfile.ZipFile(malformed, "w") as archive:
                archive.writestr("workspace/MEMORY.md", "content")
                archive.writestr("manifest.json", json.dumps(manifest))

            listed = manager.list_backups()

            self.assertEqual(len(listed), 1)
            self.assertFalse(listed[0]["valid"])
            self.assertEqual(listed[0]["encryption"], "legacy-unencrypted")

    def test_live_sqlite_database_is_snapshotted_with_wal_content(self):
        from qq_openclaw_bridge.backups import BackupManager

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "chichu.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE facts(value TEXT)")
            connection.execute("INSERT INTO facts(value) VALUES ('from-wal')")
            connection.commit()
            manager = BackupManager(
                backup_dir=root / "backups",
                sources={"workspace/chichu.sqlite3": database},
                key_provider=self.StaticKeyProvider(),
            )

            backup = manager.create_backup("manual")
            extracted = root / "restored.sqlite3"
            with self._open_encrypted_zip(manager, backup["name"]) as archive:
                extracted.write_bytes(archive.read("workspace/chichu.sqlite3"))
            with sqlite3.connect(extracted) as restored:
                value = restored.execute("SELECT value FROM facts").fetchone()[0]
            connection.close()

            self.assertEqual(value, "from-wal")

    def test_env_backup_keeps_secret_reference_but_removes_raw_api_keys(self):
        from qq_openclaw_bridge.backups import BackupManager

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_path = root / ".env"
            env_path.write_text(
                "OPENAI_API_KEY=openai-secret\n"
                "MEDIA_API_KEY=media-secret\n"
                "ACCESS_TOKEN=token-secret\n"
                "SMTP_PASSWORD=password-secret\n"
                "SERVICE_SECRET=service-secret\n"
                "MEDIA_API_SECRET_REF=keychain:qq-openclaw-media:media-api\n"
                "MEDIA_IMAGE_MODEL=image-model\n",
                encoding="utf-8",
            )
            manager = BackupManager(
                backup_dir=root / "backups",
                sources={"project/.env": env_path},
                key_provider=self.StaticKeyProvider(),
            )

            backup = manager.create_backup("manual")
            with self._open_encrypted_zip(manager, backup["name"]) as archive:
                archived_env = archive.read("project/.env").decode("utf-8")

            self.assertIn("OPENAI_API_KEY=\n", archived_env)
            self.assertIn("MEDIA_API_KEY=\n", archived_env)
            self.assertNotIn("openai-secret", archived_env)
            self.assertNotIn("media-secret", archived_env)
            self.assertNotIn("token-secret", archived_env)
            self.assertNotIn("password-secret", archived_env)
            self.assertNotIn("service-secret", archived_env)
            self.assertIn(
                "MEDIA_API_SECRET_REF=keychain:qq-openclaw-media:media-api",
                archived_env,
            )

    def test_default_backup_sources_include_e4_database_but_not_media_cache(self):
        from qq_openclaw_bridge.backups import create_backup_manager
        from qq_openclaw_bridge.config import Config

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={10001},
                fallback_reply="稍后再试",
                max_reply_chars=1800,
                housekeeper_memory_path=str(root / "MEMORY.md"),
                housekeeper_todo_path=str(root / "TODO.md"),
                style_memory_path=str(root / "STYLE.md"),
                reminder_state_path=str(root / "REMINDER_STATE.md"),
                state_database_path=str(root / "chichu.sqlite3"),
                media_cache_dir=str(root / "media-cache"),
                enable_sqlite_store=True,
            )

            sources = create_backup_manager(config).sources

            self.assertIn("workspace/chichu.sqlite3", sources)
            self.assertFalse(any("media-cache" in name for name in sources))
            self.assertFalse(any(path == root / "media-cache" for path in sources.values()))

    def test_tampered_encrypted_backup_is_rejected_before_manifest_parsing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, memory_path = self._manager(temp_dir, now)
            backup = manager.create_backup("manual")
            path = manager.backup_dir / backup["name"]
            tampered = bytearray(path.read_bytes())
            tampered[-1] ^= 1
            path.write_bytes(tampered)
            memory_path.write_text("current-safe-data\n", encoding="utf-8")

            verification = manager.verify_backup(backup["name"])
            self.assertFalse(verification["valid"])
            self.assertEqual(verification["error"], "加密备份认证失败")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                manager.restore_backup(backup["name"])
            self.assertEqual(memory_path.read_text(encoding="utf-8"), "current-safe-data\n")

    def test_legacy_zip_is_explicitly_classified_and_remains_restorable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, memory_path = self._manager(temp_dir, now)
            legacy = manager.backup_dir / "manual-legacy.zip"
            manager.backup_dir.mkdir(parents=True)
            content = b"legacy-memory\n"
            manifest = {
                "version": 1,
                "created_at": now[0].isoformat(),
                "kind": "manual",
                "files": {
                    "workspace/MEMORY.md": {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                },
            }
            with zipfile.ZipFile(legacy, "w") as archive:
                archive.writestr("workspace/MEMORY.md", content)
                archive.writestr("manifest.json", json.dumps(manifest))

            listed = {item["name"]: item for item in manager.list_backups()}
            self.assertEqual(listed[legacy.name]["encryption"], "legacy-unencrypted")
            manager.restore_backup(legacy.name)
            self.assertEqual(memory_path.read_bytes(), content)
            self.assertEqual(memory_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(
                any(
                    item["kind"] == "pre-restore"
                    and item["name"].endswith(".chichu-backup")
                    for item in manager.list_backups()
                )
            )

    def test_encryption_failure_leaves_no_plaintext_or_final_backup(self):
        from qq_openclaw_bridge.backups import BackupManager

        class FailingKeyProvider:
            def get_or_create(self):
                raise RuntimeError("key unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "MEMORY.md"
            source.write_text("must-not-remain-plaintext", encoding="utf-8")
            manager = BackupManager(
                backup_dir=root / "backups",
                sources={"workspace/MEMORY.md": source},
                key_provider=FailingKeyProvider(),
            )

            with self.assertRaises(RuntimeError):
                manager.create_backup("manual")

            remaining = [
                path
                for path in manager.backup_dir.iterdir()
                if path.name != ".backup.lock"
            ]
            self.assertEqual(remaining, [])
            self.assertFalse(
                any(
                    "must-not-remain-plaintext"
                    in path.read_text(errors="ignore")
                    for path in remaining
                )
            )

    def test_creation_rejects_sources_above_backup_limit_and_cleans_temporary(self):
        from unittest import mock
        from qq_openclaw_bridge.backups import BackupManager

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "MEMORY.md"
            source.write_bytes(b"x" * 65)
            manager = BackupManager(
                backup_dir=root / "backups",
                sources={"workspace/MEMORY.md": source},
                key_provider=self.StaticKeyProvider(),
            )

            with mock.patch(
                "qq_openclaw_bridge.backups._MAX_BACKUP_UNCOMPRESSED_BYTES", 64
            ), self.assertRaisesRegex(ValueError, "过大"):
                manager.create_backup("manual")

            remaining = [
                path
                for path in manager.backup_dir.iterdir()
                if path.name != ".backup.lock"
            ]
            self.assertEqual(remaining, [])

    def test_restore_failure_removes_plaintext_temporary_file(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, env_path, memory_path = self._manager(temp_dir, now)
            backup = manager.create_backup("manual")
            env_path.write_text("CURRENT_ENV=1\n", encoding="utf-8")
            memory_path.write_text("current-memory\n", encoding="utf-8")
            real_replace = os.replace
            failure = {"raised": False}

            def replace(source, destination):
                if Path(destination) == memory_path and not failure["raised"]:
                    failure["raised"] = True
                    raise OSError("restore failed")
                return real_replace(source, destination)

            with mock.patch(
                "qq_openclaw_bridge.backups.os.replace", side_effect=replace
            ):
                with self.assertRaises(OSError):
                    manager.restore_backup(backup["name"])

            self.assertEqual(env_path.read_text(encoding="utf-8"), "CURRENT_ENV=1\n")
            self.assertEqual(
                memory_path.read_text(encoding="utf-8"), "current-memory\n"
            )
            self.assertFalse(
                memory_path.with_name(memory_path.name + ".restore.tmp").exists()
            )
            self.assertEqual(
                list(memory_path.parent.glob(".%s.restore-*.tmp" % memory_path.name)),
                [],
            )

    def test_restore_staging_failure_cleans_all_new_and_rollback_temporaries(self):
        from unittest import mock
        import qq_openclaw_bridge.backups as backups_module

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, env_path, memory_path = self._manager(temp_dir, now)
            backup = manager.create_backup("manual")
            real_copy = backups_module._copy_secure_temporary
            calls = {"count": 0}

            def copy(destination, label):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("staging failed")
                return real_copy(destination, label)

            with mock.patch(
                "qq_openclaw_bridge.backups._copy_secure_temporary",
                side_effect=copy,
            ):
                with self.assertRaises(OSError):
                    manager.restore_backup(backup["name"])

            for destination in (env_path, memory_path):
                self.assertEqual(
                    list(destination.parent.glob(".%s.*-*.tmp" % destination.name)),
                    [],
                )

    def test_sqlite_sidecar_cleanup_failure_rolls_back_database_and_sidecars(self):
        from unittest import mock
        from qq_openclaw_bridge.backups import BackupManager

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "chichu.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE facts(value TEXT)")
                connection.execute("INSERT INTO facts(value) VALUES ('backup')")
                connection.commit()
            manager = BackupManager(
                backup_dir=root / "backups",
                sources={"workspace/chichu.sqlite3": database},
                key_provider=self.StaticKeyProvider(),
            )
            backup = manager.create_backup("manual")
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE facts SET value='current'")
                connection.commit()
            current_database = database.read_bytes()
            wal = Path(str(database) + "-wal")
            shm = Path(str(database) + "-shm")
            wal.write_bytes(b"current-wal")
            shm.write_bytes(b"current-shm")
            real_unlink = Path.unlink
            failed = {"value": False}

            def unlink(path, *args, **kwargs):
                if path == shm and not failed["value"]:
                    failed["value"] = True
                    raise OSError("sidecar cleanup failed")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                manager, "_create_backup_unlocked", return_value={"name": "pre"}
            ), mock.patch.object(Path, "unlink", side_effect=unlink, autospec=True):
                with self.assertRaises(OSError):
                    manager.restore_backup(backup["name"])

            self.assertEqual(database.read_bytes(), current_database)
            self.assertEqual(wal.read_bytes(), b"current-wal")
            self.assertEqual(shm.read_bytes(), b"current-shm")

    def test_keychain_unavailable_marks_backup_invalid_without_breaking_list(self):
        class FailingKeyProvider:
            calls = 0

            def get_or_create(self):
                self.calls += 1
                raise RuntimeError("locked")

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now)
            backups = []
            for _ in range(3):
                backups.append(manager.create_backup("manual"))
                now[0] += timedelta(microseconds=1)
            failing_provider = FailingKeyProvider()
            manager._key_provider = failing_provider

            listed = manager.list_backups()

            self.assertEqual(len(listed), 3)
            self.assertEqual(
                {item["name"] for item in listed},
                {item["name"] for item in backups},
            )
            self.assertTrue(
                all(item["encryption"] == "encrypted" for item in listed)
            )
            self.assertTrue(all(not item["valid"] for item in listed))
            self.assertEqual(failing_provider.calls, 1)

    def test_unauthenticated_future_legacy_auto_does_not_block_new_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now)
            self._write_legacy_backup(
                manager,
                "auto-forged.zip",
                "auto",
                "9999-12-31T00:00:00+00:00",
            )

            created = manager.run_if_due()

            self.assertIsNotNone(created)
            self.assertTrue(created["name"].endswith(".chichu-backup"))

    def test_legacy_timestamp_with_z_suffix_remains_compatible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now)
            legacy = self._write_legacy_backup(
                manager,
                "manual-zulu.zip",
                "manual",
                "2026-07-14T10:00:00Z",
            )

            verification = manager.verify_backup(legacy.name)

            self.assertTrue(verification["valid"])

    def test_corrupt_encrypted_auto_backups_still_follow_retention_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now, max_auto=1)
            names = []
            for _ in range(3):
                names.append(manager.create_backup("auto")["name"])
                now[0] += timedelta(hours=1)
            for name in names:
                path = manager.backup_dir / name
                damaged = bytearray(path.read_bytes())
                damaged[-1] ^= 1
                path.write_bytes(damaged)

            manager.prune()

            self.assertLessEqual(
                len(list(manager.backup_dir.glob("auto-*.chichu-backup"))), 1
            )

    def test_next_operation_cleans_stale_plaintext_zip_temporary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now)
            manager.backup_dir.mkdir(parents=True)
            stale = manager.backup_dir / ".manual-stale.zip.tmp"
            stale.write_text("plaintext-secret", encoding="utf-8")

            manager.list_backups()

            self.assertFalse(stale.exists())

    def test_oversized_backup_file_and_zip_payload_are_rejected_before_expansion(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            manager, _, _ = self._manager(temp_dir, now)
            oversized_file = manager.backup_dir / "manual-oversized.zip"
            manager.backup_dir.mkdir(parents=True)
            oversized_file.write_bytes(b"x" * 65)

            with mock.patch("qq_openclaw_bridge.backups._MAX_BACKUP_FILE_BYTES", 64):
                verification = manager.verify_backup(oversized_file.name)
            self.assertFalse(verification["valid"])
            self.assertEqual(verification["error"], "备份文件过大")

            legacy = self._write_legacy_backup(
                manager,
                "manual-expanded.zip",
                "manual",
                now[0].isoformat(),
                content=b"y" * 128,
            )
            with mock.patch(
                "qq_openclaw_bridge.backups._MAX_BACKUP_UNCOMPRESSED_BYTES", 64
            ):
                verification = manager.verify_backup(legacy.name)
            self.assertFalse(verification["valid"])
            self.assertEqual(verification["error"], "备份展开后过大")


if __name__ == "__main__":
    unittest.main()
