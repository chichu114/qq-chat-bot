import contextlib
import hashlib
import io
import json
import os
import plistlib
import sqlite3
import subprocess
import shutil
import tempfile
from pathlib import Path
import sys
from unittest import mock
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from install_routing_agent import ROUTING_AGENT_DENY


def _routing_config_digest(config):
    canonical = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ScriptTests(unittest.TestCase):
    def _style_learner_service_harness(
        self, root, *, loaded=False, fail_command=""
    ):
        project = root / "project"
        scripts = project / "scripts"
        launchd = project / "launchd"
        scripts.mkdir(parents=True)
        launchd.mkdir()
        for name in ("style_learner_service.sh", "install_launch_agents.py"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        for template in (ROOT / "launchd").glob("*.plist.template"):
            shutil.copy2(template, launchd / template.name)
        (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)

        home = root / "home"
        home.mkdir()
        call_log = root / "launchctl.log"
        fake_launchctl = root / "launchctl"
        fake_launchctl.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$CALL_LOG\"\n"
            "if [[ \"${1:-}\" == print ]]; then\n"
            "  if [[ \"$FAKE_LOADED\" == 1 ]]; then printf 'pid = 2468\\n'; exit 0; fi\n"
            "  exit 1\n"
            "fi\n"
            "if [[ \"${1:-}\" == \"$FAIL_COMMAND\" ]]; then exit 7; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_launchctl.chmod(0o755)
        fake_pgrep = root / "pgrep"
        fake_pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        fake_pgrep.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home),
            "CALL_LOG": str(call_log),
            "FAKE_LOADED": "1" if loaded else "0",
            "FAIL_COMMAND": fail_command,
            "CHICHU_LAUNCHCTL_BIN": str(fake_launchctl),
            "CHICHU_PGREP_BIN": str(fake_pgrep),
        })
        return project, home, call_log, environment

    @staticmethod
    def _launchctl_mutations(call_log):
        if not call_log.exists():
            return []
        return [
            line
            for line in call_log.read_text(encoding="utf-8").splitlines()
            if line.split(maxsplit=1)[0] in {"bootout", "bootstrap", "kickstart"}
        ]

    def test_python_entrypoints_configure_logging_before_application_state(self):
        entrypoints = (
            ("src/qq_openclaw_bridge/__main__.py", "build_application("),
            ("src/qq_openclaw_dashboard/__main__.py", "DashboardController.from_project("),
            ("src/qq_openclaw_style_learner/__main__.py", "learner = build_style_learner("),
            ("src/qq_openclaw_watchdog/__main__.py", "Watchdog("),
        )

        for relative_path, application_marker in entrypoints:
            with self.subTest(entrypoint=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("configure_process_logging(", source)
                self.assertLess(
                    source.index("configure_process_logging("),
                    source.index(application_marker),
                )

    def test_python_entrypoints_log_only_startup_error_class(self):
        entrypoints = (
            "src/qq_openclaw_bridge/__main__.py",
            "src/qq_openclaw_dashboard/__main__.py",
            "src/qq_openclaw_style_learner/__main__.py",
            "src/qq_openclaw_watchdog/__main__.py",
        )

        for relative_path in entrypoints:
            with self.subTest(entrypoint=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn('extra={"error_class": error.__class__.__name__}', source)
                self.assertNotIn("str(error)", source)

    def test_python_entrypoints_suppress_raw_bootstrap_logging_errors(self):
        from qq_openclaw_bridge import __main__ as bridge_main
        from qq_openclaw_dashboard import __main__ as dashboard_main
        from qq_openclaw_watchdog import __main__ as watchdog_main

        cases = (
            (bridge_main, ["bridge"]),
            (dashboard_main, ["dashboard", "--project-dir", str(ROOT)]),
            (watchdog_main, ["watchdog", "--project-dir", str(ROOT)]),
        )
        for module, argv in cases:
            with self.subTest(module=module.__name__):
                stderr = io.StringIO()
                with mock.patch.object(sys, "argv", argv), mock.patch.object(
                    module,
                    "configure_process_logging",
                    side_effect=OSError("raw-disk-secret"),
                ), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    module.main()
                self.assertEqual(caught.exception.code, 1)
                self.assertIn("OSError", stderr.getvalue())
                self.assertNotIn("raw-disk-secret", stderr.getvalue())

    def test_start_bridge_script_has_valid_bash_syntax_and_help(self):
        script = ROOT / "scripts" / "start_bridge.sh"

        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Usage: scripts/start_bridge.sh", help_result.stdout)

    def test_check_ready_script_has_valid_bash_syntax_and_help(self):
        script = ROOT / "scripts" / "check_ready.sh"

        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Usage: scripts/check_ready.sh", help_result.stdout)

    def test_openclaw_wrapper_uses_project_entrypoint_not_pnpm_bin_shim(self):
        content = (ROOT / "scripts" / "openclaw.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('node_modules/openclaw/openclaw.mjs', content)
        self.assertIn('exec "$NODE_BIN" "$OPENCLAW_ENTRY" "$@"', content)
        self.assertNotIn('node_modules/.bin/openclaw', content)

    def test_bridge_service_script_has_valid_bash_syntax_and_help(self):
        script = ROOT / "scripts" / "bridge_service.sh"

        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Usage: scripts/bridge_service.sh", help_result.stdout)

    def test_routing_bootstrap_parses_secured_env_without_opening_disabled_db(self):
        script = ROOT / "scripts" / "bootstrap_routing_config.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            database = project / "disabled.sqlite3"
            sentinel = project / "shell-executed"
            env_file = project / ".env"
            env_file.write_text(
                "ONEBOT_WS_URL=ws://127.0.0.1:1\n"
                "ALLOWED_QQ_USERS=10001\n"
                "ENABLE_SHADOW_ROUTING=false\n"
                "ENABLE_SQLITE_STORE=true\n"
                "STATE_DATABASE_PATH=%s\n" % database
                + "UNUSED=$(touch %s)\n" % sentinel,
                encoding="utf-8",
            )
            env_file.chmod(0o600)

            result = subprocess.run(
                [sys.executable, str(script), "--project-dir", str(project)],
                env={},
                capture_output=True,
                text=True,
            )
            database_opened = database.exists()
            shell_executed = sentinel.exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(database_opened)
        self.assertFalse(shell_executed)

    def test_routing_bootstrap_rejects_insecure_env_permissions(self):
        script = ROOT / "scripts" / "bootstrap_routing_config.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            env_file = project / ".env"
            env_file.write_text(
                "ONEBOT_WS_URL=ws://127.0.0.1:1\nENABLE_SHADOW_ROUTING=false\n",
                encoding="utf-8",
            )
            env_file.chmod(0o644)

            result = subprocess.run(
                [sys.executable, str(script), "--project-dir", str(project)],
                env={},
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600", result.stderr)

    def test_routing_bootstrap_fails_on_corrupt_enabled_database(self):
        script = ROOT / "scripts" / "bootstrap_routing_config.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            database = project / "corrupt.sqlite3"
            database.write_bytes(b"not a sqlite database")
            env_file = project / ".env"
            env_file.write_text(
                "ONEBOT_WS_URL=ws://127.0.0.1:1\n"
                "ALLOWED_QQ_USERS=10001\n"
                "ENABLE_SHADOW_ROUTING=true\n"
                "ENABLE_SQLITE_STORE=true\n"
                "STATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )
            env_file.chmod(0o600)

            result = subprocess.run(
                [sys.executable, str(script), "--project-dir", str(project)],
                env={},
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DatabaseError", result.stderr)

    def test_routing_bootstrap_does_not_rehash_tampered_marked_store(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        script = ROOT / "scripts" / "bootstrap_routing_config.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            database = project / "state.sqlite3"
            original = {
                "enabled": True,
                "classifier_enabled": False,
                "classifier_model": "openai/gpt-5.4-nano",
                "confidence_threshold_percent": 75,
                "classifier_timeout_seconds": 15,
                "max_daily_calls": 100,
                "failure_threshold": 3,
                "cooldown_seconds": 300,
                "rule_version": "f1-v1",
            }
            store = WorkspaceStateStore(database)
            active = store.routing_repository.create_config(
                original, activate=True
            )
            tampered = dict(original, rule_version="f1-v2")
            tampered_json = json.dumps(
                tampered,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            with store._connect() as connection:
                marker = connection.execute(
                    "SELECT value FROM metadata "
                    "WHERE key='routing_config_hash_compat_v1'"
                ).fetchone()
                connection.execute(
                    "UPDATE routing_config_versions SET config_json=? "
                    "WHERE version=?",
                    (tampered_json, active["version"]),
                )
            env_file = project / ".env"
            env_file.write_text(
                "ONEBOT_WS_URL=ws://127.0.0.1:1\n"
                "ALLOWED_QQ_USERS=10001\n"
                "ENABLE_SHADOW_ROUTING=true\n"
                "ENABLE_SQLITE_STORE=true\n"
                "STATE_DATABASE_PATH=%s\n" % database,
                encoding="utf-8",
            )
            env_file.chmod(0o600)

            result = subprocess.run(
                [sys.executable, str(script), "--project-dir", str(project)],
                env={},
                capture_output=True,
                text=True,
            )
            with sqlite3.connect(database) as connection:
                persisted = connection.execute(
                    "SELECT config_json, config_sha256 "
                    "FROM routing_config_versions WHERE version=?",
                    (active["version"],),
                ).fetchone()

        self.assertEqual(marker, ("1",))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(persisted, (tampered_json, active["config_sha256"]))

    def test_routing_bootstrap_rejects_fifo_without_waiting_for_writer(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo is unavailable")
        script = ROOT / "scripts" / "bootstrap_routing_config.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            env_file = project / ".env"
            os.mkfifo(env_file, 0o600)
            env_file.chmod(0o600)

            try:
                result = subprocess.run(
                    [sys.executable, str(script), "--project-dir", str(project)],
                    env={},
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except subprocess.TimeoutExpired:
                self.fail("routing bootstrap blocked while opening a FIFO")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular file", result.stderr)

    def test_service_scripts_delegate_process_ownership_to_launchctl(self):
        for name in (
            "bridge_service.sh",
            "dashboard_service.sh",
            "watchdog_service.sh",
        ):
            with self.subTest(script=name):
                content = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("launchctl", content)
                self.assertIn("bootstrap", content)
                self.assertIn("bootout", content)
                self.assertIn("kickstart", content)
                self.assertIn("print", content)
                self.assertNotIn("nohup", content)
                self.assertNotIn("kill -9", content)
                self.assertNotRegex(content, r"(?m)(?<!&)&\s*$")

    def test_service_scripts_use_exact_managed_labels(self):
        labels = {
            "bridge_service.sh": "com.chichu.qq-bridge",
            "dashboard_service.sh": "com.chichu.qq-dashboard",
            "watchdog_service.sh": "com.chichu.qq-watchdog",
        }
        for name, label in labels.items():
            with self.subTest(script=name):
                content = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn(label, content)
                self.assertIn('gui/$(id -u)', content)

    def test_service_scripts_fail_closed_when_a_legacy_process_is_running(self):
        modules = {
            "bridge_service.sh": "qq_openclaw_bridge",
            "dashboard_service.sh": "qq_openclaw_dashboard",
            "watchdog_service.sh": "qq_openclaw_watchdog",
        }
        for name, module in modules.items():
            with self.subTest(script=name):
                content = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("/usr/bin/pgrep", content)
                self.assertNotIn("pgrep -f --", content)
                self.assertIn("legacy", content.lower())
                self.assertIn(module, content)
                self.assertIn("managed_pid", content)
                self.assertNotIn("PID_FILE", content)

    def test_dashboard_wrapper_preserves_cli_with_fake_launchctl(self):
        script = ROOT / "scripts" / "dashboard_service.sh"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            (agents / "com.chichu.qq-dashboard.plist").write_text(
                "test plist", encoding="utf-8"
            )
            fake_launchctl = root / "launchctl"
            state = root / "loaded"
            log = root / "calls.log"
            fake_launchctl.write_text(
                """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >>"$CHICHU_LAUNCHCTL_LOG"
case "$1" in
  print)
    [[ -f "$CHICHU_LAUNCHCTL_STATE" ]] || exit 1
    printf '    pid = 111\\n'
    ;;
  bootstrap) : >"$CHICHU_LAUNCHCTL_STATE" ;;
  kickstart) [[ -f "$CHICHU_LAUNCHCTL_STATE" ]] ;;
  bootout) rm -f "$CHICHU_LAUNCHCTL_STATE" ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_launchctl.chmod(0o755)
            fake_pgrep = root / "pgrep"
            fake_pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            fake_pgrep.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "CHICHU_LAUNCHCTL_BIN": str(fake_launchctl),
                    "CHICHU_PGREP_BIN": str(fake_pgrep),
                    "CHICHU_LAUNCHCTL_STATE": str(state),
                    "CHICHU_LAUNCHCTL_LOG": str(log),
                }
            )

            for command in ("start", "status", "restart", "stop"):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [str(script), command],
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

            calls = log.read_text(encoding="utf-8")
            domain = "gui/%d" % os.getuid()
            target = domain + "/com.chichu.qq-dashboard"
            self.assertIn("bootstrap %s %s" % (domain, agents / "com.chichu.qq-dashboard.plist"), calls)
            self.assertIn("kickstart -k %s" % target, calls)
            self.assertIn("bootout %s" % target, calls)

            fake_pgrep.write_text(
                "#!/usr/bin/env bash\nprintf '111\\n'\n",
                encoding="utf-8",
            )
            state.write_text("loaded", encoding="utf-8")
            log.write_text("", encoding="utf-8")
            result = subprocess.run(
                [str(script), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            managed_calls = log.read_text(encoding="utf-8")
            self.assertIn("kickstart", managed_calls)
            self.assertNotIn("bootstrap", managed_calls)

            fake_pgrep.write_text(
                "#!/usr/bin/env bash\nprintf '111\\n4242\\n'\n",
                encoding="utf-8",
            )
            fake_pgrep.chmod(0o755)
            state.write_text("loaded", encoding="utf-8")
            log.write_text("", encoding="utf-8")
            result = subprocess.run(
                [str(script), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy unmanaged", result.stderr)
            legacy_calls = log.read_text(encoding="utf-8")
            self.assertNotIn("bootstrap", legacy_calls)
            self.assertNotIn("kickstart", legacy_calls)

    def test_start_bridge_runs_check_script_through_bash(self):
        script = ROOT / "scripts" / "start_bridge.sh"
        content = script.read_text(encoding="utf-8")

        self.assertIn('/bin/bash "$CHECK_SCRIPT"', content)

    def test_readiness_check_validates_configured_openclaw_agent(self):
        content = (ROOT / "scripts" / "check_ready.sh").read_text(encoding="utf-8")

        self.assertIn('agents list --json', content)
        self.assertIn('OpenClaw agent does not exist', content)
        self.assertIn('gateway health', content)
        self.assertIn('OPENCLAW_USE_GATEWAY', content)
        self.assertNotIn('OPENCLAW_GATEWAY_URL', content)
        self.assertIn('smoke_command+=("--local")', content)
        self.assertIn('EMBEDDED FALLBACK', content)
        self.assertIn('smoke test fell back to embedded mode', content)

    def test_readiness_treats_shadow_routing_as_optional_and_probes_only_routing(self):
        script = ROOT / "scripts" / "check_ready.sh"
        content = script.read_text(encoding="utf-8")
        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )

        self.assertIn("--probe-routing", help_result.stdout)
        self.assertIn("[SKIP] shadow routing: disabled", content)
        self.assertIn("ROUTING_AGENT_ID", content)
        self.assertIn("routing_config_versions", content)
        self.assertIn('openclaw_agents["defaults"]["models"]', content)
        self.assertIn('routing_tools.get("alsoAllow") != []', content)
        self.assertIn('--agent "${ROUTING_AGENT_ID:-routing}"', content)
        self.assertIn("--session-key", content)
        self.assertIn("routing-probe", content)
        self.assertIn("strict JSON", content)

    def test_runbook_routing_commands_load_controlled_env_without_leaking_values(self):
        runbook = (ROOT / "docs" / "f1-shadow-routing-runbook-zh.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("source ./.env", runbook)
        self.assertIn(
            ': "${OPENCLAW_CONFIG_PATH:?OPENCLAW_CONFIG_PATH must be set in .env}"',
            runbook,
        )
        self.assertIn(
            ': "${STATE_DATABASE_PATH:?STATE_DATABASE_PATH must be set in .env}"',
            runbook,
        )

        with tempfile.TemporaryDirectory(prefix="chichu-runbook-smoke-") as temp_dir:
            project = Path(temp_dir)
            secret = "runbook-smoke-secret-not-for-output"
            database = project / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE route_decisions (status TEXT)")
                connection.execute(
                    "CREATE TABLE route_labels (verdict TEXT, correct_level TEXT)"
                )
            config_path = project / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "token": secret,
                        "agents": {
                            "defaults": {"models": {"openai/gpt-5.4-nano": {}}},
                            "list": [{"id": "qq"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (project / ".env").write_text(
                "\n".join(
                    (
                        "OPENCLAW_CONFIG_PATH=%s" % config_path,
                        "STATE_DATABASE_PATH=%s" % database,
                        "ROUTING_MODEL=openai/gpt-5.4-nano",
                        "RUNBOOK_SMOKE_SECRET=%s" % secret,
                        "",
                    )
                ),
                encoding="utf-8",
            )
            command = """set -euo pipefail
set -a
source \"$1/.env\"
set +a
: \"${OPENCLAW_CONFIG_PATH:?OPENCLAW_CONFIG_PATH must be set in .env}\"
: \"${STATE_DATABASE_PATH:?STATE_DATABASE_PATH must be set in .env}\"
\"$2\" \"$3\" --config \"$OPENCLAW_CONFIG_PATH\" --model \"$ROUTING_MODEL\" --agent routing
/usr/bin/sqlite3 -readonly \"$STATE_DATABASE_PATH\" <<'SQL'
SELECT status, COUNT(*) AS decision_count
FROM route_decisions
GROUP BY status
ORDER BY status;

SELECT verdict, correct_level, COUNT(*) AS label_count
FROM route_labels
GROUP BY verdict, correct_level
ORDER BY verdict, correct_level;
SQL
"""
            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    command,
                    "runbook-smoke",
                    str(project),
                    str(ROOT / ".venv" / "bin" / "python"),
                    str(ROOT / "scripts" / "install_routing_agent.py"),
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)

    def test_routing_probe_uses_production_envelope_parser_once_and_never_delivers(self):
        source_script = ROOT / "scripts" / "check_ready.sh"
        installer = ROOT / "scripts" / "install_routing_agent.py"
        with tempfile.TemporaryDirectory(prefix="chichu-routing-ready-") as temp_dir:
            project = Path(temp_dir)
            scripts = project / "scripts"
            scripts.mkdir()
            script = scripts / "check_ready.sh"
            script.write_bytes(source_script.read_bytes())
            script.chmod(0o755)
            (scripts / "install_routing_agent.py").write_bytes(installer.read_bytes())
            (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
            state_database = project / "routing.sqlite3"
            routing_config = {
                "enabled": True,
                "classifier_enabled": True,
                "classifier_model": "openai/gpt-5.4-nano",
                "confidence_threshold_percent": 75,
                "classifier_timeout_seconds": 15,
                "max_daily_calls": 100,
                "failure_threshold": 3,
                "cooldown_seconds": 300,
                "rule_version": "f1-v1",
            }
            with sqlite3.connect(state_database) as connection:
                connection.execute(
                    "CREATE TABLE routing_config_versions "
                    "(version INTEGER, config_json TEXT, config_sha256 TEXT, status TEXT)"
                )
                connection.execute(
                    "INSERT INTO routing_config_versions VALUES (1, ?, ?, 'active')",
                    (json.dumps(routing_config), _routing_config_digest(routing_config)),
                )
            openclaw_config = project / "openclaw.json"
            openclaw_config.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {"models": {"openai/gpt-5.4-nano": {}}},
                            "list": [
                                {"id": "qq"},
                                {
                                    "id": "routing",
                                    "model": "openai/gpt-5.4-nano",
                                    "skills": [],
                                    "tools": {
                                        "profile": "minimal",
                                        "allow": [],
                                        "alsoAllow": [],
                                        "deny": ROUTING_AGENT_DENY,
                                        "elevated": {"enabled": False},
                                    },
                                },
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            call_log = project / "calls.log"
            fake_openclaw = scripts / "fake-openclaw"
            fake_openclaw.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"${1:-}\" == \"agent\" ]]; then\n"
                "  printf 'agent id=%s session=%s deliver=%s\\n' \"${4:-}\" \"${8:-}\" \"${10:-}\" >> \"$CHICHU_FAKE_OPENCLAW_LOG\"\n"
                "  case \"${6:-}\" in *task_kind*) printf 'prompt=task_kind\\n' >> \"$CHICHU_FAKE_OPENCLAW_LOG\" ;; esac\n"
                "else\n"
                "  printf '%s\\n' \"$*\" >> \"$CHICHU_FAKE_OPENCLAW_LOG\"\n"
                "fi\n"
                "case \"${1:-}\" in\n"
                "  --version) printf 'OpenClaw test\\n' ;;\n"
                "  agents) printf '[{\\\"id\\\":\\\"qq\\\"}]\\n' ;;\n"
                "  gateway) exit 0 ;;\n"
                "  agent)\n"
                "    if [[ \"${CHICHU_FAKE_ROUTING_MODE:-ok}\" == \"term\" ]]; then\n"
                "      kill -TERM \"$PPID\"\n"
                "      exit 143\n"
                "    fi\n"
                "    printf '%s\\n' \"$CHICHU_FAKE_ROUTING_RESPONSE\" ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_openclaw.chmod(0o755)
            env_file = project / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "ONEBOT_WS_URL=ws://127.0.0.1:1",
                        "ALLOWED_QQ_USERS=10001",
                        "AGENT_ADAPTER=openclaw-cli",
                        "OPENCLAW_COMMAND=%s" % fake_openclaw,
                        "OPENCLAW_AGENT_ID=qq",
                        "OPENCLAW_USE_GATEWAY=true",
                        "OPENCLAW_CONFIG_PATH=%s" % openclaw_config,
                        "ROUTING_AGENT_ID=routing",
                        "ENABLE_SHADOW_ROUTING=YES",
                        "ENABLE_SQLITE_STORE=true",
                        "STATE_DATABASE_PATH=%s" % state_database,
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
            environment = os.environ.copy()
            environment.update(
                {
                    "CHICHU_FAKE_OPENCLAW_LOG": str(call_log),
                    "TMPDIR": str(project),
                    "CHICHU_FAKE_ROUTING_MODE": "ok",
                    "CHICHU_FAKE_ROUTING_RESPONSE": json.dumps(
                        {
                            "payloads": [
                                {
                                    "text": json.dumps(
                                        {
                                            "level": "L1",
                                            "confidence": 0.9,
                                            "task_kind": "smalltalk",
                                            "reasons": ["harmless"],
                                            "brainstorm_required": False,
                                        }
                                    )
                                }
                            ],
                            "meta": {},
                        }
                    ),
                }
            )

            result = subprocess.run(
                [str(script), "--probe-routing"],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("routing probe passed", result.stdout)
            calls = call_log.read_text(encoding="utf-8").splitlines()
            routing_calls = [line for line in calls if line.startswith("agent ")]
            self.assertEqual(len(routing_calls), 1)
            self.assertIn("id=routing", routing_calls[0])
            self.assertIn("session=agent:routing:routing-probe-", routing_calls[0])
            self.assertNotIn("deliver=--deliver", routing_calls[0])
            self.assertNotIn("qq", routing_calls[0].lower())
            self.assertIn("task_kind", call_log.read_text(encoding="utf-8"))
            self.assertFalse(list(project.glob("chichu-routing-probe.*")))

            invalid_responses = (
                '{"level":"L1","level":"L2","confidence":0.9,'
                '"task_kind":"smalltalk","reasons":[],"brainstorm_required":false}',
                '{"level":"L1","confidence":0.9,"task_kind":"unknown",'
                '"reasons":[],"brainstorm_required":false}',
                '{"level":"L1","confidence":0.9,"task_kind":"smalltalk",'
                '"reasons":[],"brainstorm_required":false,"extra":true}',
                '{"level":"L1","confidence":NaN,"task_kind":"smalltalk",'
                '"reasons":[],"brainstorm_required":false}',
            )
            for response in invalid_responses:
                with self.subTest(response=response):
                    environment["CHICHU_FAKE_ROUTING_RESPONSE"] = json.dumps(
                        {"payloads": [{"text": response}]}
                    )
                    failed = subprocess.run(
                        [str(script), "--probe-routing"],
                        cwd=project,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("strict JSON", failed.stderr)
                    self.assertNotIn(response, failed.stdout + failed.stderr)
                    self.assertFalse(list(project.glob("chichu-routing-probe.*")))

            environment["CHICHU_FAKE_ROUTING_MODE"] = "term"
            environment["CHICHU_FAKE_ROUTING_RESPONSE"] = "provider-secret-response"
            interrupted = subprocess.run(
                [str(script), "--probe-routing"],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(interrupted.returncode, 0)
            self.assertNotIn("provider-secret-response", interrupted.stdout + interrupted.stderr)
            self.assertFalse(list(project.glob("chichu-routing-probe.*")))

            original_env = env_file.read_text(encoding="utf-8")
            environment["CHICHU_FAKE_ROUTING_MODE"] = "ok"
            environment["CHICHU_FAKE_ROUTING_RESPONSE"] = json.dumps(
                {
                    "payloads": [
                        {
                            "text": json.dumps(
                                {
                                    "level": "L1",
                                    "confidence": 0.9,
                                    "task_kind": "smalltalk",
                                    "reasons": [],
                                    "brainstorm_required": False,
                                }
                            )
                        }
                    ]
                }
            )
            for value in (
                "1", "true", "yes", "on", "TRUE",
                '"  1  "', '"  true  "', '"  yes  "', '"  on  "', '"  YES  "',
            ):
                with self.subTest(enabled=value):
                    env_file.write_text(
                        original_env.replace("ENABLE_SHADOW_ROUTING=YES", "ENABLE_SHADOW_ROUTING=" + value),
                        encoding="utf-8",
                    )
                    enabled = subprocess.run(
                        [str(script), "--probe-routing"],
                        cwd=project,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(enabled.returncode, 0, enabled.stderr)
            for value in (
                "0", "no", "off", "false", "FALSE",
                '"  0  "', '"  no  "', '"  off  "', '"  false  "', '"  FALSE  "', '"   "',
            ):
                with self.subTest(disabled=value):
                    env_file.write_text(
                        original_env.replace("ENABLE_SHADOW_ROUTING=YES", "ENABLE_SHADOW_ROUTING=" + value),
                        encoding="utf-8",
                    )
                    disabled = subprocess.run(
                        [str(script)],
                        cwd=project,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(disabled.returncode, 0, disabled.stderr)
                    self.assertIn("[SKIP] shadow routing: disabled", disabled.stdout)
            env_file.write_text(
                original_env.replace("ENABLE_SHADOW_ROUTING=YES", "ENABLE_SHADOW_ROUTING=perhaps"),
                encoding="utf-8",
            )
            invalid = subprocess.run(
                [str(script)],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("ENABLE_SHADOW_ROUTING must be true or false", invalid.stderr)

    def test_readiness_check_reports_e4_state_and_offers_explicit_media_probe(self):
        script = ROOT / "scripts" / "check_ready.sh"
        content = script.read_text(encoding="utf-8")
        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )

        self.assertIn("--smoke-media", help_result.stdout)
        self.assertIn("SMOKE_MEDIA", content)
        self.assertIn("media pipeline", content)
        self.assertIn("media cache", content)
        self.assertIn("task queue", content)
        self.assertIn("outbox", content)

    def test_readiness_reports_f0_operational_state_without_an_implicit_model_call(self):
        script = ROOT / "scripts" / "check_ready.sh"
        content = script.read_text(encoding="utf-8")
        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )

        self.assertIn("--smoke-orchestration", help_result.stdout)
        self.assertIn("[INFO] orchestration mode:", content)
        self.assertIn("[INFO] orchestration config version:", content)
        self.assertIn("[INFO] orchestration active mode:", content)
        self.assertIn("does not match ORCHESTRATION_MODE", content)
        self.assertIn("[INFO] orchestration active backlog:", content)
        self.assertIn(
            "WHERE status IN ('received','planned','executing','verifying','accepted')",
            content,
        )
        self.assertIn("[INFO] launchd bridge/dashboard/watchdog:", content)
        self.assertIn("state = running", content)
        self.assertIn("[INFO] backup encryption:", content)
        self.assertIn('if [[ "$SMOKE_ORCHESTRATION" == "1" ]]', content)
        self.assertIn("--smoke-orchestration requires ORCHESTRATION_MODE=direct", content)
        self.assertIn("EMBEDDED FALLBACK", content)
        self.assertIn("qq_openclaw_bridge.runtime.readiness", content)

    def test_readiness_default_is_free_and_direct_smoke_calls_agent_once(self):
        source_script = ROOT / "scripts" / "check_ready.sh"
        with tempfile.TemporaryDirectory(prefix="chichu-ready-") as temp_dir:
            project = Path(temp_dir)
            scripts = project / "scripts"
            scripts.mkdir()
            script = scripts / "check_ready.sh"
            script.write_bytes(source_script.read_bytes())
            script.chmod(0o755)
            (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)

            call_log = project / "openclaw-calls.log"
            fake_openclaw = scripts / "fake-openclaw"
            fake_openclaw.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$*\" >> \"$CHICHU_FAKE_OPENCLAW_LOG\"\n"
                "case \"${1:-}\" in\n"
                "  --version) printf 'OpenClaw test\\n' ;;\n"
                "  agents) printf '[{\"id\":\"qq\"}]\\n' ;;\n"
                "  gateway) exit 0 ;;\n"
                "  agent) printf '{\"reply\":\"你好\"}\\n' ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_openclaw.chmod(0o755)

            env_file = project / ".env"
            common = "\n".join(
                (
                    "ONEBOT_WS_URL=ws://127.0.0.1:1",
                    "ALLOWED_QQ_USERS=10001",
                    "AGENT_ADAPTER=openclaw-cli",
                    "OPENCLAW_COMMAND=%s" % fake_openclaw,
                    "OPENCLAW_AGENT_ID=qq",
                    "OPENCLAW_USE_GATEWAY=true",
                    "ENABLE_SQLITE_STORE=false",
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
            )
            env_file.write_text(common, encoding="utf-8")
            env_file.chmod(0o600)
            environment = os.environ.copy()
            environment["CHICHU_FAKE_OPENCLAW_LOG"] = str(call_log)

            free = subprocess.run(
                [str(script)],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(free.returncode, 0, free.stderr)
            free_calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any(line.startswith("agent ") for line in free_calls))

            env_file.write_text(
                common.replace(
                    "ORCHESTRATION_MODE=legacy_direct",
                    "ORCHESTRATION_MODE=direct",
                ),
                encoding="utf-8",
            )
            call_log.write_text("", encoding="utf-8")
            smoke = subprocess.run(
                [str(script), "--smoke-orchestration"],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(smoke.returncode, 0, smoke.stderr)
            self.assertIn("Direct orchestration probe passed", smoke.stdout)
            self.assertNotIn("OpenClaw smoke test passed", smoke.stdout)
            smoke_calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                sum(line.startswith("agent ") for line in smoke_calls),
                1,
            )

            call_log.write_text("", encoding="utf-8")
            combined = subprocess.run(
                [
                    str(script),
                    "--smoke-openclaw",
                    "--smoke-orchestration",
                ],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(combined.returncode, 0)
            self.assertIn("mutually exclusive", combined.stderr)
            if call_log.exists():
                combined_calls = call_log.read_text(encoding="utf-8").splitlines()
                self.assertFalse(
                    any(line.startswith("agent ") for line in combined_calls)
                )

    def test_double_click_apps_have_valid_bundle_structure(self):
        apps = {
            "启动QQ智能体.app": "start",
            "停止QQ智能体.app": "stop",
            "查看QQ智能体日志.app": "show_logs",
            "打开QQ智能体面板.app": "open_dashboard",
        }

        for app_name, executable_name in apps.items():
            with self.subTest(app=app_name):
                app_dir = ROOT / "apps" / app_name
                executable = app_dir / "Contents" / "MacOS" / executable_name
                info_plist = app_dir / "Contents" / "Info.plist"

                self.assertTrue(app_dir.is_dir())
                self.assertTrue(executable.is_file())
                self.assertTrue(info_plist.is_file())

                syntax = subprocess.run(
                    ["bash", "-n", str(executable)], capture_output=True, text=True
                )
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_double_click_apps_fall_back_to_project_path_when_copied(self):
        apps = {
            "启动QQ智能体.app": "start",
            "停止QQ智能体.app": "stop",
            "查看QQ智能体日志.app": "show_logs",
            "打开QQ智能体面板.app": "open_dashboard",
        }

        for app_name, executable_name in apps.items():
            with self.subTest(app=app_name):
                executable = (
                    ROOT
                    / "apps"
                    / app_name
                    / "Contents"
                    / "MacOS"
                    / executable_name
                )
                content = executable.read_text(encoding="utf-8")

                self.assertIn('${HOME}/qq-chat-bot', content)

    def test_double_click_apps_run_project_scripts_through_bash(self):
        commands = {
            "启动QQ智能体.app": ('start', '/bin/bash "$SERVICE_SCRIPT" start'),
            "停止QQ智能体.app": ('stop', '/bin/bash "$SERVICE_SCRIPT" stop'),
            "查看QQ智能体日志.app": ('show_logs', '/bin/bash "$LOG_SCRIPT" --open'),
            "打开QQ智能体面板.app": (
                'open_dashboard',
                '/bin/bash "$SERVICE_SCRIPT" start',
            ),
        }

        for app_name, (executable_name, expected_command) in commands.items():
            with self.subTest(app=app_name):
                executable = (
                    ROOT
                    / "apps"
                    / app_name
                    / "Contents"
                    / "MacOS"
                    / executable_name
                )
                content = executable.read_text(encoding="utf-8")

                self.assertIn(expected_command, content)

        start_content = (ROOT / "apps" / "启动QQ智能体.app" / "Contents" / "MacOS" / "start").read_text(encoding="utf-8")
        stop_content = (ROOT / "apps" / "停止QQ智能体.app" / "Contents" / "MacOS" / "stop").read_text(encoding="utf-8")
        self.assertIn('/bin/bash "$WATCHDOG_SCRIPT" start', start_content)
        self.assertLess(stop_content.index('/bin/bash "$WATCHDOG_SCRIPT" stop'), stop_content.index('/bin/bash "$SERVICE_SCRIPT" stop'))
        for content in (start_content, stop_content):
            self.assertNotIn("kill -9", content)
            self.assertNotIn("nohup", content)
            self.assertNotIn("qq_openclaw_bridge", content)

    def test_show_logs_script_has_valid_bash_syntax_and_help(self):
        script = ROOT / "scripts" / "show_logs.sh"

        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Usage: scripts/show_logs.sh", help_result.stdout)

    def test_dashboard_service_script_has_valid_bash_syntax_and_help(self):
        script = ROOT / "scripts" / "dashboard_service.sh"

        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Usage: scripts/dashboard_service.sh", help_result.stdout)

    def test_watchdog_service_script_has_valid_bash_syntax_and_help(self):
        script = ROOT / "scripts" / "watchdog_service.sh"

        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Usage: scripts/watchdog_service.sh", help_result.stdout)

    def test_style_learner_service_script_is_safe_launchctl_wrapper(self):
        script = ROOT / "scripts" / "style_learner_service.sh"

        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        help_result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Usage: scripts/style_learner_service.sh", help_result.stdout)

        content = script.read_text(encoding="utf-8")
        self.assertIn("com.chichu.qq-style-learner", content)
        self.assertIn('gui/$(id -u)', content)
        for command in ("bootstrap", "bootout", "kickstart", "print"):
            self.assertIn(command, content)
        self.assertIn("managed_pid", content)
        self.assertIn("qq_openclaw_style_learner", content)
        self.assertIn("logs/style-learner.log", content)
        self.assertNotIn("nohup", content)
        self.assertNotIn("kill -9", content)
        self.assertNotIn("ollama pull", content.lower())
        self.assertNotIn("ollama install", content.lower())

    def test_style_learner_start_rerenders_stale_plist_after_project_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "moved project"
            scripts = project / "scripts"
            launchd = project / "launchd"
            scripts.mkdir(parents=True)
            launchd.mkdir()
            for name in ("style_learner_service.sh", "install_launch_agents.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            for template in (ROOT / "launchd").glob("*.plist.template"):
                shutil.copy2(template, launchd / template.name)
            (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)

            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            target = agents / "com.chichu.qq-style-learner.plist"
            target.write_text("stale wrong project", encoding="utf-8")
            call_log = root / "launchctl.log"
            fake_launchctl = root / "launchctl"
            fake_launchctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$CALL_LOG\"\n"
                "[[ \"${1:-}\" == print ]] && exit 1\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_launchctl.chmod(0o755)
            fake_pgrep = root / "pgrep"
            fake_pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            fake_pgrep.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "HOME": str(home),
                "CALL_LOG": str(call_log),
                "CHICHU_LAUNCHCTL_BIN": str(fake_launchctl),
                "CHICHU_PGREP_BIN": str(fake_pgrep),
            })

            result = subprocess.run(
                [str(scripts / "style_learner_service.sh"), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = plistlib.loads(target.read_bytes())
            self.assertEqual(payload["WorkingDirectory"], str(project.resolve()))
            self.assertEqual(
                payload["ProgramArguments"][-1], str(project.resolve())
            )
            calls = call_log.read_text(encoding="utf-8")
            self.assertIn("bootstrap", calls)
            self.assertIn("kickstart", calls)

    def test_style_learner_start_rejects_symlink_plist_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            scripts = project / "scripts"
            launchd = project / "launchd"
            scripts.mkdir(parents=True)
            launchd.mkdir()
            for name in ("style_learner_service.sh", "install_launch_agents.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            for template in (ROOT / "launchd").glob("*.plist.template"):
                shutil.copy2(template, launchd / template.name)
            (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            outside = root / "outside.plist"
            outside.write_text("do not overwrite", encoding="utf-8")
            (agents / "com.chichu.qq-style-learner.plist").symlink_to(outside)
            call_log = root / "launchctl.log"
            fake_launchctl = root / "launchctl"
            fake_launchctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$CALL_LOG\"\n"
                "[[ \"${1:-}\" == print ]] && exit 1\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_launchctl.chmod(0o755)
            fake_pgrep = root / "pgrep"
            fake_pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            fake_pgrep.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "HOME": str(home),
                "CALL_LOG": str(call_log),
                "CHICHU_LAUNCHCTL_BIN": str(fake_launchctl),
                "CHICHU_PGREP_BIN": str(fake_pgrep),
            })

            result = subprocess.run(
                [str(scripts / "style_learner_service.sh"), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(outside.read_text(encoding="utf-8"), "do not overwrite")
            calls = call_log.read_text(encoding="utf-8")
            self.assertNotIn("bootstrap", calls)
            self.assertNotIn("kickstart", calls)

    def test_style_learner_start_reloads_loaded_job_after_safe_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, _home, call_log, environment = self._style_learner_service_harness(
                Path(temporary), loaded=True
            )

            result = subprocess.run(
                [str(project / "scripts" / "style_learner_service.sh"), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            target = "gui/%d/com.chichu.qq-style-learner" % os.getuid()
            plist = environment["HOME"] + "/Library/LaunchAgents/com.chichu.qq-style-learner.plist"
            self.assertEqual(
                self._launchctl_mutations(call_log),
                [
                    "bootout " + target,
                    "bootstrap gui/%d %s" % (os.getuid(), plist),
                    "kickstart -k " + target,
                ],
            )

    def test_style_learner_start_bootstraps_unloaded_job_without_bootout(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, _home, call_log, environment = self._style_learner_service_harness(
                Path(temporary), loaded=False
            )

            result = subprocess.run(
                [str(project / "scripts" / "style_learner_service.sh"), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            target = "gui/%d/com.chichu.qq-style-learner" % os.getuid()
            plist = environment["HOME"] + "/Library/LaunchAgents/com.chichu.qq-style-learner.plist"
            self.assertEqual(
                self._launchctl_mutations(call_log),
                [
                    "bootstrap gui/%d %s" % (os.getuid(), plist),
                    "kickstart -k " + target,
                ],
            )

    def test_style_learner_start_render_failure_does_not_bootout_loaded_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, home, call_log, environment = self._style_learner_service_harness(
                root, loaded=True
            )
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            outside = root / "outside.plist"
            outside.write_text("do not overwrite", encoding="utf-8")
            (agents / "com.chichu.qq-style-learner.plist").symlink_to(outside)

            result = subprocess.run(
                [str(project / "scripts" / "style_learner_service.sh"), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self._launchctl_mutations(call_log), [])
            self.assertNotIn("started", result.stdout.lower())
            self.assertEqual(outside.read_text(encoding="utf-8"), "do not overwrite")

    def test_style_learner_start_bootout_failure_does_not_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, _home, call_log, environment = self._style_learner_service_harness(
                Path(temporary), loaded=True, fail_command="bootout"
            )

            result = subprocess.run(
                [str(project / "scripts" / "style_learner_service.sh"), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            target = "gui/%d/com.chichu.qq-style-learner" % os.getuid()
            self.assertEqual(self._launchctl_mutations(call_log), ["bootout " + target])
            self.assertNotIn("started", result.stdout.lower())

    def test_style_learner_start_bootstrap_failure_does_not_kickstart(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, _home, call_log, environment = self._style_learner_service_harness(
                Path(temporary), loaded=False, fail_command="bootstrap"
            )

            result = subprocess.run(
                [str(project / "scripts" / "style_learner_service.sh"), "start"],
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            plist = environment["HOME"] + "/Library/LaunchAgents/com.chichu.qq-style-learner.plist"
            self.assertEqual(
                self._launchctl_mutations(call_log),
                ["bootstrap gui/%d %s" % (os.getuid(), plist)],
            )
            self.assertNotIn("started", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
