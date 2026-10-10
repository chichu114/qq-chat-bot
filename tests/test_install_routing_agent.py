import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from install_routing_agent import ROUTING_AGENT_DENY, install_routing_agent, main


class InstallRoutingAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config_path = Path(self.temporary_directory.name) / "openclaw.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "token": "routing-test-secret",
                    "agents": {
                        "defaults": {"models": {"openai/gpt-5": {}}},
                        "list": [
                            {
                                "id": "qq",
                                "model": "openai/gpt-5",
                                "tools": {"alsoAllow": ["message"]},
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_installer_adds_dedicated_toolless_routing_agent(self):
        install_routing_agent(self.config_path, "openai/gpt-5.4-nano")

        config = self.read_config()
        routing = next(
            agent for agent in config["agents"]["list"] if agent["id"] == "routing"
        )
        self.assertEqual(routing["model"], "openai/gpt-5.4-nano")
        self.assertEqual(routing["skills"], [])
        self.assertEqual(routing["tools"]["profile"], "minimal")
        self.assertEqual(routing["tools"]["alsoAllow"], [])
        self.assertEqual(routing["tools"]["allow"], [])
        self.assertFalse(routing["tools"]["elevated"]["enabled"])
        self.assertEqual(routing["tools"]["deny"], ROUTING_AGENT_DENY)
        for tool in (
            "code_execution",
            "group:runtime",
            "group:fs",
            "group:web",
            "group:memory",
            "group:sessions",
            "group:ui",
            "group:media",
            "canvas",
            "image",
            "pdf",
            "tts",
            "image_generate",
            "music_generate",
            "video_generate",
            "skill_workshop",
            "get_goal",
            "heartbeat_respond",
        ):
            self.assertIn(tool, ROUTING_AGENT_DENY)
        self.assertIn("openai/gpt-5.4-nano", config["agents"]["defaults"]["models"])
        self.assertEqual(
            next(agent for agent in config["agents"]["list"] if agent["id"] == "qq")[
                "tools"
            ],
            {"alsoAllow": ["message"]},
        )

    def test_installer_isolates_routing_workspace_context_and_inference(self):
        install_routing_agent(self.config_path, "openai/gpt-5.4-nano")

        routing = next(
            agent
            for agent in self.read_config()["agents"]["list"]
            if agent["id"] == "routing"
        )
        self.assertEqual(
            {
                key: routing.get(key)
                for key in (
                    "workspace",
                    "contextInjection",
                    "thinkingDefault",
                    "reasoningDefault",
                    "fastModeDefault",
                    "bootstrapMaxChars",
                    "bootstrapTotalMaxChars",
                )
            },
            {
                "workspace": str(self.config_path.parent / "workspace" / "routing"),
                "contextInjection": "never",
                "thinkingDefault": "off",
                "reasoningDefault": "off",
                "fastModeDefault": True,
                "bootstrapMaxChars": 1,
                "bootstrapTotalMaxChars": 1,
            },
        )
        self.assertEqual(routing["skills"], [])
        self.assertEqual(routing["tools"]["allow"], [])
        self.assertEqual(routing["tools"]["alsoAllow"], [])
        self.assertEqual(routing["tools"]["deny"], ROUTING_AGENT_DENY)
        self.assertFalse(routing["tools"]["elevated"]["enabled"])

    def test_installer_uses_private_atomic_config_and_timestamped_backup(self):
        original = self.config_path.read_text(encoding="utf-8")
        backup = install_routing_agent(self.config_path, "openai/gpt-5.4-nano")

        self.assertTrue(backup.is_file())
        self.assertIn("openclaw-before-routing-agent-", backup.name)
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertEqual(os.stat(self.config_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(backup.parent).st_mode & 0o777, 0o700)

    def test_installer_rejects_duplicate_routing_agents_without_writing(self):
        payload = self.read_config()
        payload["agents"]["list"].extend(
            [{"id": "routing"}, {"id": "routing"}]
        )
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")
        before = self.config_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "duplicate.*routing"):
            install_routing_agent(self.config_path, "openai/gpt-5.4-nano")

        self.assertEqual(self.config_path.read_bytes(), before)

    def test_installer_accepts_128_character_model_and_rejects_129_without_writing(self):
        accepted = "a" * 128
        install_routing_agent(self.config_path, accepted)
        self.assertIn(accepted, self.read_config()["agents"]["defaults"]["models"])

        self.setUp()
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "bounded identifier"):
            install_routing_agent(self.config_path, "a" * 129)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_installer_hardens_existing_backup_directory_and_rejects_symlink(self):
        backup_dir = Path(self.temporary_directory.name) / "backups"
        backup_dir.mkdir(mode=0o755)
        os.chmod(backup_dir, 0o755)
        backup = install_routing_agent(
            self.config_path, "openai/gpt-5.4-nano", backup_dir=backup_dir
        )
        self.assertTrue(backup.is_file())
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)

        self.setUp()
        target = Path(self.temporary_directory.name) / "target"
        target.mkdir()
        symlink = Path(self.temporary_directory.name) / "backup-link"
        symlink.symlink_to(target, target_is_directory=True)
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "symlink"):
            install_routing_agent(
                self.config_path, "openai/gpt-5.4-nano", backup_dir=symlink
            )
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_installer_rejects_backup_directory_not_owned_by_current_user(self):
        backup_dir = Path(self.temporary_directory.name) / "backups"
        backup_dir.mkdir()
        before = self.config_path.read_bytes()
        with mock.patch("install_routing_agent.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "owned"):
                install_routing_agent(
                    self.config_path, "openai/gpt-5.4-nano", backup_dir=backup_dir
                )
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_installer_rolls_back_config_and_removes_backup_on_filesystem_faults(self):
        faults = {
            "write": mock.patch(
                "install_routing_agent._write_bytes", side_effect=OSError("write")
            ),
            "chmod": mock.patch(
                "install_routing_agent._chmod_private", side_effect=OSError("chmod")
            ),
            "file fsync": mock.patch(
                "install_routing_agent._fsync_file", side_effect=OSError("fsync")
            ),
            "replace": mock.patch(
                "install_routing_agent.os.replace", side_effect=OSError("replace")
            ),
        }
        for name, fault in faults.items():
            with self.subTest(fault=name):
                self.setUp()
                before = self.config_path.read_bytes()
                with fault, self.assertRaises(OSError):
                    install_routing_agent(self.config_path, "openai/gpt-5.4-nano")
                self.assertEqual(self.config_path.read_bytes(), before)
                self.assertFalse(list(self.config_path.parent.glob("routing-agent-config-backups/*")))
                self.assertFalse(list(self.config_path.parent.glob(".routing-agent-*")))

    def test_installer_rolls_back_after_directory_fsync_fault(self):
        for failing_call in (3, 4, 5):
            with self.subTest(failing_call=failing_call):
                self.setUp()
                before = self.config_path.read_bytes()
                original_fsync = __import__("install_routing_agent")._fsync_directory
                calls = []

                def fail_after_backup_directory_setup(directory):
                    calls.append(directory)
                    if len(calls) == failing_call:
                        raise OSError("directory fsync")
                    return original_fsync(directory)

                with mock.patch(
                    "install_routing_agent._fsync_directory",
                    side_effect=fail_after_backup_directory_setup,
                ), self.assertRaises(OSError):
                    install_routing_agent(self.config_path, "openai/gpt-5.4-nano")

                self.assertEqual(self.config_path.read_bytes(), before)
                self.assertFalse(list(self.config_path.parent.glob("routing-agent-config-backups/*")))
                self.assertFalse(list(self.config_path.parent.glob(".routing-agent-*")))

    def test_installer_deny_policy_covers_current_high_risk_catalog_ids(self):
        catalog = (ROOT / "node_modules" / "openclaw" / "dist" / "tool-catalog-CJ8FQUeU.js").read_text(
            encoding="utf-8"
        )
        high_risk = {
            "code_execution",
            "image_generate",
            "music_generate",
            "video_generate",
            "skill_workshop",
        }
        self.assertTrue(high_risk.issubset(ROUTING_AGENT_DENY))
        for tool in high_risk:
            self.assertIn('"%s"' % tool, catalog)

    def test_installer_rolls_back_when_backup_replace_fails_after_config_replace(self):
        before = self.config_path.read_bytes()
        replace = os.replace
        calls = []

        def fail_backup_replace(source, destination):
            calls.append((source, destination))
            if len(calls) == 2:
                raise OSError("backup replace")
            return replace(source, destination)

        with mock.patch(
            "install_routing_agent.os.replace", side_effect=fail_backup_replace
        ), self.assertRaises(OSError):
            install_routing_agent(self.config_path, "openai/gpt-5.4-nano")

        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(list(self.config_path.parent.glob("routing-agent-config-backups/*")))
        self.assertFalse(list(self.config_path.parent.glob(".routing-agent-*")))

    def test_cli_output_does_not_include_configured_secret(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "--config",
                    str(self.config_path),
                    "--model",
                    "openai/gpt-5.4-nano",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertNotIn("routing-test-secret", stdout.getvalue())
        self.assertNotIn('"token"', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
