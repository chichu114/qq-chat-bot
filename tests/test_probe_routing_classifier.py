import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def _routing_config_digest(config):
    canonical = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _routing_config():
    return {
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


def _create_routing_database(path, config=None, digest=None):
    config = _routing_config() if config is None else config
    digest = _routing_config_digest(config) if digest is None else digest
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE routing_config_versions "
            "(version INTEGER, config_json TEXT, config_sha256 TEXT, status TEXT)"
        )
        connection.execute(
            "INSERT INTO routing_config_versions VALUES (1, ?, ?, 'active')",
            (json.dumps(config), digest),
        )


def _probe_env(database):
    return {
        "ONEBOT_WS_URL": "ws://127.0.0.1:1",
        "ENABLE_SHADOW_ROUTING": "true",
        "ENABLE_SQLITE_STORE": "true",
        "STATE_DATABASE_PATH": str(database),
        "ROUTING_CLASSIFIER_TRANSPORT": "responses_api",
        "ROUTING_AGENT_ID": "routing",
    }


class ProbeRoutingClassifierTests(unittest.TestCase):
    def test_probe_loads_validated_active_config_and_uses_direct_adapter(self):
        from probe_routing_classifier import probe_routing_classifier
        from qq_openclaw_bridge.models import AgentResponse

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            _create_routing_database(database)
            calls = []

            class Adapter:
                def reply(self, request):
                    calls.append(("reply", request))
                    return AgentResponse(
                        text=(
                            '{"level":"L1","confidence":0.9,'
                            '"task_kind":"smalltalk","reasons":["probe"],'
                            '"brainstorm_required":false}'
                        ),
                        provider="openai",
                        model="gpt-5.4-nano-2026-08-01",
                    )

            def adapter_factory(**kwargs):
                calls.append(("factory", kwargs))
                return Adapter()

            decision = probe_routing_classifier(
                env=_probe_env(database), adapter_factory=adapter_factory
            )

        self.assertEqual(decision.level, "L1")
        factory_kwargs = calls[0][1]
        self.assertEqual(factory_kwargs["model"], "openai/gpt-5.4-nano")
        self.assertEqual(factory_kwargs["timeout_seconds"], 15)
        self.assertEqual(factory_kwargs["routing_agent_id"], "routing")
        request = calls[1][1]
        self.assertIn("task_kind", request.text)
        self.assertIn("你好，这是一次无害的问候。", request.text)

    def test_probe_rejects_invalid_hash_or_multiple_active_configs_before_adapter(self):
        from probe_routing_classifier import probe_routing_classifier

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            _create_routing_database(database, digest="0" * 64)
            calls = []
            with self.assertRaises(RuntimeError):
                probe_routing_classifier(
                    env=_probe_env(database),
                    adapter_factory=lambda **kwargs: calls.append(kwargs),
                )
            self.assertEqual(calls, [])

            with sqlite3.connect(database) as connection:
                config = _routing_config()
                connection.execute(
                    "UPDATE routing_config_versions SET config_sha256=?",
                    (_routing_config_digest(config),),
                )
                connection.execute(
                    "INSERT INTO routing_config_versions VALUES (2, ?, ?, 'active')",
                    (json.dumps(config), _routing_config_digest(config)),
                )
            with self.assertRaises(RuntimeError):
                probe_routing_classifier(
                    env=_probe_env(database),
                    adapter_factory=lambda **kwargs: calls.append(kwargs),
                )
            self.assertEqual(calls, [])

    def test_probe_main_uses_uniform_safe_output(self):
        from probe_routing_classifier import main

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            _create_routing_database(database)

            def failing_factory(**kwargs):
                raise RuntimeError("secret-key raw-response-body")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = main(
                    env=_probe_env(database), adapter_factory=failing_factory
                )

        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "[ERROR] routing classifier probe failed\n")
        self.assertNotIn("secret-key", stderr.getvalue())
        self.assertNotIn("raw-response-body", stderr.getvalue())

    def test_check_ready_direct_probe_calls_helper_without_routing_openclaw_agent(self):
        source_script = ROOT / "scripts" / "check_ready.sh"
        with tempfile.TemporaryDirectory(prefix="chichu-direct-ready-") as directory:
            project = Path(directory)
            scripts = project / "scripts"
            scripts.mkdir()
            script = scripts / "check_ready.sh"
            script.write_bytes(source_script.read_bytes())
            script.chmod(0o755)
            (project / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)

            helper_log = project / "helper.log"
            (scripts / "probe_routing_classifier.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['PROBE_HELPER_LOG']).write_text('called\\n')\n",
                encoding="utf-8",
            )
            database = project / "state.sqlite3"
            _create_routing_database(database)

            home = project / "home"
            auth_path = (
                home / ".openclaw" / "agents" / "routing" /
                "agent" / "codex-home" / "auth.json"
            )
            auth_path.parent.mkdir(parents=True)
            auth_path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "test-key"}),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)

            openclaw_log = project / "openclaw.log"
            fake_openclaw = scripts / "fake-openclaw"
            fake_openclaw.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_OPENCLAW_LOG\"\n"
                "case \"${1:-}\" in\n"
                "  --version) printf 'OpenClaw test\\n' ;;\n"
                "  agents) printf '[{\"id\":\"qq\"}]\\n' ;;\n"
                "  agent) exit 91 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_openclaw.chmod(0o755)
            env_file = project / ".env"
            env_file.write_text(
                "\n".join((
                    "ONEBOT_WS_URL=ws://127.0.0.1:1",
                    "ALLOWED_QQ_USERS=10001",
                    "AGENT_ADAPTER=openclaw-cli",
                    "OPENCLAW_COMMAND=%s" % fake_openclaw,
                    "OPENCLAW_AGENT_ID=qq",
                    "OPENCLAW_USE_GATEWAY=false",
                    "ROUTING_CLASSIFIER_TRANSPORT=responses_api",
                    "ROUTING_AGENT_ID=routing",
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
                )),
                encoding="utf-8",
            )
            env_file.chmod(0o600)
            environment = os.environ.copy()
            environment.update({
                "HOME": str(home),
                "FAKE_OPENCLAW_LOG": str(openclaw_log),
                "PROBE_HELPER_LOG": str(helper_log),
                "TMPDIR": str(project),
            })

            result = subprocess.run(
                [str(script), "--probe-routing"],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            helper_output = helper_log.read_text(encoding="utf-8") if helper_log.exists() else ""
            openclaw_calls = (
                openclaw_log.read_text(encoding="utf-8").splitlines()
                if openclaw_log.exists()
                else []
            )
            env_file.write_text(
                env_file.read_text(encoding="utf-8").replace(
                    "ROUTING_CLASSIFIER_TRANSPORT=responses_api",
                    'ROUTING_CLASSIFIER_TRANSPORT="  responses_api  "',
                ),
                encoding="utf-8",
            )
            helper_log.unlink()
            openclaw_log.unlink()
            spaced_result = subprocess.run(
                [str(script), "--probe-routing"],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )
            spaced_helper_output = (
                helper_log.read_text(encoding="utf-8") if helper_log.exists() else ""
            )
            spaced_openclaw_calls = (
                openclaw_log.read_text(encoding="utf-8").splitlines()
                if openclaw_log.exists()
                else []
            )
            env_file.write_text(
                env_file.read_text(encoding="utf-8").replace(
                    'ROUTING_CLASSIFIER_TRANSPORT="  responses_api  "',
                    'ROUTING_CLASSIFIER_TRANSPORT=""',
                ),
                encoding="utf-8",
            )
            empty_result = subprocess.run(
                [str(script), "--probe-routing"],
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(helper_output, "called\n")
        self.assertFalse(any(call.startswith("agent ") for call in openclaw_calls))
        self.assertIn("routing probe passed", result.stdout)
        self.assertEqual(spaced_result.returncode, 0, spaced_result.stderr)
        self.assertEqual(spaced_helper_output, "called\n")
        self.assertFalse(
            any(call.startswith("agent ") for call in spaced_openclaw_calls)
        )
        self.assertIn("routing probe passed", spaced_result.stdout)
        self.assertNotEqual(empty_result.returncode, 0)
        self.assertIn(
            "ROUTING_CLASSIFIER_TRANSPORT must be openclaw or responses_api",
            empty_result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
