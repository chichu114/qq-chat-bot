from pathlib import Path
import json
import stat
import sys
import tempfile
import threading
import unittest
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _RunResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DashboardE4CommandTests(unittest.TestCase):
    def _service(self, project, runner=None):
        from qq_openclaw_dashboard.commands import DashboardCommandService

        return DashboardCommandService(
            housekeeper_factory=lambda: None,
            style_factory=lambda: None,
            project_dir=project,
            env_provider=lambda: {},
            backup_manager=None,
            runner=runner or (lambda *args, **kwargs: _RunResult()),
            bridge_script=project / "bridge.sh",
            watchdog_script=project / "watchdog.sh",
            bridge_pid_path=project / "bridge.pid",
        )

    def test_media_settings_update_only_allowlisted_non_secret_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            env_path = project / ".env"
            env_path.write_text(
                "OPENAI_API_KEY=keep-secret\n"
                "MEDIA_API_KEY=legacy-secret\n"
                "MEDIA_API_TIMEOUT_SECONDS=90\n",
                encoding="utf-8",
            )
            env_path.chmod(0o644)
            service = self._service(project)

            message = service.update_media_settings(
                {
                    "enabled": True,
                    "base_url": "https://media.example/v1",
                    "image_model": "vision-model",
                    "audio_model": "audio-model",
                    "timeout_seconds": 45,
                    "requests_per_minute": 8,
                    "daily_budget": "1.25",
                    "max_concurrent_calls": 2,
                }
            )

            content = env_path.read_text(encoding="utf-8")
            self.assertIn("已更新", message)
            self.assertIn("ENABLE_MEDIA_PIPELINE=true", content)
            self.assertIn("MEDIA_API_BASE_URL=https://media.example/v1", content)
            self.assertIn("MEDIA_IMAGE_MODEL=vision-model", content)
            self.assertIn("MEDIA_AUDIO_MODEL=audio-model", content)
            self.assertIn("MEDIA_API_TIMEOUT_SECONDS=45", content)
            self.assertIn("MEDIA_API_REQUESTS_PER_MINUTE=8", content)
            self.assertIn("MEDIA_API_DAILY_BUDGET=1.25", content)
            self.assertIn("MEDIA_MAX_CONCURRENT_CALLS=2", content)
            self.assertIn("OPENAI_API_KEY=keep-secret", content)
            self.assertIn("MEDIA_API_KEY=legacy-secret", content)
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

            with self.assertRaisesRegex(ValueError, "不支持"):
                service.update_media_settings({"api_key": "must-not-write"})
            self.assertNotIn("must-not-write", env_path.read_text(encoding="utf-8"))

    def test_media_secret_uses_fixed_keychain_item_and_never_persists_value(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return _RunResult(stdout="stored")

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            env_path = project / ".env"
            env_path.write_text("MEDIA_API_KEY=old-value\n", encoding="utf-8")
            service = self._service(project, runner=runner)

            result = service.replace_media_secret("super-secret-value")

            self.assertEqual(result, "媒体 API 密钥已更新。")
            self.assertEqual(
                calls[0][0],
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    "qq-openclaw-media",
                    "-a",
                    "media-api",
                    "-w",
                    "super-secret-value",
                ],
            )
            content = env_path.read_text(encoding="utf-8")
            self.assertNotIn("super-secret-value", content)
            self.assertNotIn("old-value", content)
            self.assertIn(
                "MEDIA_API_SECRET_REF=keychain:qq-openclaw-media:media-api",
                content,
            )
            self.assertIn("MEDIA_API_KEY=", content)

    def test_controller_cancels_only_the_default_users_active_task(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_dashboard.controller import DashboardController

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            workspace = root / "workspace"
            (project / "logs").mkdir(parents=True)
            workspace.mkdir()
            (project / ".env").write_text(
                "ALLOWED_QQ_USERS=10001\n", encoding="utf-8"
            )
            store = WorkspaceStateStore(workspace / "state.sqlite3")
            owned = store.e4_repository.create_task(
                10001, "m-owned", "media", task_id="owned-task"
            )
            store.e4_repository.create_task(
                20002, "m-foreign", "media", task_id="foreign-task"
            )
            controller = DashboardController(
                project_dir=project,
                memory_path=workspace / "MEMORY.md",
                todo_path=workspace / "TODO.md",
                style_path=workspace / "STYLE.md",
                reminder_state_path=workspace / "REMINDER_STATE.md",
                state_store=store,
                onebot_probe=lambda: False,
            )

            self.assertIn("已请求取消", controller.cancel_task(owned["id"]))
            self.assertTrue(
                store.e4_repository.get_task(owned["id"])["cancel_requested"]
            )
            with self.assertRaisesRegex(ValueError, "不可取消"):
                controller.cancel_task("foreign-task")


class DashboardE4HttpTests(unittest.TestCase):
    class Controller:
        def __init__(self):
            self.calls = []

        def get_state(self):
            return {}

        def cancel_task(self, task_id):
            self.calls.append(("cancel", task_id))
            return "任务已请求取消。"

        def update_media_settings(self, payload):
            self.calls.append(("settings", payload))
            return "媒体设置已更新。"

        def replace_media_secret(self, secret):
            self.calls.append(("secret", secret))
            return "媒体 API 密钥已更新。"

    def _post(self, url, payload, token=None):
        headers = {
            "Content-Type": "application/json",
            "Origin": url.rsplit("/", 1)[0],
        }
        if token is not None:
            headers["X-Dashboard-Token"] = token
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with request.urlopen(req, timeout=2) as response:
            return response.status, response.read().decode("utf-8")

    def test_e4_mutations_require_token_and_secret_response_never_echoes_value(self):
        from qq_openclaw_dashboard.server import create_server

        controller = self.Controller()
        server = create_server(controller, "127.0.0.1", 0, token="test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            unauthorized = request.Request(
                base + "/api/tasks/task-1/cancel",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(error.HTTPError) as context:
                request.urlopen(unauthorized, timeout=2)
            self.assertEqual(context.exception.code, 403)
            self.assertEqual(controller.calls, [])

            status, _ = self._post(
                base + "/api/tasks/task-1/cancel", {}, token="test-token"
            )
            self.assertEqual(status, 200)
            self._post(
                base + "/api/media/settings",
                {"enabled": True, "daily_budget": "1.00"},
                token="test-token",
            )
            secret = "never-echo-this-secret"
            _, body = self._post(
                base + "/api/media/secret", {"key": secret}, token="test-token"
            )
            self.assertNotIn(secret, body)
            self.assertEqual(
                controller.calls,
                [
                    ("cancel", "task-1"),
                    ("settings", {"enabled": True, "daily_budget": "1.00"}),
                    ("secret", secret),
                ],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class DashboardE4StaticTests(unittest.TestCase):
    def test_ui_contains_e4_views_and_never_embeds_secret_value(self):
        static = ROOT / "src" / "qq_openclaw_dashboard" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")

        for view in ("tasks", "models", "approvals", "health", "media"):
            self.assertIn('data-view="%s"' % view, html)
            self.assertIn('id="view-%s"' % view, html)
        self.assertIn('type="password"', html)
        self.assertIn("/api/tasks/", script)
        self.assertIn("/api/media/settings", script)
        self.assertIn("/api/media/secret", script)
        self.assertIn("mediaSecretInput.value = ''", script)


if __name__ == "__main__":
    unittest.main()
