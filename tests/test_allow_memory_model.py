import json
import tempfile
import unittest
from pathlib import Path


class AllowMemoryModelScriptTests(unittest.TestCase):
    def test_adds_model_without_changing_restricted_agent_tools(self):
        from scripts.allow_memory_model import allow_memory_model

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "openclaw.json"
            backup_dir = root / "backups"
            tools = {"deny": ["exec", "process", "write"], "elevated": {"enabled": False}}
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {
                                "models": {"openai/gpt-5.5": {"alias": "GPT"}}
                            },
                            "list": [{"id": "qq", "tools": tools}],
                        }
                    }
                ),
                encoding="utf-8",
            )

            backup = allow_memory_model(
                config_path,
                "openai/gpt-5.4-nano",
                restricted_agent_id="qq",
                backup_dir=backup_dir,
            )

            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIn(
                "openai/gpt-5.4-nano", updated["agents"]["defaults"]["models"]
            )
            self.assertEqual(updated["agents"]["list"][0]["tools"], tools)
            self.assertTrue(backup.is_file())


if __name__ == "__main__":
    unittest.main()

