from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib import error, request
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _RunResult:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DashboardControllerTests(unittest.TestCase):
    def _controller(
        self, root, runner=None, state_store=None, launch_agent_probe=None
    ):
        from qq_openclaw_dashboard.controller import DashboardController

        project = Path(root) / "project"
        workspace = Path(root) / "workspace"
        project.mkdir()
        workspace.mkdir()
        (project / "logs").mkdir()
        (project / "scripts").mkdir()
        (project / ".env").write_text(
            "ONEBOT_WS_URL=ws://127.0.0.1:3001\n"
            "AGENT_ADAPTER=openclaw-cli\n"
            "OPENCLAW_MODEL=openai/gpt-test\n"
            "MEMORY_LIGHT_MODEL=openai/gpt-5.4-nano\n"
            "ENABLE_MEMORY_2=true\n"
            "ALLOWED_QQ_USERS=10001\n"
            "ENABLE_SECURITY_CONFIRMATION=true\n",
            encoding="utf-8",
        )
        (project / "scripts" / "bridge_service.sh").write_text(
            "#!/usr/bin/env bash\n", encoding="utf-8"
        )
        now = datetime(2026, 7, 14, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        backup_key_provider = type(
            "StaticBackupKeyProvider",
            (),
            {"get_or_create": lambda self: bytes(range(32))},
        )()
        controller = DashboardController(
            project_dir=project,
            memory_path=workspace / "MEMORY.md",
            todo_path=workspace / "TODO.md",
            style_path=workspace / "STYLE.md",
            reminder_state_path=workspace / "REMINDER_STATE.md",
            runner=runner,
            now_provider=lambda: now,
            onebot_probe=lambda: True,
            state_store=state_store,
            backup_key_provider=backup_key_provider,
            launch_agent_probe=(
                launch_agent_probe or (lambda label: (False, None))
            ),
        )
        return controller, project, workspace

    def test_state_reads_workspace_and_hides_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, project, workspace = self._controller(temp_dir)
            (project / ".env").write_text(
                (project / ".env").read_text(encoding="utf-8")
                + "OPENAI_API_KEY=secret-value\n",
                encoding="utf-8",
            )
            (workspace / "MEMORY.md").write_text(
                "# MEMORY\n\n## 长期记忆\n- 2026-07-14: 我不吃香菜\n",
                encoding="utf-8",
            )
            (workspace / "TODO.md").write_text(
                "# TODO\n\n## 待办\n"
                "- [ ] 1 | 2026-07-14 | 写周报 | user=10001 | "
                "due=2026-07-15T09:00:00+08:00 | reminded= | category=工作\n",
                encoding="utf-8",
            )
            (workspace / "STYLE.md").write_text(
                "# STYLE\n\n## 用户反馈沉淀\n- 2026-07-14: 少废话\n",
                encoding="utf-8",
            )
            (project / "logs" / "bridge.log").write_text(
                "line one\nconnected to OneBot WebSocket\n", encoding="utf-8"
            )

            state = controller.get_state()
            serialized = json.dumps(state, ensure_ascii=False)

            self.assertEqual(state["memories"][0]["text"], "我不吃香菜")
            self.assertEqual(state["todos"][0]["category"], "工作")
            self.assertEqual(state["styles"][0]["text"], "2026-07-14: 少废话")
            self.assertIn("connected to OneBot", state["logs"])
            self.assertEqual(state["config"]["model"], "openai/gpt-test")
            self.assertEqual(state["config"]["agent_id"], "main")
            self.assertTrue(state["config"]["sqlite_store"])
            self.assertTrue(state["config"]["gateway_mode"])
            self.assertTrue(state["connections"]["onebot"])
            self.assertEqual(state["health"]["status"], "stopped")
            self.assertNotIn("secret-value", serialized)
            self.assertNotIn("OPENAI_API_KEY", serialized)

    def test_launch_agent_state_parses_running_pid_with_fixed_target(self):
        from qq_openclaw_dashboard.queries import launch_agent_state

        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return _RunResult(
                stdout=(
                    "gui/501/com.chichu.qq-bridge = {\n"
                    "\tstate = running\n"
                    "\tpid = 12345\n"
                    "}\n"
                )
            )

        self.assertEqual(
            launch_agent_state(
                "com.chichu.qq-bridge", runner=runner, uid=501
            ),
            (True, 12345),
        )
        self.assertEqual(
            calls,
            [["/bin/launchctl", "print", "gui/501/com.chichu.qq-bridge"]],
        )
        with self.assertRaises(ValueError):
            launch_agent_state("local.untrusted", runner=runner, uid=501)

    def test_state_reports_managed_launch_agents_without_pid_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            statuses = {
                "com.chichu.qq-bridge": (True, 12345),
                "com.chichu.qq-watchdog": (True, 23456),
            }
            controller, _, _ = self._controller(
                temp_dir,
                launch_agent_probe=lambda label: statuses[label],
            )
            state = controller.get_state()

            self.assertEqual(
                state["service"],
                {
                    "running": True,
                    "pid": 12345,
                    "watchdog_running": True,
                    "watchdog_pid": 23456,
                    "reminders_paused": False,
                },
            )

    def test_state_exposes_reliable_transport_metrics(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            store.enqueue_outbox(
                kind="reply",
                user_id=10001,
                message="等待发送",
                dedupe_key="reply:m-1",
            )
            store.claim_inbound("m-1", 10001)
            store.set_runtime_metric(
                "last_agent_success_at", "2026-07-16T12:00:00+00:00"
            )
            controller, _, _ = self._controller(
                temp_dir, state_store=store
            )

            state = controller.get_state()

            self.assertEqual(state["transport"]["outbox_pending"], 1)
            self.assertEqual(state["transport"]["active_inbound"], 1)
            self.assertEqual(
                state["transport"]["last_agent_success_at"],
                "2026-07-16T12:00:00+00:00",
            )

    def test_e3_state_and_candidate_approval_are_visible(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            candidate = store.memory_repository.add_candidate(
                10001,
                "profile",
                "preference",
                "用户不吃香菜",
                0.95,
                "m-e3",
            )
            controller, _, _ = self._controller(temp_dir, state_store=store)

            before = controller.get_state()["memory2"]
            message = controller.confirm_memory_candidate(candidate["id"])
            after = controller.get_state()["memory2"]

            self.assertTrue(before["enabled"])
            self.assertEqual(before["light_model"], "openai/gpt-5.4-nano")
            self.assertEqual(before["stats"]["pending"], 1)
            self.assertEqual(before["pending"][0]["content"], "用户不吃香菜")
            self.assertIn("已确认", message)
            self.assertEqual(after["stats"]["confirmed"], 1)
            self.assertEqual(after["stats"]["pending"], 0)

            rejected = store.memory_repository.add_candidate(
                10001,
                "long_term",
                "project",
                "项目改用 Rust",
                0.8,
                "m-e4",
            )
            self.assertIn("已拒绝", controller.reject_memory_candidate(rejected["id"]))

    def test_manages_memories_todos_and_styles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _, workspace = self._controller(temp_dir)

            controller.add_memory("我不吃香菜")
            controller.add_todo("明天上午10点提醒我开会", user_id=10001)
            (workspace / "STYLE.md").write_text(
                "# STYLE\n\n## 用户反馈沉淀\n- 2026-07-14: 少废话\n",
                encoding="utf-8",
            )

            self.assertIn("我不吃香菜", controller.get_state()["memories"][0]["text"])
            self.assertIn("开会", controller.get_state()["todos"][0]["text"])
            self.assertIn("已删除", controller.delete_memory(1))
            self.assertIn("处理掉", controller.complete_todo(1))
            self.assertIn("已删除", controller.delete_style(1))

    def test_service_action_uses_whitelisted_script_command(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return _RunResult(stdout="Bridge started")

        with tempfile.TemporaryDirectory() as temp_dir:
            controller, project, _ = self._controller(temp_dir, runner=runner)

            result = controller.service_action("restart")

            self.assertIn("Bridge started", result)
            self.assertEqual(len(calls), 3)
            self.assertEqual(
                calls[1],
                [
                    "/bin/bash",
                    str((project / "scripts" / "bridge_service.sh").resolve()),
                    "restart",
                ],
            )
            self.assertEqual(calls[0][-1], "stop")
            self.assertTrue(calls[0][-2].endswith("watchdog_service.sh"))
            self.assertEqual(calls[2][-1], "start")
            self.assertTrue(calls[2][-2].endswith("watchdog_service.sh"))
            with self.assertRaises(ValueError):
                controller.service_action("explode")

    def test_state_exposes_sanitized_watchdog_health(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, project, _ = self._controller(temp_dir)
            (project / "logs" / "health.json").write_text(
                json.dumps({
                    "status": "degraded",
                    "message": "OneBot 不可用",
                    "checks": {"bridge": True, "onebot": False, "proxy": True, "openclaw": True},
                    "restart_count": 2,
                    "updated_at": "2026-07-14T16:00:00+08:00",
                    "last_alert_key": "do-not-expose",
                    "restart_history": ["private-detail"],
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            state = controller.get_state()

            self.assertEqual(state["health"]["status"], "stopped")
            self.assertEqual(state["health"]["restart_count"], 2)
            self.assertNotIn("last_alert_key", state["health"])
            self.assertNotIn("restart_history", state["health"])

    def test_backup_and_restore_only_managed_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, project, workspace = self._controller(temp_dir)
            memory_path = workspace / "MEMORY.md"
            memory_path.write_text("before\n", encoding="utf-8")
            (project / ".env").write_text("ONEBOT_WS_URL=ws://127.0.0.1:3001\n", encoding="utf-8")

            backup = controller.create_backup()
            memory_path.write_text("after\n", encoding="utf-8")
            result = controller.restore_backup(backup["name"])

            self.assertIn("恢复完成", result)
            self.assertEqual(memory_path.read_text(encoding="utf-8"), "before\n")
            with self.assertRaises(ValueError):
                controller.restore_backup("../escape.zip")


class DashboardHttpTests(unittest.TestCase):
    def test_http_api_requires_token_for_mutation(self):
        from qq_openclaw_dashboard.controller import DashboardController
        from qq_openclaw_dashboard.server import create_server

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            workspace = Path(temp_dir) / "workspace"
            project.mkdir()
            workspace.mkdir()
            (project / "logs").mkdir()
            controller = DashboardController(
                project_dir=project,
                memory_path=workspace / "MEMORY.md",
                todo_path=workspace / "TODO.md",
                style_path=workspace / "STYLE.md",
                reminder_state_path=workspace / "REMINDER_STATE.md",
                onebot_probe=lambda: False,
            )
            server = create_server(controller, "127.0.0.1", 0, token="test-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%d" % server.server_port

            try:
                with request.urlopen(base + "/api/state", timeout=2) as response:
                    state = json.loads(response.read().decode("utf-8"))
                self.assertTrue(state["ok"])

                body = json.dumps({"text": "我不吃香菜"}).encode("utf-8")
                unauthorized = request.Request(
                    base + "/api/memories", data=body, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(error.HTTPError) as context:
                    request.urlopen(unauthorized, timeout=2)
                self.assertEqual(context.exception.code, 403)

                authorized = request.Request(
                    base + "/api/memories", data=body, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Dashboard-Token": "test-token",
                    },
                )
                with request.urlopen(authorized, timeout=2) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertTrue(result["ok"])
                self.assertIn("记下了", result["message"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
