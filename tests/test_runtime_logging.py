import json
import io
import logging
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_openclaw_bridge.runtime.logging import configure_process_logging


class RuntimeLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temporary_directory.name) / "logs"

    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_chichu_process_handler", False):
                root.removeHandler(handler)
                handler.close()
        self.temporary_directory.cleanup()

    def test_logging_rotates_at_configured_size_and_hardens_files(self):
        logger = configure_process_logging("bridge", self.log_dir, "info", 512, 2)

        for index in range(100):
            logger.info("line-%03d %s", index, "x" * 40)
        for handler in logger.handlers:
            handler.flush()

        paths = list(self.log_dir.glob("bridge.log*"))
        self.assertLessEqual(len(paths), 3)
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_formatter_emits_fixed_json_fields_and_approved_identifiers_only(self):
        logger = configure_process_logging("bridge", self.log_dir, "info", 4096, 1)

        logger.info(
            "operation_complete",
            extra={
                "run_id": "run-1",
                "task_id": "task-2",
                "api_key": "sk-secret",
                "password": "also-secret",
                "arbitrary": {"nested": "private"},
            },
        )
        for handler in logger.handlers:
            handler.flush()

        payload = json.loads((self.log_dir / "bridge.log").read_text(encoding="utf-8"))
        self.assertEqual(payload["component"], "bridge")
        self.assertEqual(payload["level"], "info")
        self.assertEqual(payload["message"], "operation_complete")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["task_id"], "task-2")
        self.assertRegex(payload["timestamp"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertEqual(
            set(payload),
            {"timestamp", "component", "level", "message", "run_id", "task_id"},
        )
        serialized = json.dumps(payload)
        self.assertNotIn("sk-secret", serialized)
        self.assertNotIn("also-secret", serialized)
        self.assertNotIn("private", serialized)

    def test_formatter_redacts_known_secret_patterns_in_message(self):
        logger = configure_process_logging("bridge", self.log_dir, "info", 4096, 1)

        logger.info(
            "request Authorization: Bearer bearer-secret api_key=sk-message-secret"
        )
        for handler in logger.handlers:
            handler.flush()

        serialized = (self.log_dir / "bridge.log").read_text(encoding="utf-8")
        self.assertNotIn("bearer-secret", serialized)
        self.assertNotIn("sk-message-secret", serialized)
        self.assertGreaterEqual(serialized.count("[REDACTED]"), 2)

    def test_reconfiguration_replaces_owned_handler_instead_of_duplicating(self):
        first = configure_process_logging("bridge", self.log_dir, "info", 4096, 1)
        second = configure_process_logging("bridge", self.log_dir, "debug", 4096, 1)

        owned = [
            handler
            for handler in second.handlers
            if getattr(handler, "_chichu_process_handler", False)
        ]
        self.assertIs(first, second)
        self.assertEqual(len(owned), 1)

    def test_configuration_removes_preexisting_handlers_that_could_leak(self):
        capture = io.StringIO()
        unsafe_handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        root.addHandler(unsafe_handler)

        try:
            logger = configure_process_logging(
                "bridge", self.log_dir, "info", 4096, 1
            )
            logger.info("sensitive-message")
            self.assertNotIn(unsafe_handler, logger.handlers)
            self.assertEqual(capture.getvalue(), "")
        finally:
            if unsafe_handler in root.handlers:
                root.removeHandler(unsafe_handler)
            unsafe_handler.close()

    def test_failed_reconfiguration_preserves_existing_handler(self):
        logger = configure_process_logging("bridge", self.log_dir, "info", 4096, 1)
        original = tuple(logger.handlers)

        with mock.patch(
            "qq_openclaw_bridge.runtime.logging._HardenedRotatingFileHandler",
            side_effect=OSError("disk-secret"),
        ):
            with self.assertRaises(OSError):
                configure_process_logging("bridge", self.log_dir, "debug", 4096, 1)

        self.assertEqual(tuple(logger.handlers), original)
        self.assertFalse(original[0].stream.closed)

    def test_rejects_component_names_that_could_escape_log_directory(self):
        for component in ("../bridge", "bridge/log", "", "."):
            with self.subTest(component=component), self.assertRaises(ValueError):
                configure_process_logging(component, self.log_dir, "info", 4096, 1)

    def test_rejects_invalid_rotation_and_level_values(self):
        invalid = (
            {"level": "verbose", "max_bytes": 4096, "backup_count": 1},
            {"level": "info", "max_bytes": 0, "backup_count": 1},
            {"level": "info", "max_bytes": 4096, "backup_count": 0},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                configure_process_logging("bridge", self.log_dir, **values)


if __name__ == "__main__":
    unittest.main()
