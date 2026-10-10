import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class F1OperationalAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _routing_config(classifier_enabled=False):
        return {
            "enabled": True,
            "classifier_enabled": classifier_enabled,
            "classifier_model": "openai/gpt-5.4-nano",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 15,
            "max_daily_calls": 100,
            "failure_threshold": 3,
            "cooldown_seconds": 300,
            "rule_version": "f1-v1",
        }

    @staticmethod
    def _routing_config_digest(config):
        canonical = json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def test_disabled_by_default_starts_without_a_shadow_runtime(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        config = Config.from_env(
            {"ONEBOT_WS_URL": "ws://127.0.0.1:3001", "ALLOWED_QQ_USERS": "10001"}
        )

        self.assertFalse(config.enable_shadow_routing)
        self.assertIsNone(create_shadow_routing_runtime(config, state_store=None))

    def test_pure_rule_runtime_starts_without_constructing_a_routing_agent(self):
        from types import SimpleNamespace

        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.routing_repository.create_config(
                self._routing_config(classifier_enabled=False), activate=True
            )
            config = SimpleNamespace(
                enable_shadow_routing=True,
                openclaw_command="not-called",
                openclaw_use_gateway=True,
                routing_agent_id="routing",
            )

            runtime = create_shadow_routing_runtime(
                config,
                store,
                adapter_factory=lambda **kwargs: self.fail("routing Agent was constructed"),
            )

        self.assertIsNotNone(runtime)

    def test_pure_rule_check_ready_succeeds_without_a_routing_agent(self):
        source_script = ROOT / "scripts" / "check_ready.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            scripts = project / "scripts"
            scripts.mkdir()
            script = scripts / "check_ready.sh"
            script.write_bytes(source_script.read_bytes())
            script.chmod(0o755)
            (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
            database = project / "state.sqlite3"
            with __import__("sqlite3").connect(database) as connection:
                connection.execute(
                    "CREATE TABLE routing_config_versions "
                    "(version INTEGER, config_json TEXT, config_sha256 TEXT, status TEXT)"
                )
                routing_config = self._routing_config(classifier_enabled=False)
                connection.execute(
                    "INSERT INTO routing_config_versions VALUES (1, ?, ?, 'active')",
                    (
                        json.dumps(routing_config),
                        self._routing_config_digest(routing_config),
                    ),
                )
            fake_openclaw = scripts / "fake-openclaw"
            fake_openclaw.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "[[ \"${1:-}\" == '--version' ]]\n"
                "printf 'OpenClaw test\\n'\n",
                encoding="utf-8",
            )
            fake_openclaw.chmod(0o755)
            (project / ".env").write_text(
                "\n".join(
                    (
                        "ONEBOT_WS_URL=ws://127.0.0.1:1",
                        "ALLOWED_QQ_USERS=10001",
                        "AGENT_ADAPTER=openclaw-cli",
                        "OPENCLAW_COMMAND=%s" % fake_openclaw,
                        "OPENCLAW_USE_GATEWAY=false",
                        "ENABLE_SHADOW_ROUTING=true",
                        "ENABLE_SQLITE_STORE=true",
                        "STATE_DATABASE_PATH=%s" % database,
                        "ENABLE_STYLE_MEMORY=false",
                        "ENABLE_HOUSEKEEPER=false",
                        "ENABLE_REMINDER_PUSH=false",
                        "ENABLE_AUTO_BACKUP=false",
                        "ENABLE_MEMORY_2=false",
                        "ENABLE_MEDIA_PIPELINE=false",
                        "ENABLE_LONG_TASKS=false",
                        "ORCHESTRATION_MODE=legacy_direct",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(script)], cwd=project, capture_output=True, text=True
            )
            invalid_config = self._routing_config(classifier_enabled="false")
            with __import__("sqlite3").connect(database) as connection:
                connection.execute(
                    "UPDATE routing_config_versions SET config_json=?, config_sha256=?",
                    (
                        json.dumps(invalid_config),
                        self._routing_config_digest(invalid_config),
                    ),
                )
            invalid = subprocess.run(
                [str(script)], cwd=project, capture_output=True, text=True
            )
            valid_config = self._routing_config(classifier_enabled=False)
            with __import__("sqlite3").connect(database) as connection:
                connection.execute(
                    "UPDATE routing_config_versions SET config_json=?, config_sha256=?",
                    (json.dumps(valid_config), "0" * 64),
                )
            mismatched = subprocess.run(
                [str(script)], cwd=project, capture_output=True, text=True
            )
            with __import__("sqlite3").connect(database) as connection:
                connection.execute(
                    "UPDATE routing_config_versions SET config_json='{', config_sha256=?",
                    ("0" * 64,),
                )
            malformed = subprocess.run(
                [str(script)], cwd=project, capture_output=True, text=True
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[SKIP] routing Agent: pure-rule mode", result.stdout)
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("routing active config is invalid", invalid.stderr)
        self.assertEqual(mismatched.returncode, 1)
        self.assertIn("routing active config is invalid", mismatched.stderr)
        self.assertEqual(malformed.returncode, 1)
        self.assertIn("routing active config is invalid", malformed.stderr)

    def test_fresh_pure_rule_enable_bootstraps_config_before_strict_readiness(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            scripts = project / "scripts"
            scripts.mkdir()
            for name in ("bridge_service.sh", "check_ready.sh"):
                script = scripts / name
                script.write_bytes((ROOT / "scripts" / name).read_bytes())
                script.chmod(0o755)
            bootstrap_source = ROOT / "scripts" / "bootstrap_routing_config.py"
            if bootstrap_source.exists():
                (scripts / bootstrap_source.name).write_bytes(
                    bootstrap_source.read_bytes()
                )
            (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
            database = project / "state.sqlite3"
            fake_openclaw = scripts / "fake-openclaw"
            fake_openclaw.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$*\" >>\"$OPENCLAW_CALLS\"\n"
                "[[ \"${1:-}\" == '--version' ]]\n"
                "printf 'OpenClaw test\\n'\n",
                encoding="utf-8",
            )
            fake_openclaw.chmod(0o755)
            home = project / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            (agents / "com.chichu.qq-bridge.plist").write_text(
                "test plist", encoding="utf-8"
            )
            fake_launchctl = project / "launchctl"
            fake_launchctl.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$*\" >>\"$CHICHU_LAUNCHCTL_LOG\"\n"
                "case \"$1\" in\n"
                "  print)\n"
                "    [[ -f \"$CHICHU_LAUNCHCTL_STATE\" ]] || exit 1\n"
                "    printf '    pid = 111\\n'\n"
                "    ;;\n"
                "  bootstrap) : >\"$CHICHU_LAUNCHCTL_STATE\" ;;\n"
                "  kickstart) [[ -f \"$CHICHU_LAUNCHCTL_STATE\" ]] ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_launchctl.chmod(0o755)
            fake_pgrep = project / "pgrep"
            fake_pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            fake_pgrep.chmod(0o755)
            openclaw_calls = project / "openclaw-calls.log"
            launchctl_calls = project / "launchctl-calls.log"
            launchctl_state = project / "launchctl-loaded"
            before_bootstrap = project / "before-bootstrap"
            after_bootstrap = project / "after-bootstrap"
            (project / ".env").write_text(
                "\n".join(
                    (
                        "ONEBOT_WS_URL=ws://127.0.0.1:1",
                        "ALLOWED_QQ_USERS=10001",
                        "AGENT_ADAPTER=openclaw-cli",
                        "OPENCLAW_COMMAND=%s" % fake_openclaw,
                        "OPENCLAW_USE_GATEWAY=false",
                        "ENABLE_SHADOW_ROUTING=true",
                        "ENABLE_SQLITE_STORE=true",
                        "STATE_DATABASE_PATH=%s" % database,
                        "BOOTSTRAP_ORDER=$(if [[ -f \"$STATE_DATABASE_PATH\" ]]; then "
                        "/usr/bin/touch \"$AFTER_BOOTSTRAP\"; else "
                        "/usr/bin/touch \"$BEFORE_BOOTSTRAP\"; fi)",
                        "ENABLE_STYLE_MEMORY=false",
                        "ENABLE_HOUSEKEEPER=false",
                        "ENABLE_REMINDER_PUSH=false",
                        "ENABLE_AUTO_BACKUP=false",
                        "ENABLE_MEMORY_2=false",
                        "ENABLE_MEDIA_PIPELINE=false",
                        "ENABLE_LONG_TASKS=false",
                        "ORCHESTRATION_MODE=legacy_direct",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            (project / ".env").chmod(0o600)
            environment = dict(
                __import__("os").environ,
                HOME=str(home),
                CHICHU_LAUNCHCTL_BIN=str(fake_launchctl),
                CHICHU_PGREP_BIN=str(fake_pgrep),
                CHICHU_LAUNCHCTL_LOG=str(launchctl_calls),
                CHICHU_LAUNCHCTL_STATE=str(launchctl_state),
                OPENCLAW_CALLS=str(openclaw_calls),
                BEFORE_BOOTSTRAP=str(before_bootstrap),
                AFTER_BOOTSTRAP=str(after_bootstrap),
            )

            strict_readiness = subprocess.run(
                [str(scripts / "check_ready.sh")],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            before_bootstrap.unlink(missing_ok=True)
            after_bootstrap.unlink(missing_ok=True)
            starts = [
                subprocess.run(
                    [str(scripts / "bridge_service.sh"), "start"],
                    cwd=project,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                for _ in range(2)
            ]
            for result in starts:
                self.assertEqual(result.returncode, 0, result.stderr)
            with __import__("sqlite3").connect(database) as connection:
                rows = connection.execute(
                    "SELECT version, config_json, status "
                    "FROM routing_config_versions ORDER BY version"
                ).fetchall()
            openclaw_call_lines = openclaw_calls.read_text(
                encoding="utf-8"
            ).splitlines()
            sourced_before_bootstrap = before_bootstrap.exists()
            sourced_after_bootstrap = after_bootstrap.exists()

        self.assertEqual(strict_readiness.returncode, 1)
        self.assertIn(
            "routing active config is unavailable", strict_readiness.stderr
        )
        for result in starts:
            self.assertIn("[OK] routing active config version: 1", result.stdout)
            self.assertIn("[SKIP] routing Agent: pure-rule mode", result.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][2], "active")
        self.assertEqual(json.loads(rows[0][1]), self._routing_config())
        self.assertEqual(openclaw_call_lines, ["--version", "--version"])
        self.assertFalse(sourced_before_bootstrap)
        self.assertTrue(sourced_after_bootstrap)

    def test_classifier_enabled_readiness_fails_when_routing_agent_is_missing(self):
        source_script = ROOT / "scripts" / "check_ready.sh"
        installer = ROOT / "scripts" / "install_routing_agent.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            scripts = project / "scripts"
            scripts.mkdir()
            script = scripts / "check_ready.sh"
            script.write_bytes(source_script.read_bytes())
            script.chmod(0o755)
            (scripts / "install_routing_agent.py").write_bytes(installer.read_bytes())
            (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
            database = project / "state.sqlite3"
            with __import__("sqlite3").connect(database) as connection:
                connection.execute(
                    "CREATE TABLE routing_config_versions "
                    "(version INTEGER, config_json TEXT, config_sha256 TEXT, status TEXT)"
                )
                routing_config = self._routing_config(classifier_enabled=True)
                connection.execute(
                    "INSERT INTO routing_config_versions VALUES (1, ?, ?, 'active')",
                    (
                        json.dumps(routing_config),
                        self._routing_config_digest(routing_config),
                    ),
                )
            openclaw_config = project / "openclaw.json"
            openclaw_config.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {"models": {"openai/gpt-5.4-nano": {}}},
                            "list": [{"id": "qq"}],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (project / ".env").write_text(
                "\n".join(
                    (
                        "ONEBOT_WS_URL=ws://127.0.0.1:1",
                        "ALLOWED_QQ_USERS=10001",
                        "AGENT_ADAPTER=openclaw-cli",
                        "OPENCLAW_COMMAND=/usr/bin/true",
                        "OPENCLAW_CONFIG_PATH=%s" % openclaw_config,
                        "ENABLE_SHADOW_ROUTING=true",
                        "ENABLE_SQLITE_STORE=true",
                        "STATE_DATABASE_PATH=%s" % database,
                        "ENABLE_STYLE_MEMORY=false",
                        "ENABLE_HOUSEKEEPER=false",
                        "ENABLE_REMINDER_PUSH=false",
                        "ENABLE_AUTO_BACKUP=false",
                        "ENABLE_MEMORY_2=false",
                        "ENABLE_MEDIA_PIPELINE=false",
                        "ENABLE_LONG_TASKS=false",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(script)], cwd=project, capture_output=True, text=True
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("routing Agent must exist exactly once", result.stderr)

    def test_stale_pending_recovers_to_a_healthy_routing_probe(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_watchdog.probes import RoutingProbe

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            database = project / "state.sqlite3"
            store = WorkspaceStateStore(database)
            active = store.routing_repository.create_config(
                self._routing_config(), activate=True
            )
            now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
            store.routing_repository.start_decision(
                user_id=10001,
                source_message_sha256="a" * 64,
                message_sha256="b" * 64,
                message_preview="bounded",
                routing_config_version=active["version"],
                rule_version="f1-v1",
                dispatch_overhead_ms=0,
                now=now - timedelta(seconds=76),
            )
            probe = RoutingProbe(
                {
                    "ENABLE_SHADOW_ROUTING": "true",
                    "ENABLE_SQLITE_STORE": "true",
                    "STATE_DATABASE_PATH": str(database),
                },
                project,
                now_provider=lambda: now,
            )

            self.assertFalse(probe.run())
            self.assertEqual(store.routing_repository.recover_pending_as_interrupted(), 1)
            self.assertTrue(probe.run())

    def test_routing_degradation_isolated_from_bridge_restart_ownership(self):
        from qq_openclaw_watchdog.watchdog import Watchdog, WatchdogSettings

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            (project / "logs").mkdir()
            (project / "scripts").mkdir()
            restarts = []
            state = Watchdog(
                project,
                WatchdogSettings(notifications=False, failure_threshold=1),
                bridge_probe=lambda: True,
                onebot_probe=lambda: True,
                proxy_probe=lambda: True,
                openclaw_probe=lambda: True,
                queue_probe=lambda: True,
                routing_probe=lambda: False,
                runner=lambda *args, **kwargs: restarts.append(args),
                now_provider=lambda: datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
            ).check_once()

        self.assertEqual(state["status"], "degraded")
        self.assertEqual(state["restart_count"], 0)
        self.assertEqual(restarts, [])

    def test_f0_legacy_direct_rollback_replies_once_when_f1_is_disabled(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.models import AgentRequest, AgentResponse
        from qq_openclaw_bridge.runtime.composition import build_application
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        calls = []

        class Adapter:
            def reply(self, request):
                calls.append(request)
                return AgentResponse("你好")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            async def run_legacy_reply():
                bridge = build_application(
                    Config(
                        onebot_ws_url="ws://127.0.0.1:3001",
                        allowed_qq_users={10001},
                        orchestration_mode="legacy_direct",
                        enable_shadow_routing=False,
                        enable_security_confirmation=False,
                        enable_auto_backup=False,
                        enable_style_memory=False,
                        enable_housekeeper=False,
                        enable_reminder_push=False,
                        enable_memory2=False,
                    ),
                    state_store_factory=lambda _config: store,
                    agent_factory=lambda _config: Adapter(),
                    style_factory=lambda _config, state_store=None: None,
                    housekeeper_factory=lambda _config, state_store=None: None,
                    reminder_factory=lambda _config, state_store=None: None,
                    memory_factory=lambda _config, state_store, housekeeper: None,
                    backup_factory=lambda _config: None,
                )
                request = AgentRequest(10001, "f0-rollback", "你好", 10.0)
                resolution = await bridge.message_coordinator.resolve_prepared(
                    bridge.message_coordinator.prepare(request)
                )
                return bridge.message_coordinator, resolution

            coordinator, result = asyncio.run(run_legacy_reply())

        self.assertEqual(result.reply, "你好")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(coordinator.orchestration_runtime)
        self.assertFalse(coordinator.has_shadow_routing_observer)


if __name__ == "__main__":
    unittest.main()
