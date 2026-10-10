from datetime import datetime, timedelta
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from urllib import error, request
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class DashboardOrchestrationTests(unittest.TestCase):
    def _controller(self, root, store=None, runner=None):
        from qq_openclaw_dashboard.controller import DashboardController

        project = Path(root) / "project"
        workspace = Path(root) / "workspace"
        (project / "logs").mkdir(parents=True)
        (project / "scripts").mkdir()
        workspace.mkdir()
        (project / ".env").write_text(
            "ONEBOT_WS_URL=ws://127.0.0.1:3001\n"
            "ALLOWED_QQ_USERS=10001\n"
            "ORCHESTRATION_MODE=direct\n"
            "OPENAI_API_KEY=keep-secret\n",
            encoding="utf-8",
        )
        now = datetime(2026, 8, 9, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        controller = DashboardController(
            project_dir=project,
            memory_path=workspace / "MEMORY.md",
            todo_path=workspace / "TODO.md",
            style_path=workspace / "STYLE.md",
            reminder_state_path=workspace / "REMINDER_STATE.md",
            now_provider=lambda: now,
            onebot_probe=lambda: False,
            state_store=store,
            runner=runner,
        )
        return controller, project, workspace, now

    def test_state_exposes_bounded_sanitized_orchestration_runs(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            active = store.orchestration_repository.create_config_version(
                {"mode": "direct"}, activate=True
            )
            controller, _, _, now = self._controller(temp_dir, store=store)
            for index in range(101):
                run = store.orchestration_repository.create_run(
                    user_id=10001,
                    source_message_id="request_text_secret_%d" % index,
                    session_key="agent:main:qq-10001",
                    mode="direct",
                    risk_level="normal",
                    config_version=active["version"],
                    now=now + timedelta(microseconds=index),
                )
                if index == 100:
                    store.orchestration_repository.transition_run(
                        run["run_id"],
                        "received",
                        "fallback",
                        "fallback",
                        "verification_rejected",
                        now=now + timedelta(seconds=1),
                    )

            orchestration = controller.get_state()["orchestration"]
            serialized = json.dumps(orchestration, ensure_ascii=False)

            self.assertEqual(orchestration["mode"], "direct")
            self.assertEqual(orchestration["config_version"], active["version"])
            self.assertEqual(len(orchestration["recent_runs"]), 100)
            self.assertEqual(orchestration["fallback_count"], 1)
            self.assertEqual(
                set(orchestration["ops_status"]),
                {"launch_agents", "logging", "backup_encryption"},
            )
            self.assertNotIn("request_text", serialized)
            self.assertNotIn("source_message_id", serialized)
            self.assertNotIn("session_key", serialized)
            self.assertNotIn("user_id", serialized)

    def test_mode_mutation_only_accepts_f0_modes_and_versions_config(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            runner_calls = []
            controller, project, _, _ = self._controller(
                temp_dir,
                store=store,
                runner=lambda *args, **kwargs: runner_calls.append(
                    (args, kwargs)
                ),
            )
            before = (project / ".env").read_text(encoding="utf-8")

            with self.assertRaises(ValueError):
                controller.update_orchestration_mode("shadow")
            self.assertEqual((project / ".env").read_text(encoding="utf-8"), before)

            message = controller.update_orchestration_mode("legacy_direct")
            content = (project / ".env").read_text(encoding="utf-8")
            active = store.orchestration_repository.active_config()

            self.assertIn("重启后生效", message)
            self.assertIn("ORCHESTRATION_MODE=legacy_direct", content)
            self.assertIn("OPENAI_API_KEY=keep-secret", content)
            self.assertEqual(active["config"], {"mode": "legacy_direct"})
            self.assertEqual(stat.S_IMODE((project / ".env").stat().st_mode), 0o600)
            self.assertEqual(runner_calls, [])

    def test_missing_active_config_is_reported_as_unset(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            controller, _, _, _ = self._controller(temp_dir, store=store)
            self.assertIsNone(
                controller.get_state()["orchestration"]["config_version"]
            )

    def test_database_failure_restores_env_byte_for_byte(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        class FailingRepository:
            def create_config_version(self, config, activate):
                raise OSError("database unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            controller, project, _, _ = self._controller(temp_dir, store=store)
            env_path = project / ".env"
            original = env_path.read_bytes().replace(
                b"ORCHESTRATION_MODE=direct\n", b""
            )
            env_path.write_bytes(original)
            controller._command_service._orchestration_repository = (
                FailingRepository()
            )

            with self.assertRaises(OSError):
                controller.update_orchestration_mode("direct")

            self.assertEqual(env_path.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)


class DashboardOrchestrationHttpTests(unittest.TestCase):
    def test_mode_mutation_requires_token_and_rejects_unimplemented_mode(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_dashboard.server import create_server

        with tempfile.TemporaryDirectory() as temp_dir:
            helper = DashboardOrchestrationTests()
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            controller, _, _, _ = helper._controller(temp_dir, store=store)
            server = create_server(controller, "127.0.0.1", 0, token="test-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%d" % server.server_port

            def post(mode, token=None):
                headers = {
                    "Content-Type": "application/json",
                    "Origin": base,
                }
                if token is not None:
                    headers["X-Dashboard-Token"] = token
                call = request.Request(
                    base + "/api/orchestration/mode",
                    data=json.dumps({"mode": mode}).encode("utf-8"),
                    method="POST",
                    headers=headers,
                )
                return request.urlopen(call, timeout=2)

            try:
                with self.assertRaises(error.HTTPError) as context:
                    post("direct")
                self.assertEqual(context.exception.code, 403)
                with self.assertRaises(error.HTTPError) as context:
                    post("shadow", token="test-token")
                self.assertEqual(context.exception.code, 400)
                with self.assertRaises(error.HTTPError) as context:
                    post(" direct ", token="test-token")
                self.assertEqual(context.exception.code, 400)
                with post("legacy_direct", token="test-token") as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertTrue(result["ok"])
                self.assertIn("重启后生效", result["message"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class DashboardOrchestrationStaticTests(unittest.TestCase):
    def test_ui_has_orchestration_view_without_model_role_controls(self):
        static = ROOT / "src" / "qq_openclaw_dashboard" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app = (static / "app.js").read_text(encoding="utf-8")
        orchestration = (static / "orchestration.js").read_text(encoding="utf-8")
        combined = html + app + orchestration

        self.assertIn("编排控制", html)
        self.assertIn('data-view="orchestration"', html)
        self.assertIn("orchestration.js", html)
        self.assertIn("/api/orchestration/mode", app)
        self.assertIn("recent_runs", orchestration)
        for forbidden in ("架构师模型", "审计模型", "执行模型"):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
