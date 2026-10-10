from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class DashboardServiceTests(unittest.TestCase):
    def test_command_service_rejects_non_whitelisted_action(self):
        from qq_openclaw_dashboard.commands import DashboardCommandService

        service = DashboardCommandService(
            housekeeper_factory=lambda: None,
            style_factory=lambda: None,
            project_dir=Path("/tmp"),
            env_provider=lambda: {},
            backup_manager=None,
            runner=lambda *args, **kwargs: None,
            bridge_script=Path("bridge.sh"),
            watchdog_script=Path("watchdog.sh"),
            bridge_pid_path=Path("bridge.pid"),
        )
        with self.assertRaisesRegex(ValueError, "不支持"):
            service.service_action("destroy")

    def test_dashboard_state_exposes_bounded_e4_data_without_secrets(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_dashboard.queries import DashboardQueryService

        class Backups:
            interval_hours = 24
            retention_days = 30
            max_auto_backups = 30

            @staticmethod
            def list_backups():
                return [
                    {
                        "name": "/private/BACKUP-PATH/backup-safe.chichu-backup",
                        "size": 42,
                        "mtime": "2026-07-30T11:00:00+00:00",
                        "created_at": "2026-07-30T10:00:00+00:00",
                        "kind": "manual",
                        "encryption": "encrypted",
                        "valid": True,
                        "file_count": 3,
                        "path": "/private/BACKUP-OBJECT-PATH",
                        "url": (
                            "https://backup-user:BACKUP-URL-SECRET@"
                            "example.invalid/private"
                        ),
                        "detail": "BACKUP-DETAIL-SECRET",
                    }
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            logs = project / "logs"
            logs.mkdir()
            workspace = project / "workspace"
            workspace.mkdir()
            paths = {
                "memory": workspace / "MEMORY.md",
                "todo": workspace / "TODO.md",
                "style": workspace / "STYLE.md",
                "reminder": workspace / "REMINDER_STATE.md",
            }
            (project / ".env").write_text(
                "MEDIA_API_DAILY_BUDGET=1.25\n"
                "MEDIA_API_KEY=env-secret\n"
                "MEDIA_API_SECRET_REF=keychain:qq-openclaw-media:media-api\n"
                "ENABLE_MEDIA_PIPELINE=true\n"
                "MEDIA_API_BASE_URL=https://media.example/v1\n"
                "MEDIA_IMAGE_MODEL=vision-model\n"
                "MEDIA_AUDIO_MODEL=audio-model\n"
                "MEDIA_API_TIMEOUT_SECONDS=45\n"
                "MEDIA_API_REQUESTS_PER_MINUTE=8\n"
                "MEDIA_MAX_CONCURRENT_CALLS=2\n"
                "AGENT_ADAPTER=https://ADAPTER-SECRET.invalid/private\n"
                "OPENCLAW_AGENT_ID=AGENTIDSECRET\n"
                "OPENCLAW_MODEL=CONFIGMODELSECRET\n"
                "MEMORY_LIGHT_MODEL=LIGHTMODELSECRET\n"
                "ONEBOT_WS_URL=ws://onebot-user:ONEBOT-PASSWORD@"
                "127.0.0.1:3001/private?token=ONEBOT-TOKEN#fragment\n"
                "OPENCLAW_GATEWAY_PROBE_URL=ws://gateway-user:GATEWAY-PASSWORD@"
                "127.0.0.1:18789/private?token=GATEWAY-TOKEN#fragment\n"
                "HTTP_PROXY=http://proxy-user:PROXY-PASSWORD@127.0.0.1:8080\n",
                encoding="utf-8",
            )
            (logs / "bridge.log").write_text(
                '{"API_KEY":"LOG-JSON-SECRET"} '
                "Authorization: Bearer LOG-BEARER-SECRET "
                "X-Api-Key: LOG-HEADER-SECRET "
                "command=rm -rf /private/LOG-COMMAND-PATH\n",
                encoding="utf-8",
            )
            (logs / "watchdog.pid").write_text(str(os.getpid()), encoding="utf-8")
            (logs / "health.json").write_text(
                json.dumps(
                    {
                        "status": "degraded",
                        "message": (
                            "rm -rf /private/HEALTH-PATH "
                            "https://health-user:HEALTH-URL-SECRET@"
                            "example.invalid/private"
                        ),
                        "detail": "HEALTH-DETAIL-SECRET",
                        "checks": {
                            "bridge": True,
                            "onebot": False,
                            "proxy": True,
                            "openclaw": True,
                            "queue": True,
                        },
                        "restart_count": 2,
                        "updated_at": "https://health.invalid/HEALTH-TIMESTAMP-SECRET",
                    }
                ),
                encoding="utf-8",
            )
            store = WorkspaceStateStore(workspace / "state.sqlite3")
            store.ensure_documents(
                {
                    "memory": (paths["memory"], "# MEMORY\n"),
                    "todo": (paths["todo"], "# TODO\n\n## 待办\n"),
                    "style": (paths["style"], "# STYLE\n"),
                    "reminder_state": (paths["reminder"], "status=active\n"),
                }
            )
            now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
            task = store.e4_repository.create_task(1, "m-1", "media", now=now)
            store.e4_repository.record_model_call(
                "image",
                "https://model-user:MODEL-PROVIDER-PASS@example.invalid/private",
                "--token MODEL-COMMAND-SECRET /private/MODEL-PATH",
                True,
                input_tokens=10,
                output_tokens=2,
                duration_ms=125,
                estimated_cost=0.25,
                now=now - timedelta(hours=1),
            )
            store.e4_repository.record_health_change(
                "openclaw", "healthy", "ok", now=now
            )
            store.save_approval(
                "PUBLIC-ID",
                1,
                "rm private-file --token APPROVAL-SECRET",
                "rm -rf /private/APPROVAL-PATH --token APPROVAL-DESCRIPTION-SECRET",
                "later",
                now.isoformat(),
            )
            store.e4_repository.create_media_asset(
                1,
                "m-media",
                "image",
                status="ready",
                error_detail="https://private/media?id=secret",
                cache_path="/private/cache/secret.png",
                now=now,
            )
            store.e4_repository.create_media_asset(
                1,
                "m-malformed",
                "image",
                status="MEDIA_API_KEY=https://private/media?id=secret",
                now=now,
            )
            service = DashboardQueryService(
                project,
                paths["memory"],
                paths["todo"],
                paths["style"],
                paths["reminder"],
                store,
                Backups(),
                lambda: now,
                onebot_probe=lambda: False,
                launch_agent_probe=lambda label: (True, 12345),
            )

            state = service.get_state()
            serialized = json.dumps(state, ensure_ascii=False)

            self.assertEqual(state["tasks"][0]["id"], task["id"])
            self.assertEqual(state["model_stats"][0]["p50_latency_ms"], 125)
            self.assertEqual(state["model_stats"][0]["p95_latency_ms"], 125)
            self.assertEqual(state["model_stats"][0]["today_estimated_cost"], 0.25)
            self.assertEqual(state["model_stats"][0]["daily_budget"], 1.25)
            self.assertEqual(state["model_stats"][0]["provider"], "unknown")
            self.assertEqual(state["model_stats"][0]["model"], "unknown")
            self.assertEqual(state["approvals"][0]["approval_id"], "PUBLIC-ID")
            self.assertEqual(
                state["approvals"][0]["description"], "待确认的敏感操作"
            )
            self.assertEqual(state["health_timeline"][0]["component"], "openclaw")
            self.assertIn("total", state["media"])
            self.assertEqual(
                state["media"]["settings"],
                {
                    "enabled": True,
                    "base_url": "https://media.example/v1",
                    "image_model": "vision-model",
                    "audio_model": "audio-model",
                    "timeout_seconds": 45,
                    "requests_per_minute": 8,
                    "daily_budget": 1.25,
                    "max_concurrent_calls": 2,
                    "secret_configured": True,
                },
            )
            self.assertEqual(state["config"]["adapter"], "unknown")
            self.assertEqual(state["config"]["agent_id"], "configured")
            self.assertEqual(state["config"]["model"], "configured")
            self.assertEqual(state["config"]["memory_light_model"], "configured")
            self.assertEqual(state["memory2"]["light_model"], "configured")
            self.assertEqual(state["config"]["onebot_url"], "127.0.0.1:3001")
            self.assertEqual(state["config"]["gateway_url"], "127.0.0.1:18789")
            self.assertTrue(state["config"]["proxy_configured"])
            self.assertEqual(
                state["logs"],
                "运行日志已隐藏，请使用本机日志查看工具。",
            )
            self.assertEqual(state["health"]["status"], "degraded")
            self.assertEqual(
                state["health"]["message"], "自愈监控检测到异常。"
            )
            self.assertIsNone(state["health"]["updated_at"])
            self.assertEqual(
                state["backups"],
                [
                    {
                        "name": "backup-safe.chichu-backup",
                        "size": 42,
                        "mtime": "2026-07-30T11:00:00+00:00",
                        "created_at": "2026-07-30T10:00:00+00:00",
                        "kind": "manual",
                        "encryption": "encrypted",
                        "valid": True,
                        "file_count": 3,
                    }
                ],
            )
            for secret in (
                "MEDIA_API_KEY",
                "env-secret",
                "MEDIA_API_SECRET_REF",
                "keychain:qq-openclaw-media:media-api",
                "ADAPTER-SECRET",
                "AGENTIDSECRET",
                "CONFIGMODELSECRET",
                "LIGHTMODELSECRET",
                "ONEBOT-PASSWORD",
                "ONEBOT-TOKEN",
                "GATEWAY-PASSWORD",
                "GATEWAY-TOKEN",
                "PROXY-PASSWORD",
                "LOG-JSON-SECRET",
                "LOG-BEARER-SECRET",
                "LOG-HEADER-SECRET",
                "LOG-COMMAND-PATH",
                "BACKUP-PATH",
                "BACKUP-OBJECT-PATH",
                "BACKUP-URL-SECRET",
                "BACKUP-DETAIL-SECRET",
                "MODEL-PROVIDER-PASS",
                "MODEL-COMMAND-SECRET",
                "MODEL-PATH",
                "APPROVAL-SECRET",
                "rm private-file",
                "APPROVAL-PATH",
                "APPROVAL-DESCRIPTION-SECRET",
                "HEALTH-PATH",
                "HEALTH-URL-SECRET",
                "HEALTH-DETAIL-SECRET",
                "HEALTH-TIMESTAMP-SECRET",
            ):
                self.assertNotIn(secret, serialized)

    def test_dashboard_state_has_safe_e4_defaults_without_a_store(self):
        from qq_openclaw_dashboard.queries import DashboardQueryService

        class Backups:
            interval_hours = 24
            retention_days = 30
            max_auto_backups = 30

            @staticmethod
            def list_backups():
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            workspace = project / "workspace"
            workspace.mkdir()
            service = DashboardQueryService(
                project,
                workspace / "MEMORY.md",
                workspace / "TODO.md",
                workspace / "STYLE.md",
                workspace / "REMINDER_STATE.md",
                None,
                Backups(),
                lambda: datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
                onebot_probe=lambda: False,
                launch_agent_probe=lambda label: (False, None),
            )

            state = service.get_state()

            self.assertEqual(state["tasks"], [])
            self.assertEqual(state["model_stats"], [])
            self.assertEqual(state["approvals"], [])
            self.assertEqual(state["health_timeline"], [])
            self.assertEqual(
                state["media"],
                {
                    "total": 0,
                    "settings": {
                        "enabled": False,
                        "base_url": "",
                        "image_model": "",
                        "audio_model": "",
                        "timeout_seconds": 90,
                        "requests_per_minute": 10,
                        "daily_budget": None,
                        "max_concurrent_calls": 2,
                        "secret_configured": False,
                    },
                },
            )


if __name__ == "__main__":
    unittest.main()
