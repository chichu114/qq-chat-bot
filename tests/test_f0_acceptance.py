import logging
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from tests import test_bridge as bridge_fakes


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AcceptanceScenario:
    name: str
    text: str
    kind: str = "text"


@dataclass(frozen=True)
class AcceptanceOutcome:
    reply: str
    agent_calls: int
    outbox_rows: tuple
    inbound_status: object
    accepted: tuple
    agent_contexts: tuple
    orchestration_statuses: tuple


class _AcceptanceAdapter:
    def __init__(self):
        self.requests = []

    def reply(self, request):
        from qq_openclaw_bridge.models import AgentResponse

        self.requests.append(request)
        if request.text == "触发超时":
            raise subprocess.TimeoutExpired("agent", 1)
        if request.text == "触发代理错误":
            raise RuntimeError("unsupported_region")
        if request.text == "长回复":
            return AgentResponse("长" * 160)
        if request.text == "继续 E3 项目":
            return AgentResponse("模型看见：%s" % request.context)
        return AgentResponse("模型回复：%s" % request.text)


class _AcceptanceStyle:
    def handle_command(self, text):
        if text == "语气设置":
            return "当前风格：毒舌御姐，但先把事办完。"
        return None

    def record_if_feedback(self, text):
        return None


class _AcceptanceHousekeeper:
    def handle(self, text, user_id=None):
        replies = {
            "列出待办": "待办：完成 F0 验收。",
            "提醒事项": "提醒：今晚检查服务。",
        }
        return replies.get(text)


class _MemoryCommand:
    def handle(self, request):
        if request.text == "记忆列表":
            return "长期记忆：偏好简洁回复。"
        return None


class _MemoryContext:
    def enrich(self, request):
        if request.text == "继续 E3 项目":
            return replace(request, context="E3 已确认上下文")
        return request


class _AcceptanceMemory:
    def __init__(self):
        self.command_handler = _MemoryCommand()
        self.context_service = _MemoryContext()


class _AcceptanceConfirmationGuard:
    def __init__(
        self,
        timeout_seconds,
        approval_store,
        binding_key,
        config_version,
    ):
        self.timeout_seconds = timeout_seconds
        self.approval_store = approval_store
        self.binding_key = binding_key
        self.config_version = config_version

    def evaluate(self, user_id, text, message_id=None, session_key=None):
        if text == "需要审批":
            return SimpleNamespace(
                reply="该操作需要审批，确认后才会执行。",
                command=None,
                execution_token=None,
            )
        return SimpleNamespace(reply=None, command=None, execution_token=None)


class _FakeApprovalBindingKeyProvider:
    def get(self):
        return b"f" * 32


class F0AcceptanceHarness:
    @staticmethod
    def fixed_corpus():
        return (
            AcceptanceScenario("identity", "你是谁"),
            AcceptanceScenario("memory", "记忆列表"),
            AcceptanceScenario("todo", "列出待办"),
            AcceptanceScenario("reminder", "提醒事项"),
            AcceptanceScenario("style", "语气设置"),
            AcceptanceScenario("approval", "需要审批"),
            AcceptanceScenario("plain", "你好"),
            AcceptanceScenario("long_reply", "长回复"),
            AcceptanceScenario("timeout", "触发超时"),
            AcceptanceScenario("proxy_error", "触发代理错误"),
            AcceptanceScenario("duplicate", "重复消息", "duplicate"),
            AcceptanceScenario("outbox_retry", "重试发送", "outbox_retry"),
            AcceptanceScenario("e3_context", "继续 E3 项目"),
            AcceptanceScenario("e4_disabled_media", "看看图片", "media"),
        )

    @classmethod
    def scenario(cls, name):
        return next(item for item in cls.fixed_corpus() if item.name == name)

    async def run_mode(self, mode, scenario):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        with tempfile.TemporaryDirectory(prefix="chichu-f0-acceptance-") as temp:
            root = Path(temp)
            database = root / "state.sqlite3"
            config = Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={10001},
                orchestration_mode=mode,
                state_database_path=str(database),
                housekeeper_memory_path=str(root / "MEMORY.md"),
                housekeeper_todo_path=str(root / "TODO.md"),
                style_memory_path=str(root / "STYLE.md"),
                reminder_state_path=str(root / "REMINDER_STATE.md"),
                enable_security_confirmation=True,
                enable_auto_backup=False,
                enable_media_pipeline=False,
                enable_long_tasks=False,
                agent_retry_attempts=1,
                max_reply_chars=80,
                rate_limit_messages_per_minute=1000,
            )
            adapter = _AcceptanceAdapter()
            bridge = build_application(
                config,
                agent_factory=lambda _config: adapter,
                style_factory=lambda _config, state_store=None: _AcceptanceStyle(),
                housekeeper_factory=(
                    lambda _config, state_store=None: _AcceptanceHousekeeper()
                ),
                reminder_factory=lambda _config, state_store=None: None,
                memory_factory=(
                    lambda _config, state_store, housekeeper: _AcceptanceMemory()
                ),
                confirmation_guard_factory=_AcceptanceConfirmationGuard,
                approval_binding_key_provider_factory=_FakeApprovalBindingKeyProvider,
                backup_factory=lambda _config: None,
            )

            current_time = [
                datetime(2099, 8, 9, 12, 0, tzinfo=ZoneInfo("UTC"))
            ]
            bridge._delivery_service._now_provider = lambda: current_time[0]
            message_id = "%s-1" % scenario.name
            event = self._event(scenario, message_id)
            accepted = []
            websocket = (
                bridge_fakes._FailingWebSocket(failures=1)
                if scenario.kind == "outbox_retry"
                else bridge_fakes._FakeWebSocket()
            )
            accepted.append(await bridge.handle_event(event, websocket))

            sent = list(websocket.sent)
            if scenario.kind == "duplicate":
                accepted.append(await bridge.handle_event(event, websocket))
                sent = list(websocket.sent)
            elif scenario.kind == "outbox_retry":
                current_time[0] += timedelta(seconds=10)
                retry_socket = bridge_fakes._FakeWebSocket()
                await bridge.deliver_outbox_once(retry_socket)
                sent.extend(retry_socket.sent)

            with bridge.state_store._connect() as connection:
                outbox_rows = tuple(
                    connection.execute(
                        "SELECT kind, message, echo, dedupe_key, status, attempts "
                        "FROM outbox ORDER BY id"
                    ).fetchall()
                )
                inbound = connection.execute(
                    "SELECT status FROM inbound_messages WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                orchestration_statuses = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM orchestration_runs ORDER BY started_at, run_id"
                    ).fetchall()
                )

            replies = [
                item["params"]["message"]
                for item in sent
                if item.get("action") == "send_private_msg"
            ]
            return AcceptanceOutcome(
                reply=replies[-1] if replies else "",
                agent_calls=len(adapter.requests),
                outbox_rows=outbox_rows,
                inbound_status=None if inbound is None else inbound[0],
                accepted=tuple(accepted),
                agent_contexts=tuple(request.context for request in adapter.requests),
                orchestration_statuses=orchestration_statuses,
            )

    @staticmethod
    def _event(scenario, message_id):
        if scenario.kind == "media":
            return {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": message_id,
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "file": "acceptance-image",
                            "url": "https://invalid.example/image.png",
                        },
                    },
                ],
                "time": 10,
            }
        return {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": message_id,
            "message": scenario.text,
            "time": 10,
        }


class F0AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._logging_disable_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.harness = F0AcceptanceHarness()

    def tearDown(self):
        logging.disable(self._logging_disable_level)

    async def test_legacy_and_runtime_direct_are_behaviorally_equivalent(self):
        for scenario in self.harness.fixed_corpus():
            with self.subTest(scenario=scenario.name):
                legacy = await self.harness.run_mode("legacy_direct", scenario)
                direct = await self.harness.run_mode("direct", scenario)
                self.assertEqual(direct.reply, legacy.reply)
                self.assertEqual(direct.agent_calls, legacy.agent_calls)
                self.assertEqual(direct.outbox_rows, legacy.outbox_rows)
                self.assertEqual(direct.inbound_status, legacy.inbound_status)
                self.assertEqual(direct.accepted, legacy.accepted)
                self.assertEqual(direct.agent_contexts, legacy.agent_contexts)

    async def test_direct_calls_agent_once_for_text_and_zero_for_local_commands(self):
        plain = await self.harness.run_mode(
            "direct", self.harness.scenario("plain")
        )
        self.assertEqual(plain.agent_calls, 1)

        for name in ("identity", "memory", "todo", "reminder", "style", "approval"):
            with self.subTest(scenario=name):
                local = await self.harness.run_mode(
                    "direct", self.harness.scenario(name)
                )
                self.assertEqual(local.agent_calls, 0)

    async def test_duplicate_and_retry_keep_durable_state_consistent(self):
        duplicate = await self.harness.run_mode(
            "direct", self.harness.scenario("duplicate")
        )
        self.assertEqual(duplicate.accepted, (True, False))
        self.assertEqual(duplicate.inbound_status, "completed")
        self.assertEqual(len(duplicate.outbox_rows), 1)

        retry = await self.harness.run_mode(
            "direct", self.harness.scenario("outbox_retry")
        )
        self.assertEqual(retry.inbound_status, "completed")
        self.assertEqual(retry.outbox_rows[0][-2:], ("sent", 2))

        e3 = await self.harness.run_mode(
            "direct", self.harness.scenario("e3_context")
        )
        self.assertEqual(e3.agent_contexts, ("E3 已确认上下文",))

        plain = await self.harness.run_mode(
            "direct", self.harness.scenario("plain")
        )
        self.assertEqual(plain.orchestration_statuses, ("delivered",))
        self.assertEqual(retry.orchestration_statuses, ("delivered",))

    async def test_direct_readiness_probe_uses_runtime_and_calls_adapter_once(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.models import AgentResponse
        from qq_openclaw_bridge.runtime.readiness import run_direct_probe

        class Adapter:
            def __init__(self):
                self.calls = []

            def reply(self, request):
                self.calls.append(request)
                return AgentResponse("你好")

        adapter = Adapter()
        result = await run_direct_probe(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={10001},
                orchestration_mode="direct",
                agent_adapter="openclaw-cli",
            ),
            adapter_factory=lambda _config: adapter,
            state_store_factory=lambda _config: None,
        )

        self.assertEqual(result, "你好")
        self.assertEqual(len(adapter.calls), 1)

    async def test_direct_readiness_probe_uses_active_config_version(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.models import AgentResponse
        from qq_openclaw_bridge.runtime.readiness import run_direct_probe

        captured = []

        class Repository:
            def active_config(self):
                return {"version": 9, "config": {"mode": "direct"}}

        class Store:
            orchestration_repository = Repository()

        class Adapter:
            def reply(self, request):
                return AgentResponse("你好")

        class Runtime:
            def __init__(self, agent_call):
                self._agent_call = agent_call

            async def execute(self, contract):
                return await self._agent_call(contract.request)

        def runtime_factory(agent_call, trace_sink=None, config_version=1):
            captured.append(config_version)
            return Runtime(agent_call)

        result = await run_direct_probe(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={10001},
                orchestration_mode="direct",
                agent_adapter="openclaw-cli",
            ),
            adapter_factory=lambda _config: Adapter(),
            state_store_factory=lambda _config: Store(),
            runtime_factory=runtime_factory,
        )

        self.assertEqual(result, "你好")
        self.assertEqual(captured, [9])

    def test_chinese_operations_guide_covers_f0_deploy_and_rollback(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for marker in (
            "legacy_direct",
            "ActionIntent",
            "qq-openclaw-backup",
            "scripts/install_launch_agents.py",
            "scripts/bridge_service.sh restart",
            "python -m unittest discover -s tests -p 'test_*.py'",
            "F1",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, readme)


if __name__ == "__main__":
    unittest.main()
