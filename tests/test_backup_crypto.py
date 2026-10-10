import base64
from pathlib import Path
import sys
import tempfile
import threading
import traceback
from unittest import mock
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class BackupCryptoTests(unittest.TestCase):
    def test_approval_binding_provider_derives_domain_separated_stable_key(self):
        from qq_openclaw_bridge.runtime.secrets import ApprovalBindingKeyProvider

        class FakeBackupKeyProvider:
            def __init__(self, key):
                self._key = key

            def get_or_create(self):
                return self._key

        first = ApprovalBindingKeyProvider(
            backup_key_provider=FakeBackupKeyProvider(b"m" * 32)
        ).get()
        second = ApprovalBindingKeyProvider(
            backup_key_provider=FakeBackupKeyProvider(b"m" * 32)
        ).get()
        different = ApprovalBindingKeyProvider(
            backup_key_provider=FakeBackupKeyProvider(b"n" * 32)
        ).get()

        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertNotEqual(first, b"m" * 32)

    def test_envelope_round_trip_and_random_nonce(self):
        from qq_openclaw_bridge.runtime.backup_crypto import BackupEnvelope

        key = bytes(range(32))
        first = BackupEnvelope.encrypt(b"payload", key)
        second = BackupEnvelope.encrypt(b"payload", key)

        self.assertNotEqual(first, second)
        self.assertEqual(BackupEnvelope.decrypt(first, key), b"payload")
        self.assertEqual(BackupEnvelope.decrypt(second, key), b"payload")

    def test_tamper_and_wrong_key_fail_closed_without_sensitive_chain(self):
        from qq_openclaw_bridge.runtime.backup_crypto import (
            BackupEnvelope,
            BackupIntegrityError,
        )

        encrypted = bytearray(BackupEnvelope.encrypt(b"payload", b"a" * 32))
        encrypted[-1] ^= 1
        cases = (
            (bytes(encrypted), b"a" * 32),
            (BackupEnvelope.encrypt(b"payload", b"a" * 32), b"b" * 32),
        )
        for ciphertext, key in cases:
            with self.subTest(key=key[:1]), self.assertRaises(
                BackupIntegrityError
            ) as caught:
                BackupEnvelope.decrypt(ciphertext, key)
            formatted = "".join(
                traceback.format_exception(
                    type(caught.exception),
                    caught.exception,
                    caught.exception.__traceback__,
                )
            )
            self.assertEqual(str(caught.exception), "backup integrity check failed")
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("payload", formatted)

    def test_encryption_random_source_failure_is_sanitized(self):
        from qq_openclaw_bridge.runtime.backup_crypto import (
            BackupEnvelope,
            BackupIntegrityError,
        )

        with mock.patch(
            "qq_openclaw_bridge.runtime.backup_crypto.os.urandom",
            side_effect=RuntimeError("random-source-secret"),
        ), self.assertRaises(BackupIntegrityError) as caught:
            BackupEnvelope.encrypt(b"payload", b"a" * 32)

        formatted = "".join(
            traceback.format_exception(
                type(caught.exception),
                caught.exception,
                caught.exception.__traceback__,
            )
        )
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("random-source-secret", formatted)

    def test_key_provider_reads_or_creates_fixed_keychain_entry(self):
        from qq_openclaw_bridge.runtime.secrets import BackupKeyProvider

        calls = []
        encoded = base64.b64encode(b"k" * 32).decode("ascii")
        reads = 0

        def runner(command, **kwargs):
            nonlocal reads
            calls.append((command, kwargs))
            if command[1] == "find-generic-password":
                reads += 1
                return type(
                    "Result",
                    (),
                    {
                        "returncode": 44 if reads == 1 else 0,
                        "stdout": "" if reads == 1 else encoded + "\n",
                    },
                )()
            return type("Result", (), {"returncode": 0, "stdout": ""})()

        provider = BackupKeyProvider(
            runner=runner,
            random_bytes=lambda size: b"k" * size,
            timeout_seconds=7,
        )

        self.assertEqual(provider.get_or_create(), b"k" * 32)
        self.assertEqual(
            calls[0][0],
            [
                "/usr/bin/security",
                "find-generic-password",
                "-w",
                "-s",
                "qq-openclaw-backup",
                "-a",
                "backup-master-v1",
            ],
        )
        self.assertEqual(calls[1][0], [
            "/usr/bin/security",
            "add-generic-password",
            "-s",
            "qq-openclaw-backup",
            "-a",
            "backup-master-v1",
            "-w",
        ])
        self.assertEqual(base64.b64decode(calls[1][1]["input"].strip()), b"k" * 32)
        self.assertNotIn(calls[1][1]["input"].strip(), calls[1][0])
        self.assertEqual(calls[2][0][1], "find-generic-password")
        for _, kwargs in calls:
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 7)

    def test_key_provider_rejects_successful_add_when_readback_is_empty(self):
        from qq_openclaw_bridge.runtime.secrets import (
            BackupKeyProvider,
            SecretResolutionError,
        )

        calls = 0

        def runner(command, **kwargs):
            nonlocal calls
            calls += 1
            if command[1] == "find-generic-password":
                return type(
                    "Result",
                    (),
                    {"returncode": 44 if calls == 1 else 0, "stdout": ""},
                )()
            return type("Result", (), {"returncode": 0, "stdout": ""})()

        with self.assertRaises(SecretResolutionError):
            BackupKeyProvider(
                runner=runner, random_bytes=lambda size: b"k" * size
            ).get_or_create()

    def test_key_provider_rejects_invalid_stored_material_without_overwriting(self):
        from qq_openclaw_bridge.runtime.secrets import (
            BackupKeyProvider,
            SecretResolutionError,
        )

        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return type("Result", (), {"stdout": "not-base64\n"})()

        with self.assertRaises(SecretResolutionError) as caught:
            BackupKeyProvider(runner=runner).get_or_create()

        self.assertEqual(str(caught.exception), "backup key resolution failed")
        self.assertEqual(len(calls), 1)

    def test_key_provider_returns_existing_key_without_writing(self):
        from qq_openclaw_bridge.runtime.secrets import BackupKeyProvider

        calls = []
        encoded = base64.b64encode(b"e" * 32).decode("ascii")

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return type(
                "Result", (), {"returncode": 0, "stdout": encoded + "\n"}
            )()

        provider = BackupKeyProvider(runner=runner)
        self.assertEqual(provider.get_or_create(), b"e" * 32)
        self.assertEqual(provider.get_or_create(), b"e" * 32)
        self.assertEqual(len(calls), 1)

    def test_key_provider_random_source_failure_is_sanitized(self):
        from qq_openclaw_bridge.runtime.secrets import (
            BackupKeyProvider,
            SecretResolutionError,
        )

        def runner(command, **kwargs):
            del command, kwargs
            return type("Result", (), {"returncode": 44, "stdout": ""})()

        with self.assertRaises(SecretResolutionError) as caught:
            BackupKeyProvider(
                runner=runner,
                random_bytes=lambda size: (_ for _ in ()).throw(
                    RuntimeError("random-key-secret")
                ),
            ).get_or_create()

        formatted = "".join(
            traceback.format_exception(
                type(caught.exception),
                caught.exception,
                caught.exception.__traceback__,
            )
        )
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("random-key-secret", formatted)

    def test_concurrent_first_creation_returns_one_persisted_key(self):
        from qq_openclaw_bridge.runtime.secrets import BackupKeyProvider

        barrier = threading.Barrier(2)
        storage = {"value": None}
        storage_lock = threading.Lock()

        def runner(command, **kwargs):
            if command[1] == "find-generic-password":
                with storage_lock:
                    value = storage["value"]
                if value is None:
                    barrier.wait(timeout=2)
                    return type("Result", (), {"returncode": 44, "stdout": ""})()
                return type(
                    "Result", (), {"returncode": 0, "stdout": value + "\n"}
                )()
            candidate = kwargs["input"].strip()
            with storage_lock:
                if storage["value"] is None:
                    storage["value"] = candidate
                    return type("Result", (), {"returncode": 0, "stdout": ""})()
            return type("Result", (), {"returncode": 45, "stdout": ""})()

        providers = (
            BackupKeyProvider(runner=runner, random_bytes=lambda size: b"a" * size),
            BackupKeyProvider(runner=runner, random_bytes=lambda size: b"b" * size),
        )
        results = [None, None]
        errors = []

        def resolve(index):
            try:
                results[index] = providers[index].get_or_create()
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=resolve, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], base64.b64decode(storage["value"]))

    def test_requirements_and_pyproject_include_same_pdf_and_crypto_bounds(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        for dependency in ("pypdf>=4,<7", "cryptography>=42,<47"):
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, pyproject)
                self.assertIn(dependency, requirements)


if __name__ == "__main__":
    unittest.main()
