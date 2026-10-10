from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class WatchdogProbeTests(unittest.TestCase):
    @staticmethod
    def _routing_config(classifier_enabled=False, timeout_seconds=15, cooldown_seconds=300):
        return {
            "enabled": True,
            "classifier_enabled": classifier_enabled,
            "classifier_model": "openai/gpt-5.4-nano",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": timeout_seconds,
            "max_daily_calls": 100,
            "failure_threshold": 3,
            "cooldown_seconds": cooldown_seconds,
            "rule_version": "f1-v1",
        }

    def _routing_watchdog(self, project, now):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        return Watchdog(
            project,
            WatchdogSettings(notifications=False),
            bridge_probe=lambda: True,
            onebot_probe=lambda: True,
            proxy_probe=lambda: True,
            openclaw_probe=lambda: True,
            queue_probe=lambda: True,
            now_provider=lambda: now[0],
        )

    def test_routing_health_is_disabled_by_default(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            state = Watchdog(
                project,
                WatchdogSettings(notifications=False),
                bridge_probe=lambda: True,
                onebot_probe=lambda: True,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
            ).check_once()

        self.assertEqual(state["checks"].get("routing"), True)

    def test_enabled_routing_requires_one_active_config(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            database = project / "state.sqlite3"
            WorkspaceStateStore(database)
            (project / ".env").write_text(
                "ENABLE_SHADOW_ROUTING=true\nSTATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )
            now = [datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)]

            state = self._routing_watchdog(project, now).check_once()

        self.assertEqual(state["checks"].get("routing"), False)

    def test_enabled_routing_rejects_pending_decisions_older_than_timeout_plus_sixty_seconds(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            database = project / "state.sqlite3"
            store = WorkspaceStateStore(database)
            active = store.routing_repository.create_config(
                self._routing_config(timeout_seconds=10), activate=True
            )
            now = [datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)]
            store.routing_repository.start_decision(
                user_id=10001,
                source_message_sha256="a" * 64,
                message_sha256="b" * 64,
                message_preview="bounded",
                routing_config_version=active["version"],
                rule_version="f1-v1",
                dispatch_overhead_ms=0,
                now=now[0] - timedelta(seconds=71),
            )
            (project / ".env").write_text(
                "ENABLE_SHADOW_ROUTING=true\nSTATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )

            state = self._routing_watchdog(project, now).check_once()

        self.assertEqual(state["checks"].get("routing"), False)

    def test_retired_config_stale_pending_does_not_degrade_active_routing_health(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            database = project / "state.sqlite3"
            store = WorkspaceStateStore(database)
            retired = store.routing_repository.create_config(
                self._routing_config(timeout_seconds=10, classifier_enabled=False),
                activate=True,
            )
            now = [datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)]
            store.routing_repository.start_decision(
                user_id=10001,
                source_message_sha256="c" * 64,
                message_sha256="d" * 64,
                message_preview="bounded",
                routing_config_version=retired["version"],
                rule_version="f1-v1",
                dispatch_overhead_ms=0,
                now=now[0] - timedelta(seconds=71),
            )
            store.routing_repository.create_config(
                self._routing_config(timeout_seconds=10, classifier_enabled=False),
                activate=True,
                force_new=True,
            )
            (project / ".env").write_text(
                "ENABLE_SHADOW_ROUTING=true\nSTATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )

            state = self._routing_watchdog(project, now).check_once()

        self.assertEqual(state["checks"].get("routing"), True)

    def test_active_pending_is_healthy_at_cutoff_and_unhealthy_after_cutoff(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            database = project / "state.sqlite3"
            store = WorkspaceStateStore(database)
            active = store.routing_repository.create_config(
                self._routing_config(timeout_seconds=10, classifier_enabled=False),
                activate=True,
            )
            now = [datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)]
            store.routing_repository.start_decision(
                user_id=10001,
                source_message_sha256="e" * 64,
                message_sha256="f" * 64,
                message_preview="bounded",
                routing_config_version=active["version"],
                rule_version="f1-v1",
                dispatch_overhead_ms=0,
                now=now[0] - timedelta(seconds=70),
            )
            (project / ".env").write_text(
                "ENABLE_SHADOW_ROUTING=true\nSTATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )

            at_cutoff = self._routing_watchdog(project, now).check_once()
            store.routing_repository.start_decision(
                user_id=10001,
                source_message_sha256="a" * 64,
                message_sha256="b" * 64,
                message_preview="bounded",
                routing_config_version=active["version"],
                rule_version="f1-v1",
                dispatch_overhead_ms=0,
                now=now[0] - timedelta(seconds=71),
            )
            after_cutoff = self._routing_watchdog(project, now).check_once()

        self.assertEqual(at_cutoff["checks"].get("routing"), True)
        self.assertEqual(after_cutoff["checks"].get("routing"), False)

    def test_classifier_cooling_is_effectively_closed_after_one_configured_cooldown(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            database = project / "state.sqlite3"
            store = WorkspaceStateStore(database)
            active = store.routing_repository.create_config(
                self._routing_config(classifier_enabled=True, cooldown_seconds=300),
                activate=True,
            )
            now = [datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)]
            store.routing_repository.record_classifier_circuit(
                active["version"], "cooling", now=now[0] - timedelta(seconds=300)
            )
            (project / ".env").write_text(
                "ENABLE_SHADOW_ROUTING=true\nSTATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )

            healthy = self._routing_watchdog(project, now).check_once()
            store.routing_repository.record_classifier_circuit(
                active["version"], "cooling", now=now[0] - timedelta(seconds=301)
            )
            unhealthy = self._routing_watchdog(project, now).check_once()

        self.assertEqual(healthy["checks"].get("routing"), True)
        self.assertEqual(unhealthy["checks"].get("routing"), True)

    def test_launch_agent_probe_uses_fixed_bridge_label_and_running_state(self):
        from qq_openclaw_watchdog.probes import LaunchAgentProbe

        calls = []

        class Result:
            returncode = 0
            stdout = "state = running\npid = 12345\n"

        def runner(command, **kwargs):
            calls.append(command)
            return Result()

        probe = LaunchAgentProbe(
            "com.chichu.qq-bridge", runner=runner, uid=501
        )

        self.assertTrue(probe.run())
        self.assertEqual(
            calls,
            [["/bin/launchctl", "print", "gui/501/com.chichu.qq-bridge"]],
        )
        with self.assertRaises(ValueError):
            LaunchAgentProbe("local.untrusted", runner=runner, uid=501)

    def test_onebot_status_probe_rejects_responsive_but_offline_session(self):
        from qq_openclaw_watchdog.probes import OneBotStatusProbe

        probe = OneBotStatusProbe(
            "ws://127.0.0.1:3001",
            status_provider=lambda _url, _timeout: {
                "status": "ok",
                "retcode": 0,
                "data": {"online": False, "good": True, "stat": {}},
            },
        )

        self.assertFalse(probe.run())

    def test_onebot_status_probe_accepts_online_good_session(self):
        from qq_openclaw_watchdog.probes import OneBotStatusProbe

        probe = OneBotStatusProbe(
            "ws://127.0.0.1:3001",
            status_provider=lambda _url, _timeout: {
                "status": "ok",
                "retcode": 0,
                "data": {"online": True, "good": True, "stat": {}},
            },
        )

        self.assertTrue(probe.run())

    def test_napcat_restarter_authenticates_then_restarts_process(self):
        from qq_openclaw_watchdog.recovery import NapCatRestarter

        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "webui.json"
            config.write_text(
                json.dumps({"token": "test-secret"}), encoding="utf-8"
            )
            requests = []

            def requester(request, timeout):
                requests.append((request, timeout))
                if request.full_url.endswith("/api/auth/login"):
                    return json.dumps(
                        {
                            "code": 0,
                            "message": "success",
                            "data": {"Credential": "credential-1"},
                        }
                    ).encode("utf-8")
                return json.dumps(
                    {"code": 0, "message": "success", "data": None}
                ).encode("utf-8")

            recovered = NapCatRestarter(
                "http://127.0.0.1:6099",
                config,
                requester=requester,
            ).run()

        self.assertTrue(recovered)
        self.assertEqual(
            [request.full_url for request, _timeout in requests],
            [
                "http://127.0.0.1:6099/api/auth/login",
                "http://127.0.0.1:6099/api/Process/Restart",
            ],
        )
        login_body = json.loads(requests[0][0].data.decode("utf-8"))
        self.assertEqual(
            login_body,
            {
                "hash": "1f3d967541cd81ff08b8baaab6f8616b91a1fd2f8702afe59738750c62dfec7b",
                "totpCode": "",
            },
        )
        self.assertEqual(
            requests[1][0].get_header("Authorization"),
            "Bearer credential-1",
        )

    def test_napcat_restarter_rejects_malformed_login_data_without_raising(self):
        from qq_openclaw_watchdog.recovery import NapCatRestarter

        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "webui.json"
            config.write_text(
                json.dumps({"token": "test-secret"}), encoding="utf-8"
            )

            recovered = NapCatRestarter(
                "http://127.0.0.1:6099",
                config,
                requester=lambda _request, _timeout: json.dumps(
                    {"code": 0, "message": "success", "data": [1]}
                ).encode("utf-8"),
            ).run()

        self.assertFalse(recovered)

    def test_napcat_restarter_rejects_non_loopback_target_before_reading_token(self):
        from qq_openclaw_watchdog.recovery import NapCatRestarter

        requests = []
        recovered = NapCatRestarter(
            "https://example.com",
            Path("/does/not/need/to/exist"),
            requester=lambda request, _timeout: requests.append(request) or b"{}",
        ).run()

        self.assertFalse(recovered)
        self.assertEqual(requests, [])

    def test_napcat_restarter_does_not_follow_http_redirects(self):
        from qq_openclaw_watchdog.recovery import NapCatRestarter

        paths = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                paths.append(self.path)
                if self.path == "/api/auth/login":
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        "http://127.0.0.1:%d/redirected" % self.server.server_port,
                    )
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":0,"data":null}')

            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"code":0,"data":{"Credential":"redirected"}}'
                )

            def log_message(self, _format, *_args):
                return

        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "webui.json"
            config.write_text(
                json.dumps({"token": "test-secret"}), encoding="utf-8"
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                recovered = NapCatRestarter(
                    "http://127.0.0.1:%d" % server.server_port,
                    config,
                ).run()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertFalse(recovered)
        self.assertEqual(paths, ["/api/auth/login"])

    def test_watchdog_recovers_onebot_after_threshold_and_honors_cooldown(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            now = [datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)]
            recovery_calls = []
            watchdog = Watchdog(
                project,
                WatchdogSettings(
                    notifications=False,
                    onebot_failure_threshold=2,
                    onebot_auto_recovery=True,
                    restart_cooldown_seconds=300,
                ),
                bridge_probe=lambda: True,
                onebot_probe=lambda: False,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
                routing_probe=lambda: True,
                now_provider=lambda: now[0],
                onebot_restarter=lambda: recovery_calls.append(now[0]) or True,
            )

            first = watchdog.check_once()
            now[0] += timedelta(seconds=10)
            second = watchdog.check_once()
            now[0] += timedelta(seconds=10)
            third = watchdog.check_once()

        self.assertEqual(first["status"], "degraded")
        self.assertEqual(second["status"], "recovering")
        self.assertEqual(second["onebot_restart_count"], 1)
        self.assertEqual(third["status"], "degraded")
        self.assertIn("冷却", third["message"])
        self.assertEqual(len(recovery_calls), 1)

    def test_watchdog_blocks_onebot_restarts_after_window_limit(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            now = [datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)]
            recovery_calls = []
            watchdog = Watchdog(
                project,
                WatchdogSettings(
                    notifications=False,
                    onebot_failure_threshold=1,
                    onebot_auto_recovery=True,
                    restart_cooldown_seconds=10,
                    max_restarts=1,
                    restart_window_seconds=1800,
                ),
                bridge_probe=lambda: True,
                onebot_probe=lambda: False,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
                routing_probe=lambda: True,
                now_provider=lambda: now[0],
                onebot_restarter=lambda: recovery_calls.append(now[0]) or True,
            )

            first = watchdog.check_once()
            now[0] += timedelta(seconds=11)
            blocked = watchdog.check_once()

        self.assertEqual(first["status"], "recovering")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["onebot_restart_count"], 1)
        self.assertEqual(len(recovery_calls), 1)

    def test_watchdog_onebot_block_survives_window_until_online(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            now = [datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)]
            online = [False]
            recovery_calls = []
            watchdog = Watchdog(
                project,
                WatchdogSettings(
                    notifications=False,
                    onebot_failure_threshold=1,
                    onebot_auto_recovery=True,
                    restart_cooldown_seconds=1,
                    max_restarts=1,
                    restart_window_seconds=10,
                ),
                bridge_probe=lambda: True,
                onebot_probe=lambda: online[0],
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
                routing_probe=lambda: True,
                now_provider=lambda: now[0],
                onebot_restarter=lambda: recovery_calls.append(now[0]) or True,
            )

            watchdog.check_once()
            now[0] += timedelta(seconds=2)
            watchdog.check_once()
            now[0] += timedelta(seconds=11)
            still_blocked = watchdog.check_once()
            online[0] = True
            recovered = watchdog.check_once()

        self.assertEqual(still_blocked["status"], "blocked")
        self.assertTrue(still_blocked["onebot_recovery_blocked"])
        self.assertEqual(len(recovery_calls), 1)
        self.assertEqual(recovered["status"], "healthy")
        self.assertFalse(recovered["onebot_recovery_blocked"])

    def test_watchdog_persists_onebot_attempt_before_external_restart(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            reserved_counts = []

            def recover():
                state = json.loads(
                    (project / "logs" / "health.json").read_text(
                        encoding="utf-8"
                    )
                )
                reserved_counts.append(state["onebot_restart_count"])
                return True

            state = Watchdog(
                project,
                WatchdogSettings(
                    notifications=False,
                    onebot_failure_threshold=1,
                    onebot_auto_recovery=True,
                ),
                bridge_probe=lambda: True,
                onebot_probe=lambda: False,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
                routing_probe=lambda: True,
                onebot_restarter=recover,
            ).check_once()

        self.assertEqual(state["status"], "recovering")
        self.assertEqual(reserved_counts, [1])

    def test_watchdog_settings_read_onebot_recovery_environment(self):
        from qq_openclaw_watchdog.watchdog import WatchdogSettings

        settings = WatchdogSettings.from_env(
            {
                "WATCHDOG_CHECK_INTERVAL_SECONDS": "10",
                "WATCHDOG_ONEBOT_FAILURE_THRESHOLD": "2",
                "WATCHDOG_ONEBOT_AUTO_RECOVERY": "true",
            }
        )

        self.assertEqual(settings.check_interval_seconds, 10)
        self.assertEqual(settings.onebot_failure_threshold, 2)
        self.assertTrue(settings.onebot_auto_recovery)

    def test_notification_failure_does_not_erase_health_result(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        def failing_notifier(_title, _message):
            raise RuntimeError("notification unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            state = Watchdog(
                project,
                WatchdogSettings(
                    notifications=True,
                    onebot_failure_threshold=2,
                ),
                bridge_probe=lambda: True,
                onebot_probe=lambda: False,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
                routing_probe=lambda: True,
                notifier=failing_notifier,
            ).check_once()
            persisted = json.loads(
                (project / "logs" / "health.json").read_text(encoding="utf-8")
            )

        self.assertEqual(state["status"], "degraded")
        self.assertFalse(state["checks"]["onebot"])
        self.assertEqual(state["notification_error"], "RuntimeError")
        self.assertEqual(persisted, state)

    def test_watchdog_defaults_to_launch_agent_bridge_probe(self):
        from qq_openclaw_watchdog.probes import LaunchAgentProbe
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            watchdog = Watchdog(
                project,
                WatchdogSettings(notifications=False),
            )

            self.assertIsInstance(
                watchdog._bridge_probe_object, LaunchAgentProbe
            )

    def test_disabled_queue_probe_is_healthy_without_database(self):
        from qq_openclaw_watchdog.probes import QueueProbe

        self.assertTrue(QueueProbe({"ENABLE_SQLITE_STORE": "false"}).run())

    def test_openclaw_non_gateway_probe_checks_command_file(self):
        from qq_openclaw_watchdog.probes import OpenClawProbe

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            command = root / "openclaw.sh"
            command.write_text("#!/bin/sh\n", encoding="utf-8")
            probe = OpenClawProbe(
                {"OPENCLAW_USE_GATEWAY": "false", "OPENCLAW_COMMAND": "openclaw.sh"},
                root,
            )
            self.assertTrue(probe.run())

    def test_watchdog_records_only_changed_semantic_health_across_restart(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            store = WorkspaceStateStore(project / "state.sqlite3")
            now = [datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)]
            checks = {
                "bridge": True,
                "onebot": True,
                "proxy": True,
                "openclaw": True,
                "queue": True,
            }

            def build_watchdog():
                return Watchdog(
                    project,
                    WatchdogSettings(notifications=False),
                    bridge_probe=lambda: checks["bridge"],
                    onebot_probe=lambda: checks["onebot"],
                    proxy_probe=lambda: checks["proxy"],
                    openclaw_probe=lambda: checks["openclaw"],
                    queue_probe=lambda: checks["queue"],
                    now_provider=lambda: now[0],
                    state_store=store,
                )

            build_watchdog().check_once()
            now[0] += timedelta(seconds=30)
            build_watchdog().check_once()
            checks["onebot"] = False
            now[0] += timedelta(seconds=30)
            first_failure = build_watchdog().check_once()
            now[0] += timedelta(seconds=30)
            repeated_failure = build_watchdog().check_once()

            for component in ("bridge", "proxy", "openclaw", "queue"):
                timeline = store.e4_repository.health_timeline(component=component)
                self.assertEqual(
                    [event["status"] for event in timeline],
                    ["healthy"],
                )
            onebot = store.e4_repository.health_timeline(component="onebot")
            self.assertEqual(
                [event["status"] for event in onebot],
                ["unhealthy", "healthy"],
            )
            self.assertEqual(first_failure["consecutive_failures"]["onebot"], 1)
            self.assertEqual(repeated_failure["consecutive_failures"]["onebot"], 2)
            health_json = (project / "logs" / "health.json").read_text(
                encoding="utf-8"
            )
            self.assertIn("2026-07-30T12:01:30+00:00", health_json)

    def test_watchdog_uses_configured_existing_sqlite_store(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            database = project / "state.sqlite3"
            WorkspaceStateStore(database)
            (project / ".env").write_text(
                "ENABLE_SQLITE_STORE=true\nSTATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )

            watchdog = Watchdog(
                project,
                WatchdogSettings(notifications=False),
                bridge_probe=lambda: True,
                onebot_probe=lambda: True,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
                now_provider=lambda: datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            )

            watchdog.check_once()

            events = WorkspaceStateStore(database).e4_repository.health_timeline()
            self.assertEqual(len(events), 6)
            self.assertEqual(
                {event["component"] for event in events},
                {"bridge", "onebot", "proxy", "openclaw", "queue", "routing"},
            )

    def test_watchdog_skips_health_events_when_sqlite_is_disabled(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            database = project / "state.sqlite3"
            store = WorkspaceStateStore(database)
            (project / ".env").write_text(
                "ENABLE_SQLITE_STORE=false\nSTATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )

            state = Watchdog(
                project,
                WatchdogSettings(notifications=False),
                bridge_probe=lambda: True,
                onebot_probe=lambda: True,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
            ).check_once()

            self.assertEqual(state["status"], "healthy")
            self.assertEqual(store.e4_repository.health_timeline(), [])
            self.assertTrue((project / "logs" / "health.json").is_file())

    def test_watchdog_tolerates_missing_sqlite_path(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            (project / ".env").write_text(
                "ENABLE_SQLITE_STORE=true\n",
                encoding="utf-8",
            )

            state = Watchdog(
                project,
                WatchdogSettings(notifications=False),
                bridge_probe=lambda: True,
                onebot_probe=lambda: True,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
            ).check_once()

            self.assertEqual(state["status"], "healthy")
            self.assertTrue((project / "logs" / "health.json").is_file())

    def test_health_snapshot_still_updates_when_event_persistence_fails(self):
        import sqlite3

        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        class FailingRepository:
            def record_health_change(self, *args, **kwargs):
                raise sqlite3.OperationalError("database unavailable")

        class FailingStore:
            e4_repository = FailingRepository()

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            state = Watchdog(
                project,
                WatchdogSettings(notifications=False),
                bridge_probe=lambda: True,
                onebot_probe=lambda: True,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
                state_store=FailingStore(),
            ).check_once()

            self.assertEqual(state["status"], "healthy")
            self.assertTrue((project / "logs" / "health.json").is_file())


if __name__ == "__main__":
    unittest.main()
