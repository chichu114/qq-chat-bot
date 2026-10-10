from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class WatchdogTests(unittest.TestCase):
    def _watchdog(self, root, now, health, runner=None, notifier=None, **overrides):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        project = Path(root)
        (project / "logs").mkdir(exist_ok=True)
        (project / "scripts").mkdir(exist_ok=True)
        (project / "scripts" / "bridge_service.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        settings = WatchdogSettings(
            check_interval_seconds=30,
            failure_threshold=overrides.get("failure_threshold", 3),
            restart_cooldown_seconds=overrides.get("restart_cooldown_seconds", 300),
            max_restarts=overrides.get("max_restarts", 3),
            restart_window_seconds=overrides.get("restart_window_seconds", 1800),
            notifications=True,
        )
        return Watchdog(
            project_dir=project,
            settings=settings,
            bridge_probe=lambda: health["bridge"],
            onebot_probe=lambda: health["onebot"],
            proxy_probe=lambda: health["proxy"],
            openclaw_probe=lambda: health.get("openclaw", True),
            queue_probe=lambda: health.get("queue", True),
            routing_probe=lambda: health.get("routing", True),
            runner=runner,
            notifier=notifier,
            now_provider=lambda: now[0],
        )

    def test_healthy_snapshot_is_written_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            health = {"bridge": True, "onebot": True, "proxy": True, "openclaw": True}
            watchdog = self._watchdog(temp_dir, now, health)

            state = watchdog.check_once()
            serialized = json.dumps(state, ensure_ascii=False)

            self.assertEqual(state["status"], "healthy")
            self.assertTrue(state["checks"]["bridge"])
            self.assertTrue(state["checks"]["queue"])
            self.assertNotIn("API_KEY", serialized)
            self.assertTrue((Path(temp_dir) / "logs" / "health.json").is_file())

    def test_bridge_restarts_only_after_failure_threshold(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return type("Result", (), {"returncode": 0, "stdout": "started", "stderr": ""})()

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            health = {"bridge": False, "onebot": True, "proxy": True}
            watchdog = self._watchdog(
                temp_dir, now, health, runner=runner, failure_threshold=2
            )

            first = watchdog.check_once()
            second = watchdog.check_once()

            self.assertEqual(first["restart_count"], 0)
            self.assertEqual(second["status"], "recovering")
            self.assertEqual(second["restart_count"], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-1], "start")

    def test_external_dependency_failure_does_not_restart_bridge(self):
        calls = []
        notifications = []

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            health = {"bridge": False, "onebot": False, "proxy": True}
            watchdog = self._watchdog(
                temp_dir,
                now,
                health,
                runner=lambda *args, **kwargs: calls.append(args),
                notifier=lambda title, message: notifications.append((title, message)),
                failure_threshold=1,
            )

            state = watchdog.check_once()

            self.assertEqual(state["status"], "degraded")
            self.assertIn("OneBot", state["message"])
            self.assertEqual(calls, [])
            self.assertEqual(len(notifications), 1)

    def test_routing_degradation_does_not_restart_healthy_bridge(self):
        calls = []

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            health = {
                "bridge": True,
                "onebot": True,
                "proxy": True,
                "openclaw": True,
                "queue": True,
                "routing": False,
            }
            state = self._watchdog(
                temp_dir,
                now,
                health,
                runner=lambda *args, **kwargs: calls.append(args),
                failure_threshold=1,
            ).check_once()

            self.assertEqual(state["status"], "degraded")
            self.assertFalse(state["checks"]["routing"])
            self.assertEqual(state["restart_count"], 0)
            self.assertEqual(calls, [])

    def test_routing_failure_does_not_block_restart_for_failed_bridge(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return type("Result", (), {"returncode": 0})()

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 8, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            health = {
                "bridge": False,
                "onebot": True,
                "proxy": True,
                "openclaw": True,
                "queue": True,
                "routing": False,
            }
            watchdog = self._watchdog(
                temp_dir, now, health, runner=runner, failure_threshold=2
            )

            watchdog.check_once()
            restarted = watchdog.check_once()

        self.assertEqual(restarted["status"], "recovering")
        self.assertEqual(restarted["restart_count"], 1)
        self.assertEqual(len(calls), 1)

    def test_restart_limit_blocks_looping_restarts(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return type("Result", (), {"returncode": 0, "stdout": "started", "stderr": ""})()

        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            health = {"bridge": False, "onebot": True, "proxy": True}
            watchdog = self._watchdog(
                temp_dir,
                now,
                health,
                runner=runner,
                failure_threshold=1,
                restart_cooldown_seconds=60,
                max_restarts=1,
            )

            watchdog.check_once()
            now[0] += timedelta(seconds=61)
            blocked = watchdog.check_once()

            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(len(calls), 1)
            self.assertIn("上限", blocked["message"])

    def test_recovery_resets_failures_and_sends_single_transition_notice(self):
        notices = []
        with tempfile.TemporaryDirectory() as temp_dir:
            now = [datetime(2026, 7, 14, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
            health = {"bridge": True, "onebot": False, "proxy": True}
            watchdog = self._watchdog(
                temp_dir,
                now,
                health,
                notifier=lambda title, message: notices.append((title, message)),
            )

            watchdog.check_once()
            watchdog.check_once()
            health["onebot"] = True
            recovered = watchdog.check_once()

            self.assertEqual(recovered["status"], "healthy")
            self.assertEqual(len(notices), 2)
            self.assertIn("恢复", notices[-1][1])

    def test_openclaw_probe_checks_gateway_port_in_gateway_mode(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            (project / ".env").write_text(
                "OPENCLAW_USE_GATEWAY=true\n"
                "OPENCLAW_GATEWAY_PROBE_URL=ws://127.0.0.1:18789\n",
                encoding="utf-8",
            )
            watchdog = Watchdog(project, WatchdogSettings())

            with mock.patch(
                "qq_openclaw_watchdog.watchdog._probe_url", return_value=True
            ) as probe:
                self.assertTrue(watchdog._probe_openclaw())

            probe.assert_called_once_with("ws://127.0.0.1:18789", default_port=18789)

    def test_failed_outbox_marks_queue_dependency_unhealthy(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            workspace = project / "workspace"
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            workspace.mkdir()
            database = workspace / "chichu.sqlite3"
            store = WorkspaceStateStore(database)
            row = store.enqueue_outbox(
                kind="reply",
                user_id=10001,
                message="失败",
                dedupe_key="reply:failed",
            )
            claimed = store.claim_due_outbox(max_attempts=1)
            store.mark_outbox_retry(
                claimed["id"], claimed["claim_token"], "failed", 1, 1
            )
            (project / ".env").write_text(
                "ENABLE_SQLITE_STORE=true\nSTATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )
            watchdog = Watchdog(project, WatchdogSettings())

            self.assertEqual(row["status"], "pending")
            self.assertFalse(watchdog._probe_queue())


if __name__ == "__main__":
    unittest.main()
