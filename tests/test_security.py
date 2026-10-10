from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ConfirmationGuardTests(unittest.TestCase):
    _BINDING_KEY = b"k" * 32

    def _guard(self, now):
        from qq_openclaw_bridge.security import ConfirmationGuard

        return ConfirmationGuard(
            timeout_seconds=120,
            now_provider=lambda: now[0],
            id_provider=lambda: "A1B2C3",
        )

    def test_destructive_local_command_requires_confirmation(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)

        decision = guard.evaluate(10001, "删除记忆 1")

        self.assertTrue(decision.intercepted)
        self.assertIn("待确认", decision.reply)
        self.assertIn("长期记忆", decision.reply)
        self.assertIsNone(decision.command)

    def test_all_destructive_local_commands_require_confirmation(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)

        commands = (
            "删除记忆 1",
            "清空记忆",
            "删除待办 2",
            "清空已完成",
            "删除风格 3",
            "清空风格反馈",
        )

        for command in commands:
            with self.subTest(command=command):
                decision = guard.evaluate(10001, command)
                self.assertTrue(decision.intercepted)
                self.assertIn("确认执行", decision.reply)

    def test_confirmation_releases_original_command_once(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)
        guard.evaluate(10001, "清空记忆")

        confirmed = guard.evaluate(10001, "确认执行 A1B2C3")
        repeated = guard.evaluate(10001, "确认执行 A1B2C3")

        self.assertTrue(confirmed.intercepted)
        self.assertEqual(confirmed.command, "清空记忆")
        self.assertIsNone(confirmed.reply)
        self.assertIn("没有待确认", repeated.reply)

    def test_cancel_discards_pending_command(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)
        guard.evaluate(10001, "删除待办 3")

        cancelled = guard.evaluate(10001, "取消操作")
        confirmed = guard.evaluate(10001, "确认执行 A1B2C3")

        self.assertIn("已取消", cancelled.reply)
        self.assertIn("没有待确认", confirmed.reply)

    def test_pending_confirmation_expires(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)
        guard.evaluate(10001, "清空风格反馈")
        now[0] += timedelta(seconds=121)

        decision = guard.evaluate(10001, "确认执行 A1B2C3")

        self.assertIn("已过期", decision.reply)
        self.assertIsNone(decision.command)

    def test_pending_commands_are_isolated_by_qq_user(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)
        guard.evaluate(10001, "删除记忆 1")

        other_user = guard.evaluate(10002, "确认执行 A1B2C3")
        owner = guard.evaluate(10001, "确认执行 A1B2C3")

        self.assertIn("没有待确认", other_user.reply)
        self.assertEqual(owner.command, "删除记忆 1")

    def test_sensitive_agent_actions_require_confirmation(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)

        risky_commands = (
            "帮我给张三发消息说今晚开会",
            "帮我删除桌面的文件",
            "运行这个清理脚本",
            "修改系统代理配置",
            "帮我付款购买会员",
        )

        for command in risky_commands:
            with self.subTest(command=command):
                decision = guard.evaluate(10001, command)
                self.assertTrue(decision.intercepted)
                self.assertIn("确认执行", decision.reply)

    def test_confirmation_requires_matching_approval_id(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)
        guard.evaluate(10001, "清空记忆")

        missing = guard.evaluate(10001, "确认执行")
        wrong = guard.evaluate(10001, "确认执行 FFFFFF")
        confirmed = guard.evaluate(10001, "确认执行 A1B2C3")

        self.assertIn("带上审批码", missing.reply)
        self.assertIn("不匹配", wrong.reply)
        self.assertEqual(confirmed.command, "清空记忆")

    def test_pending_approval_survives_guard_recreation(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        import tempfile

        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            first = ConfirmationGuard(
                timeout_seconds=120,
                now_provider=lambda: now[0],
                approval_store=store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            )
            first.evaluate(10001, "删除待办 3")
            recreated = ConfirmationGuard(
                timeout_seconds=120,
                now_provider=lambda: now[0],
                approval_store=store,
                binding_key=self._BINDING_KEY,
            )

            confirmed = recreated.evaluate(10001, "确认执行 A1B2C3")

            self.assertEqual(confirmed.command, "删除待办 3")

    def test_normal_questions_and_safe_commands_pass_through(self):
        now = [datetime(2026, 7, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
        guard = self._guard(now)

        for command in ("如何删除文件", "取消提醒 1", "查看记忆", "你好"):
            with self.subTest(command=command):
                decision = guard.evaluate(10001, command)
                self.assertFalse(decision.intercepted)
                self.assertIsNone(decision.reply)
                self.assertIsNone(decision.command)

    def test_destructive_local_commands_create_fixed_hash_bound_intents(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        cases = (
            ("删除记忆 1", "memory.delete", "local.housekeeper", {"index": 1}),
            ("清空记忆", "memory.clear", "local.housekeeper", {}),
            ("删除待办 2", "todo.delete", "local.housekeeper", {"index": 2}),
            ("清空已完成", "todo.completed.clear", "local.housekeeper", {}),
            ("删除风格 3", "style.delete", "local.style", {"index": 3}),
            ("清空风格反馈", "style.clear", "local.style", {}),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            approval_ids = iter(
                ("A1B2C1", "A1B2C2", "A1B2C3", "A1B2C4", "A1B2C5", "A1B2C6")
            )
            guard = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: next(approval_ids),
                binding_key=self._BINDING_KEY,
            )
            for number, (command, action_type, adapter_id, payload) in enumerate(cases):
                with self.subTest(command=command):
                    decision = guard.evaluate(
                        10001,
                        command,
                        message_id="m-%d" % number,
                        session_key="agent:main:qq-10001",
                    )
                    intent = store.action_intent_repository.get_by_approval(
                        decision.approval_id
                    )
                    self.assertEqual(intent["action_type"], action_type)
                    self.assertEqual(intent["adapter_id"], adapter_id)
                    self.assertEqual(json.loads(intent["payload_json"]), payload)

    def test_confirmation_consumes_once_and_rejects_payload_tampering(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_bridge.domain.actions import payload_sha256

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            approval_ids = iter(("A1B2C3", "D4E5F6"))
            guard = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: next(approval_ids),
                binding_key=self._BINDING_KEY,
            )
            pending = guard.evaluate(
                10001, "删除待办 2", message_id="m-1", session_key="s-1"
            )
            with store._connect() as connection:
                connection.execute(
                    "UPDATE action_intents SET action_type=?, adapter_id=?, "
                    "payload_json=?, payload_sha256=? WHERE intent_id=?",
                    (
                        "style.delete",
                        "local.style",
                        '{"index":3}',
                        payload_sha256({"index": 3}),
                        pending.intent_id,
                    ),
                )

            rejected = guard.evaluate(10001, "确认执行 A1B2C3")

            self.assertIsNone(rejected.command)
            self.assertIsNone(rejected.execution_token)
            self.assertIn("重新发起", rejected.reply)

            fresh = guard.evaluate(
                10001, "删除待办 2", message_id="m-2", session_key="s-1"
            )
            confirmed = guard.evaluate(10001, "确认执行 D4E5F6")
            row = store.action_intent_repository.get(fresh.intent_id)

            self.assertEqual(confirmed.command, "删除待办 2")
            self.assertEqual(confirmed.execution_token.intent_id, fresh.intent_id)
            self.assertEqual(row["status"], "executing")
            repeated = guard.evaluate(10001, "确认执行 D4E5F6")
            self.assertIn("没有待确认", repeated.reply)

    def test_confirmation_rejects_coordinated_approval_and_intent_tampering(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_bridge.domain.actions import payload_sha256

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            guard = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            )
            pending = guard.evaluate(
                10001, "删除待办 2", message_id="m-1", session_key="s-1"
            )
            with store._connect() as connection:
                approval = connection.execute(
                    "SELECT command FROM approvals WHERE approval_id=?",
                    (pending.approval_id,),
                ).fetchone()[0]
                prefix, encoded = approval.split(":", 1)
                binding = json.loads(encoded)
                binding.update(
                    {
                        "action_type": "style.delete",
                        "adapter_id": "local.style",
                        "payload_json": '{"index":3}',
                        "payload_sha256": payload_sha256({"index": 3}),
                    }
                )
                tampered_approval = prefix + ":" + json.dumps(
                    binding,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.execute(
                    "UPDATE approvals SET command=? WHERE approval_id=?",
                    (tampered_approval, pending.approval_id),
                )
                connection.execute(
                    "UPDATE action_intents SET action_type=?, adapter_id=?, "
                    "payload_json=?, payload_sha256=? WHERE intent_id=?",
                    (
                        "style.delete",
                        "local.style",
                        '{"index":3}',
                        payload_sha256({"index": 3}),
                        pending.intent_id,
                    ),
                )

            rejected = guard.evaluate(10001, "确认执行 A1B2C3")

            self.assertIsNone(rejected.command)
            self.assertIsNone(rejected.execution_token)
            self.assertIn("校验失败", rejected.reply)

    def test_missing_referenced_intent_never_becomes_a_model_command(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            guard = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            )
            pending = guard.evaluate(
                10001, "删除记忆 1", message_id="m-1", session_key="s-1"
            )
            with store._connect() as connection:
                connection.execute(
                    "DELETE FROM action_intents WHERE intent_id=?",
                    (pending.intent_id,),
                )

            decision = guard.evaluate(10001, "确认执行 A1B2C3")

            self.assertIsNone(decision.command)
            self.assertIsNone(decision.execution_token)
            self.assertIn("没有执行", decision.reply)

    def test_executing_action_blocks_duplicate_reissue_until_manual_verification(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            guard = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            )
            first = guard.evaluate(
                10001, "删除待办 2", message_id="m-1", session_key="s-1"
            )
            confirmed = guard.evaluate(10001, "确认执行 A1B2C3")

            duplicate = guard.evaluate(
                10001, "删除待办 2", message_id="m-2", session_key="s-1"
            )
            with store._connect() as connection:
                intent_count = connection.execute(
                    "SELECT COUNT(*) FROM action_intents"
                ).fetchone()[0]

            self.assertIsNotNone(confirmed.execution_token)
            self.assertTrue(duplicate.intercepted)
            self.assertIn("待核验", duplicate.reply)
            self.assertIn("暂勿重复", duplicate.reply)
            self.assertEqual(intent_count, 1)

    def test_persistent_plain_command_tampering_never_releases_to_model(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            guard = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            )
            guard.evaluate(10001, "清空记忆", message_id="m-1", session_key="s-1")
            with store._connect() as connection:
                connection.execute(
                    "UPDATE approvals SET command='清空记忆' WHERE approval_id='A1B2C3'"
                )

            decision = guard.evaluate(10001, "确认执行 A1B2C3")

            self.assertIsNone(decision.command)
            self.assertIsNone(decision.execution_token)
            self.assertIn("校验失败", decision.reply)

    def test_persistent_risky_command_without_adapter_is_not_saved_for_confirmation(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            guard = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            )

            decision = guard.evaluate(10001, "帮我给张三发消息说今晚开会")

            self.assertTrue(decision.intercepted)
            self.assertIn("无授权适配器", decision.reply)
            self.assertIsNone(store.get_pending_approval(10001))

    def test_fresh_store_requires_the_same_persistent_binding_key(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.sqlite3"
            first_store = WorkspaceStateStore(database_path)
            first = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=first_store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            )
            first.evaluate(10001, "删除待办 2", message_id="m-1", session_key="s-1")

            restarted_store = WorkspaceStateStore(database_path)
            accepted = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=restarted_store,
                binding_key=self._BINDING_KEY,
            ).evaluate(10001, "确认执行 A1B2C3")

            self.assertEqual(accepted.command, "删除待办 2")

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.sqlite3"
            store = WorkspaceStateStore(database_path)
            ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            ).evaluate(10001, "删除待办 2", message_id="m-1", session_key="s-1")

            rejected = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=WorkspaceStateStore(database_path),
                binding_key=b"z" * 32,
            ).evaluate(10001, "确认执行 A1B2C3")

            self.assertIsNone(rejected.command)
            self.assertIn("校验失败", rejected.reply)

    def test_unknown_local_action_blocks_an_identical_reissue(self):
        from qq_openclaw_bridge.security import ConfirmationGuard
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        now = datetime(2026, 8, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            guard = ConfirmationGuard(
                now_provider=lambda: now,
                approval_store=store,
                id_provider=lambda: "A1B2C3",
                binding_key=self._BINDING_KEY,
            )
            pending = guard.evaluate(
                10001, "删除待办 2", message_id="m-1", session_key="s-1"
            )
            token = guard.evaluate(10001, "确认执行 A1B2C3").execution_token
            store.action_intent_repository.mark_unknown(
                token.intent_id,
                token.user_id,
                token.payload_sha256,
                token.config_version,
                "HandlerError",
                "side effect result requires verification",
                now=now,
            )

            duplicate = guard.evaluate(
                10001, "删除待办 2", message_id="m-2", session_key="s-1"
            )

            self.assertEqual(pending.intent_id, token.intent_id)
            self.assertIn("待核验", duplicate.reply)
            self.assertIn("暂勿重复", duplicate.reply)


if __name__ == "__main__":
    unittest.main()
