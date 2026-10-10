import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class _FailingWebSocket(_FakeWebSocket):
    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    async def send(self, payload):
        if self.failures:
            self.failures -= 1
            raise ConnectionError("socket closed")
        await super().send(payload)


class _FakeAdapter:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.requests = []

    def reply(self, request):
        from qq_openclaw_bridge.models import AgentResponse

        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return AgentResponse(text=self.text)


class _FakeStyleMemory:
    def __init__(self, reply=None, command_reply=None):
        self.reply = reply
        self.command_reply = command_reply
        self.texts = []
        self.command_texts = []

    def handle_command(self, text):
        self.command_texts.append(text)
        return self.command_reply

    def record_if_feedback(self, text):
        self.texts.append(text)
        return self.reply


class _FakeHousekeeper:
    def __init__(self, reply=None):
        self.reply = reply
        self.texts = []

    def handle(self, text, user_id=None):
        self.texts.append(text)
        return self.reply


class _FakeReminder:
    def __init__(self, todo_id=1, user_id=10001, message="该检查 API 额度了"):
        self.todo_id = todo_id
        self.user_id = user_id
        self.message = message


class _FakeReminderStore:
    def __init__(self, reminders=None):
        self.reminders = reminders or []
        self.collected_at = []
        self.sent = []

    def collect_due(self, now):
        self.collected_at.append(now)
        return list(self.reminders)

    def mark_sent(self, reminder, now):
        self.sent.append((reminder.todo_id, now))


class _FakeBackupManager:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def run_if_due(self):
        self.calls += 1
        return self.result


class _FakeMemoryCommand:
    def __init__(self, reply=None):
        self.reply = reply
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        return self.reply


class _FakeMemoryContext:
    def enrich(self, request):
        from dataclasses import replace

        return replace(request, context="E3 相关记忆")


class _FakeMemoryLearning:
    def __init__(self):
        self.observed = []

    async def observe(self, request, reply):
        self.observed.append((request, reply))

    async def wait(self):
        return None


class _FakeMemoryRuntime:
    def __init__(self, command_reply=None):
        self.command_handler = _FakeMemoryCommand(command_reply)
        self.context_service = _FakeMemoryContext()
        self.learning_service = _FakeMemoryLearning()


class _FakeQuoteResolver:
    def __init__(self, artifact):
        self.artifact = artifact
        self.calls = []

    async def resolve(self, envelope, websocket):
        self.calls.append((envelope, websocket))
        return self.artifact


class _FakePipeline:
    def __init__(self, artifacts=(), error=None):
        self.artifacts = tuple(artifacts)
        self.error = error
        self.calls = []

    async def process(self, envelope, progress, cancellation):
        self.calls.append((envelope, cancellation))
        if progress is not None:
            progress("downloading", "attachment=1 kind=image")
        if self.error is not None:
            raise self.error
        return self.artifacts


class _FakeTaskService:
    def __init__(self, notice=None, cancel=False, repository=None):
        self.notice = notice
        self.cancel_run = cancel
        self.repository = repository
        self.runs = []
        self.reports = []

    @property
    def active_task_ids(self):
        return ("task-44",) if self.runs else ()

    async def run(self, envelope, operation, notification_sink):
        self.runs.append(envelope)
        if self.cancel_run:
            raise asyncio.CancelledError()
        if self.repository is not None and self.repository.get_task("task-44") is None:
            self.repository.create_task(
                envelope.user_id,
                envelope.message_id,
                "long_task",
                task_id="task-44",
            )
            self.repository.transition_task(
                "task-44", "queued", "running", "running"
            )
        if self.notice is not None:
            notification_sink = notification_sink.bind_task("task-44")
            await notification_sink(self.notice)
        token = type("Token", (), {"raise_if_cancelled": lambda self: None})()
        return await operation("task-44", token)

    def report(self, task_id, stage, detail=""):
        self.reports.append((task_id, stage, detail))


class _FakeTaskCommands:
    def __init__(self, reply=None):
        self.reply = reply
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        return self.reply


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, **overrides):
        from qq_openclaw_bridge.config import Config

        values = {
            "onebot_ws_url": "ws://127.0.0.1:3001",
            "allowed_qq_users": {10001},
            "fallback_reply": "智能体暂时不可用，请稍后再试。",
            "max_reply_chars": 1800,
        }
        values.update(overrides)
        return Config(**values)

    async def test_f1_off_run_forever_does_not_wait_for_event_or_memory_shutdown(self):
        from qq_openclaw_bridge.bridge import Bridge

        class NeverWaiter:
            async def wait(self):
                await asyncio.Event().wait()

        class Supervisor:
            async def run_forever(self):
                await asyncio.Event().wait()

        bridge = Bridge(self._config(), _FakeAdapter(text="unused"))
        bridge._event_dispatcher = NeverWaiter()
        bridge.memory_runtime = type("Memory", (), {
            "learning_service": NeverWaiter(),
        })()
        bridge._websocket_supervisor = Supervisor()
        task = asyncio.create_task(bridge.run_forever())
        await asyncio.sleep(0)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)

    async def test_f1_run_forever_shuts_down_only_shadow_observer_with_bound(self):
        from qq_openclaw_bridge.bridge import Bridge

        class Coordinator:
            has_shadow_routing_observer = True

            def __init__(self):
                self.timeouts = []

            async def shutdown_shadow_routing(self, timeout_seconds):
                self.timeouts.append(timeout_seconds)
                return True

        class Supervisor:
            async def run_forever(self):
                await asyncio.Event().wait()

        coordinator = Coordinator()
        bridge = Bridge(
            self._config(enable_shadow_routing=True), _FakeAdapter(text="unused"),
            message_coordinator=coordinator,
        )
        bridge._websocket_supervisor = Supervisor()
        task = asyncio.create_task(bridge.run_forever())
        await asyncio.sleep(0)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)

        self.assertEqual(coordinator.timeouts, [5.0])

    def _media_runtime(
        self, pipeline=None, quote=None, task_service=None, task_commands=None
    ):
        from qq_openclaw_bridge.application.quotes import QuoteArtifact
        from qq_openclaw_bridge.runtime.media import MediaRuntime

        return MediaRuntime(
            pipeline=pipeline,
            quote_resolver=_FakeQuoteResolver(
                quote or QuoteArtifact("", "absent", None)
            ),
            task_service=task_service,
            task_commands=task_commands,
            rpc_client=type("Rpc", (), {
                "consume": lambda self, payload: False,
                "fail_pending": lambda self, error=None: None,
            })(),
        )

    @staticmethod
    def _group_event(group_id=77, user_id=88, message_id=123, *, reply_id=None):
        message = []
        if reply_id is None:
            message.append({"type": "at", "data": {"qq": "999"}})
        else:
            message.append({"type": "reply", "data": {"id": str(reply_id)}})
        message.append({"type": "text", "data": {"text": "群问题"}})
        return {
            "post_type": "message",
            "message_type": "group",
            "group_id": group_id,
            "user_id": user_id,
            "message_id": message_id,
            "message": message,
            "time": 10,
        }

    def _group_runtime(self, *, coordinator=None, delivery=None, repository=None, rpc=None,
                       allowed_groups=frozenset({77, 78}),
                       interjection_decider=None, interjection_limiter=None,
                       private_agent_adapter=None):
        from qq_openclaw_bridge.rate_limit import RateLimiter

        repository = repository or type("Repository", (), {
            "is_bot_message": lambda self, group_id, message_id: False,
        })()
        rpc = rpc or type("Rpc", (), {
            "consume": lambda self, payload: False,
            "fail_pending": lambda self: None,
            "request": lambda self, *args, **kwargs: None,
        })()
        return type("GroupRuntime", (), {
            "repository": repository,
            "rpc_client": rpc,
            "coordinator": coordinator,
            "delivery": delivery,
            "allowed_groups_provider": lambda self: allowed_groups,
            "group_limiter": RateLimiter(100),
            "user_limiter": RateLimiter(100),
            "interjection_decider": interjection_decider,
            "interjection_limiter": interjection_limiter or RateLimiter(100),
            "private_agent_adapter": private_agent_adapter,
            "generation_semaphore": asyncio.Semaphore(2),
        })()

    async def test_configured_private_user_uses_kanagawave_adapter(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.models import AgentRequest

        default_adapter = _FakeAdapter(text="默认模型")
        kanagawave_adapter = _FakeAdapter(text="KanagawaWave 模型")
        bridge = Bridge(
            self._config(
                allowed_qq_users={10001, 20002},
                enable_group_agent=True,
                bot_qq=999,
                kanagawave_private_users={20002},
            ),
            default_adapter,
            group_runtime=self._group_runtime(
                private_agent_adapter=kanagawave_adapter,
            ),
        )
        request = AgentRequest(20002, "7", "你好", 1.0)

        response = await bridge._call_agent(request)

        self.assertEqual(response.text, "KanagawaWave 模型")
        self.assertEqual(default_adapter.requests, [])
        self.assertEqual(kanagawave_adapter.requests, [request])

    async def test_other_private_user_keeps_default_adapter(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.models import AgentRequest

        default_adapter = _FakeAdapter(text="默认模型")
        kanagawave_adapter = _FakeAdapter(text="KanagawaWave 模型")
        bridge = Bridge(
            self._config(
                allowed_qq_users={10001, 20002},
                enable_group_agent=True,
                bot_qq=999,
                kanagawave_private_users={20002},
            ),
            default_adapter,
            group_runtime=self._group_runtime(
                private_agent_adapter=kanagawave_adapter,
            ),
        )
        request = AgentRequest(10001, "8", "你好", 1.0)

        response = await bridge._call_agent(request)

        self.assertEqual(response.text, "默认模型")
        self.assertEqual(default_adapter.requests, [request])
        self.assertEqual(kanagawave_adapter.requests, [])

    async def test_kanagawave_private_failure_does_not_fall_back_to_default_model(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.models import AgentRequest

        default_adapter = _FakeAdapter(text="不应回退")
        kanagawave_adapter = _FakeAdapter(error=RuntimeError("local model down"))
        bridge = Bridge(
            self._config(
                allowed_qq_users={20002},
                enable_group_agent=True,
                bot_qq=999,
                kanagawave_private_users={20002},
                agent_retry_attempts=1,
            ),
            default_adapter,
            group_runtime=self._group_runtime(
                private_agent_adapter=kanagawave_adapter,
            ),
        )
        request = AgentRequest(20002, "9", "你好", 1.0)

        with self.assertRaisesRegex(RuntimeError, "local model down"):
            await bridge._call_agent(request)

        self.assertEqual(default_adapter.requests, [])
        self.assertEqual(kanagawave_adapter.requests, [request])

    async def test_plain_group_message_sends_when_interjection_is_approved(self):
        from qq_openclaw_bridge.application.group_chat import GroupReply
        from qq_openclaw_bridge.bridge import Bridge

        class Decider:
            def __init__(self):
                self.messages = []

            def should_reply(self, text):
                self.messages.append(text)
                return True

        class Delivery:
            def __init__(self):
                self.requests = []

            async def claim(self, request):
                self.requests.append(request)
                return "token"

            async def send(self, websocket, request, reply, *, claim_token=None):
                return True

        decider = Decider()
        delivery = Delivery()
        coordinator = type("Coordinator", (), {
            "reply": lambda self, request: GroupReply("接梗", "profile", 1),
        })()
        runtime = self._group_runtime(
            coordinator=coordinator,
            delivery=delivery,
            interjection_decider=decider,
        )
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=runtime,
        )
        event = self._group_event()
        event["message"] = [
            {"type": "text", "data": {"text": "这也太离谱了吧"}},
        ]

        self.assertTrue(await bridge.handle_event(event, object()))
        self.assertEqual(decider.messages, ["这也太离谱了吧"])
        self.assertEqual([request.text for request in delivery.requests], [
            "这也太离谱了吧",
        ])

    async def test_plain_group_message_stays_silent_when_interjection_is_declined(self):
        from qq_openclaw_bridge.bridge import Bridge

        class Decider:
            def __init__(self):
                self.messages = []

            def should_reply(self, text):
                self.messages.append(text)
                return False

        class Delivery:
            async def claim(self, request):
                raise AssertionError("declined interjection must not be claimed")

        decider = Decider()
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=self._group_runtime(
                coordinator=object(), delivery=Delivery(),
                interjection_decider=decider,
            ),
        )
        event = self._group_event()
        event["message"] = [
            {"type": "text", "data": {"text": "今晚吃啥"}},
        ]

        self.assertFalse(await bridge.handle_event(event, object()))
        self.assertEqual(decider.messages, ["今晚吃啥"])

    async def test_bot_own_plain_group_message_is_ignored_before_interjection(self):
        from qq_openclaw_bridge.bridge import Bridge

        class Decider:
            def __init__(self):
                self.calls = 0

            def should_reply(self, text):
                self.calls += 1
                return True

        decider = Decider()
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=self._group_runtime(
                coordinator=object(), delivery=object(),
                interjection_decider=decider,
            ),
        )
        event = self._group_event(user_id=999)
        event["message"] = [
            {"type": "text", "data": {"text": "机器人刚发的消息"}},
        ]

        self.assertFalse(await bridge.handle_event(event, object()))
        self.assertEqual(decider.calls, 0)

    async def test_direct_at_skips_interjection_decider_and_cooldown(self):
        from qq_openclaw_bridge.application.group_chat import GroupReply
        from qq_openclaw_bridge.bridge import Bridge

        class ExplodingDecider:
            def should_reply(self, text):
                raise AssertionError("direct at must skip the interjection decision")

        class ExplodingLimiter:
            def allow(self, key):
                raise AssertionError("direct at must skip interjection cooldown")

        class Delivery:
            async def claim(self, request):
                return "token"

            async def send(self, websocket, request, reply, *, claim_token=None):
                return True

        coordinator = type("Coordinator", (), {
            "reply": lambda self, request: GroupReply("回应", "profile", 1),
        })()
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=self._group_runtime(
                coordinator=coordinator,
                delivery=Delivery(),
                interjection_decider=ExplodingDecider(),
                interjection_limiter=ExplodingLimiter(),
            ),
        )

        self.assertTrue(await bridge.handle_event(self._group_event(), object()))

    async def test_group_duplicate_claim_calls_coordinator_and_delivery_once(self):
        from qq_openclaw_bridge.application.group_chat import GroupReply
        from qq_openclaw_bridge.bridge import Bridge

        class Coordinator:
            def __init__(self):
                self.calls = 0

            def reply(self, request):
                self.calls += 1
                return GroupReply("群回复", "profile", 2)

        class Delivery:
            def __init__(self):
                self.claims = set()
                self.calls = 0

            async def claim(self, request):
                key = (request.group_id, request.message_id)
                if key in self.claims:
                    return None
                self.claims.add(key)
                return "claim-token"

            async def send(self, websocket, request, reply, *, claim_token=None):
                self.calls += 1
                self.claim_token = claim_token
                return True

        coordinator = Coordinator()
        delivery = Delivery()
        runtime = self._group_runtime(coordinator=coordinator, delivery=delivery)
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=runtime,
        )
        event = self._group_event()

        self.assertTrue(await bridge.handle_event(event, object()))
        self.assertFalse(await bridge.handle_event(event, object()))
        self.assertEqual(coordinator.calls, 1)
        self.assertEqual(delivery.calls, 1)
        self.assertEqual(delivery.claim_token, "claim-token")

    async def test_disabled_group_mode_keeps_existing_rpc_hooks_inert(self):
        from qq_openclaw_bridge.bridge import Bridge

        bridge = Bridge(
            self._config(enable_group_agent=False),
            _FakeAdapter(text="private"),
            group_runtime=object(),
        )

        self.assertIsNone(bridge.group_runtime)
        self.assertIsNone(bridge._websocket_supervisor._payload_consumer)
        self.assertIsNone(bridge._websocket_supervisor._disconnect_hook)

    async def test_disabled_group_mode_does_not_add_private_event_reads(self):
        from qq_openclaw_bridge.bridge import Bridge

        class CountingEvent(dict):
            def __init__(self, value):
                super().__init__(value)
                self.reads = []

            def get(self, key, default=None):
                self.reads.append(key)
                return super().get(key, default)

        value = {
            "post_type": "message", "message_type": "private",
            "user_id": 10001, "message_id": 8010,
            "message": [{"type": "text", "data": {"text": "私聊"}}],
            "time": 10,
        }
        bridge = Bridge(
            self._config(enable_group_agent=False), _FakeAdapter(text="回复")
        )
        direct = CountingEvent(value)
        routed = CountingEvent(value)

        bridge._event_user_id(direct)
        self.assertTrue(await bridge._handle_event_core(direct, _FakeWebSocket()))
        self.assertTrue(await bridge.handle_event(routed, _FakeWebSocket()))
        self.assertEqual(routed.reads, direct.reads)

    async def test_reply_trigger_checks_local_record_then_strict_rpc_sender(self):
        from qq_openclaw_bridge.application.group_chat import GroupReply
        from qq_openclaw_bridge.bridge import Bridge

        calls = []

        class Repository:
            def is_bot_message(self, group_id, message_id):
                calls.append(("local", group_id, message_id))
                return False

        class Rpc:
            def consume(self, payload):
                return False

            def fail_pending(self):
                return None

            async def request(self, websocket, action, params, timeout=5):
                calls.append(("rpc", action, params, timeout))
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"sender": {"user_id": "999"}},
                }

        class Delivery:
            async def claim(self, request):
                return "token"

            async def send(self, websocket, request, reply, *, claim_token=None):
                return True

        coordinator = type("Coordinator", (), {
            "reply": lambda self, request: GroupReply("回复", "profile", 1),
        })()
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=self._group_runtime(
                repository=Repository(), rpc=Rpc(), coordinator=coordinator,
                delivery=Delivery(),
            ),
        )

        self.assertTrue(await bridge.handle_event(
            self._group_event(reply_id=456), object()
        ))
        self.assertEqual(calls, [
            ("local", 77, "456"),
            ("rpc", "get_msg", {"message_id": "456"}, 5),
        ])

    async def test_disallowed_group_is_rejected_before_reply_lookup(self):
        from qq_openclaw_bridge.bridge import Bridge

        class Repository:
            def is_bot_message(self, *args):
                raise AssertionError("disallowed group must be cheap-rejected")

        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=self._group_runtime(
                repository=Repository(), coordinator=object(), delivery=object(),
                allowed_groups=frozenset({77}),
            ),
        )

        self.assertFalse(await bridge.handle_event(
            self._group_event(group_id=9999, reply_id=456), object()
        ))

    async def test_group_failure_uses_acknowledged_fallback_and_private_still_works(self):
        from qq_openclaw_bridge.bridge import Bridge

        class Coordinator:
            def reply(self, request):
                raise RuntimeError("secret model failure")

        class Delivery:
            def __init__(self):
                self.replies = []

            async def claim(self, request):
                return "token"

            async def send(self, websocket, request, reply, *, claim_token=None):
                self.replies.append(reply)
                return True

        delivery = Delivery()
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="私聊回复"),
            group_runtime=self._group_runtime(
                coordinator=Coordinator(), delivery=delivery
            ),
        )

        self.assertTrue(await bridge.handle_event(self._group_event(), websocket))
        self.assertEqual(delivery.replies[0].safe_error_class, "ollama_unavailable")
        self.assertNotIn("secret", repr(delivery.replies[0]))
        private = {
            "post_type": "message", "message_type": "private",
            "user_id": 10001, "message_id": 8001,
            "message": [{"type": "text", "data": {"text": "私聊"}}],
            "time": 10,
        }
        self.assertTrue(await bridge.handle_event(private, websocket))
        self.assertEqual(websocket.sent[-1]["params"]["message"], "私聊回复")

    async def test_group_fallback_is_filtered_while_private_reply_is_unchanged(self):
        from qq_openclaw_bridge.application.group_delivery import GroupDeliveryService
        from qq_openclaw_bridge.application.group_homophone_filter import (
            GroupHomophoneFilter,
        )
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        class Coordinator:
            def reply(self, request):
                raise RuntimeError("model unavailable")

        class Rpc:
            def __init__(self):
                self.calls = []

            async def request(self, websocket, action, params, timeout=5):
                self.calls.append((action, params))
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 9991},
                }

            def consume(self, payload):
                return False

            def fail_pending(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            map_path = Path(temp_dir) / "group-homophones.json"
            map_path.write_text(
                json.dumps(
                    {"version": 1, "replacements": {"智能体": "知能体"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            repository = WorkspaceStateStore(
                Path(temp_dir) / "state.sqlite3"
            ).community_style_repository
            rpc = Rpc()
            delivery = GroupDeliveryService(
                repository,
                rpc,
                text_filter=GroupHomophoneFilter(map_path).replace,
            )
            bridge = Bridge(
                self._config(enable_group_agent=True, bot_qq=999),
                _FakeAdapter(text="私聊智能体"),
                group_runtime=self._group_runtime(
                    coordinator=Coordinator(),
                    delivery=delivery,
                    repository=repository,
                    rpc=rpc,
                ),
            )
            websocket = _FakeWebSocket()

            self.assertTrue(
                await bridge.handle_event(self._group_event(), websocket)
            )
            private = {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 8002,
                "message": [{"type": "text", "data": {"text": "私聊"}}],
                "time": 10,
            }
            self.assertTrue(await bridge.handle_event(private, websocket))

        self.assertEqual(
            rpc.calls[0],
            (
                "send_group_msg",
                {
                    "group_id": 77,
                    "message": "群聊知能体暂时不可用，请稍后再试。",
                },
            ),
        )
        self.assertEqual(websocket.sent[-1]["params"]["message"], "私聊智能体")

    async def test_payload_and_disconnect_are_fanned_out_to_media_and_group_rpc(self):
        from dataclasses import replace
        from qq_openclaw_bridge.bridge import Bridge

        calls = []

        def rpc(name, consumed):
            return type("Rpc", (), {
                "consume": lambda self, payload: calls.append((name, "consume")) or consumed,
                "fail_pending": lambda self: calls.append((name, "disconnect")),
            })()

        media = replace(self._media_runtime(), rpc_client=rpc("media", True))
        group_rpc = rpc("group", False)
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            media_runtime=media,
            group_runtime=self._group_runtime(
                rpc=group_rpc, coordinator=object(), delivery=object()
            ),
        )

        self.assertTrue(bridge._websocket_supervisor._payload_consumer({"echo": "x"}))
        bridge._websocket_supervisor._disconnect_hook()
        self.assertEqual(calls, [
            ("media", "consume"), ("group", "consume"),
            ("media", "disconnect"), ("group", "disconnect"),
        ])

    async def test_disabled_group_mode_preserves_media_rpc_hook_identity(self):
        from dataclasses import replace
        from qq_openclaw_bridge.bridge import Bridge

        media_rpc = type("Rpc", (), {})()
        media_rpc.consume = lambda payload: False
        media_rpc.fail_pending = lambda: None
        consume = media_rpc.consume
        disconnect = media_rpc.fail_pending
        media = replace(self._media_runtime(), rpc_client=media_rpc)
        bridge = Bridge(
            self._config(enable_group_agent=False),
            _FakeAdapter(text="private"),
            media_runtime=media,
            group_runtime=object(),
        )

        self.assertIs(
            bridge._websocket_supervisor._payload_consumer,
            consume,
        )
        self.assertIs(
            bridge._websocket_supervisor._disconnect_hook,
            disconnect,
        )

    async def test_group_events_serialize_per_group_and_overlap_across_groups(self):
        from qq_openclaw_bridge.application.group_chat import GroupReply
        from qq_openclaw_bridge.bridge import Bridge

        release = threading.Event()

        class Coordinator:
            def __init__(self):
                self.lock = threading.Lock()
                self.started = []
                self.active = 0
                self.max_active = 0

            def reply(self, request):
                with self.lock:
                    self.started.append(request.group_id)
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                release.wait(1)
                with self.lock:
                    self.active -= 1
                return GroupReply("回复", "profile", 1)

        class Delivery:
            def __init__(self):
                self.claimed = set()

            async def claim(self, request):
                key = (request.group_id, request.message_id)
                if key in self.claimed:
                    return None
                self.claimed.add(key)
                return "token-%s-%s" % key

            async def send(self, websocket, request, reply, *, claim_token=None):
                return True

        coordinator = Coordinator()
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=self._group_runtime(
                coordinator=coordinator, delivery=Delivery()
            ),
        )
        first = asyncio.create_task(bridge.handle_event(
            self._group_event(group_id=77, message_id=1001), object()
        ))
        while coordinator.started != [77]:
            await asyncio.sleep(0)
        same_group = asyncio.create_task(bridge.handle_event(
            self._group_event(group_id=77, user_id=89, message_id=1002), object()
        ))
        await asyncio.sleep(0.02)
        self.assertEqual(coordinator.started, [77])
        other_group = asyncio.create_task(bridge.handle_event(
            self._group_event(group_id=78, message_id=1003), object()
        ))
        for _ in range(100):
            if coordinator.started == [77, 78]:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(coordinator.started, [77, 78])
        self.assertEqual(coordinator.max_active, 2)
        release.set()
        self.assertEqual(await asyncio.gather(first, same_group, other_group), [
            True, True, True,
        ])

    async def test_reply_rpc_malformed_sender_fails_closed_without_claim(self):
        from qq_openclaw_bridge.bridge import Bridge

        class Delivery:
            def __init__(self):
                self.claims = 0

            async def claim(self, request):
                self.claims += 1
                return "token"

        malformed = (
            {"status": "failed", "retcode": 0, "data": {"sender": {"user_id": 999}}},
            {"status": type("Status", (str,), {})("ok"), "retcode": 0, "data": {"sender": {"user_id": 999}}},
            {"status": "ok", "data": {"sender": {"user_id": 999}}},
            {"status": "ok", "retcode": "0", "data": {"sender": {"user_id": 999}}},
            {"status": "ok", "retcode": True, "data": {"sender": {"user_id": 999}}},
            {"status": "ok", "retcode": 1, "data": {"sender": {"user_id": 999}}},
            {"status": "ok", "retcode": 0, "data": {"sender": {"user_id": True}}},
            {"status": "ok", "retcode": 0, "data": {"sender": {"user_id": " 999"}}},
            {"status": "ok", "retcode": 0, "data": {"sender": {}}},
        )
        for response in malformed:
            class Rpc:
                def consume(self, payload):
                    return False

                def fail_pending(self):
                    return None

                async def request(self, *args, **kwargs):
                    return response

            with self.subTest(response=response):
                delivery = Delivery()
                bridge = Bridge(
                    self._config(enable_group_agent=True, bot_qq=999),
                    _FakeAdapter(text="private"),
                    group_runtime=self._group_runtime(
                        rpc=Rpc(), coordinator=object(), delivery=delivery
                    ),
                )
                self.assertFalse(await bridge.handle_event(
                    self._group_event(reply_id=456), object()
                ))
                self.assertEqual(delivery.claims, 0)

    async def test_dynamic_group_allowlist_is_refreshed_before_any_lookup(self):
        from qq_openclaw_bridge.application.group_chat import GroupReply
        from qq_openclaw_bridge.bridge import Bridge

        active = {77}
        lookups = []

        class Repository:
            def is_bot_message(self, group_id, message_id):
                lookups.append((group_id, message_id))
                return True

        class Delivery:
            async def claim(self, request):
                return "token"

            async def send(self, websocket, request, reply, *, claim_token=None):
                return True

        coordinator = type("Coordinator", (), {
            "reply": lambda self, request: GroupReply("回复", "profile", 1),
        })()
        runtime = self._group_runtime(
            repository=Repository(), coordinator=coordinator,
            delivery=Delivery(), allowed_groups=frozenset(),
        )
        runtime.allowed_groups_provider = lambda: frozenset(active)
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"), group_runtime=runtime,
        )

        self.assertFalse(await bridge.handle_event(
            self._group_event(group_id=78, reply_id=456), object()
        ))
        self.assertEqual(lookups, [])
        active.add(78)
        self.assertTrue(await bridge.handle_event(
            self._group_event(group_id=78, message_id=124), object()
        ))
        active.remove(77)
        self.assertFalse(await bridge.handle_event(
            self._group_event(group_id=77, message_id=125, reply_id=456), object()
        ))
        self.assertEqual(lookups, [])

    async def test_group_allowlist_refresh_failure_rejects_before_repository(self):
        from qq_openclaw_bridge.bridge import Bridge

        class Repository:
            def is_bot_message(self, *args):
                raise AssertionError("failed refresh must reject before lookup")

        runtime = self._group_runtime(
            repository=Repository(), coordinator=object(), delivery=object(),
        )
        runtime.allowed_groups_provider = lambda: None
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"), group_runtime=runtime,
        )

        self.assertFalse(await bridge.handle_event(
            self._group_event(reply_id=456), object()
        ))

    async def test_group_boundaries_clean_exact_controls_and_contain_subclasses(self):
        from qq_openclaw_bridge.bridge import Bridge

        class SecretKeyboardInterrupt(KeyboardInterrupt):
            pass

        def runtime_for(error):
            runtime = self._group_runtime(
                coordinator=object(), delivery=object(),
            )
            runtime.allowed_groups_provider = lambda: (_ for _ in ()).throw(error)
            return runtime

        exact = KeyboardInterrupt("secret")
        exact.__cause__ = RuntimeError("cause secret")
        exact.__context__ = RuntimeError("context secret")
        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"), group_runtime=runtime_for(exact),
        )
        try:
            await bridge.handle_event(self._group_event(), object())
        except BaseException as error:
            self.assertIs(type(error), KeyboardInterrupt)
            self.assertEqual(error.args, ())
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            frames = []
            traceback = error.__traceback__
            while traceback is not None:
                frames.append(traceback.tb_frame.f_code.co_name)
                traceback = traceback.tb_next
            self.assertNotIn("<lambda>", frames)
            self.assertNotIn("_capture_group_boundary", frames)
        else:
            self.fail("exact control exception was swallowed")

        bridge = Bridge(
            self._config(enable_group_agent=True, bot_qq=999),
            _FakeAdapter(text="private"),
            group_runtime=runtime_for(SecretKeyboardInterrupt("secret")),
        )
        self.assertFalse(await bridge.handle_event(self._group_event(), object()))

    @staticmethod
    def _image_event(message_id=9701, text=""):
        message = [
            {"type": "image", "data": {
                "file": "image-1", "url": "https://fake.invalid/i"
            }}
        ]
        if text:
            message.insert(0, {"type": "text", "data": {"text": text}})
        return {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": message_id,
            "message": message,
            "time": 10,
        }

    async def test_accepted_private_message_sends_agent_reply(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="你好，我是 OpenClaw")
        bridge = Bridge(self._config(), adapter)

        sent = await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 777,
                "message": "你好",
                "time": 10,
            },
            websocket,
        )

        self.assertTrue(sent)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.requests[0].text, "你好")
        self.assertEqual(websocket.sent[0]["action"], "send_private_msg")
        self.assertEqual(
            websocket.sent[0]["params"],
            {"user_id": 10001, "message": "你好，我是 OpenClaw"},
        )

    async def test_e3_memory_runtime_routes_commands_enriches_model_and_observes_reply(self):
        from qq_openclaw_bridge.bridge import Bridge

        command_runtime = _FakeMemoryRuntime(command_reply="目前没有待确认记忆。")
        adapter = _FakeAdapter(text="不应被调用")
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(), adapter, memory_runtime=command_runtime
        )
        await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 700,
                "message": "待确认记忆",
                "time": 10,
            },
            websocket,
        )
        self.assertEqual(adapter.requests, [])

        runtime = _FakeMemoryRuntime()
        adapter = _FakeAdapter(text="模型回复")
        bridge = Bridge(self._config(), adapter, memory_runtime=runtime)
        await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 701,
                "message": "继续项目",
                "time": 10,
            },
            _FakeWebSocket(),
        )

        self.assertEqual(adapter.requests[0].context, "E3 相关记忆")
        self.assertEqual(runtime.learning_service.observed[0][1], "模型回复")

    async def test_style_feedback_is_recorded_without_calling_agent(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="不应被调用")
        style_memory = _FakeStyleMemory(reply="记下了。以后少点客服味，少废话。")
        bridge = Bridge(self._config(), adapter, style_memory=style_memory)

        sent = await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 778,
                "message": "刚才太客服了，少废话",
                "time": 10,
            },
            websocket,
        )

        self.assertTrue(sent)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(style_memory.texts, ["刚才太客服了，少废话"])
        self.assertEqual(
            websocket.sent[0]["params"]["message"],
            "记下了。以后少点客服味，少废话。",
        )

    async def test_style_command_is_handled_without_calling_agent(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="不应被调用")
        style_memory = _FakeStyleMemory(command_reply="当前风格反馈沉淀：\n1. 太客服了")
        bridge = Bridge(self._config(), adapter, style_memory=style_memory)

        sent = await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 778,
                "message": "查看风格",
                "time": 10,
            },
            websocket,
        )

        self.assertTrue(sent)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(style_memory.command_texts, ["查看风格"])
        self.assertEqual(style_memory.texts, [])
        self.assertIn("当前风格反馈沉淀", websocket.sent[0]["params"]["message"])

    async def test_housekeeper_command_is_handled_before_agent_and_style_memory(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="不应被调用")
        housekeeper = _FakeHousekeeper(reply="记下了。以后我会按这个来。")
        style_memory = _FakeStyleMemory(reply="不应被调用")
        bridge = Bridge(
            self._config(),
            adapter,
            style_memory=style_memory,
            housekeeper=housekeeper,
        )

        sent = await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 779,
                "message": "记住：我喜欢先给结论",
                "time": 10,
            },
            websocket,
        )

        self.assertTrue(sent)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(housekeeper.texts, ["记住：我喜欢先给结论"])
        self.assertEqual(style_memory.texts, [])
        self.assertEqual(
            websocket.sent[0]["params"]["message"],
            "记下了。以后我会按这个来。",
        )

    async def test_destructive_command_requires_confirmation_before_housekeeper(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.security import ConfirmationGuard

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="不应被调用")
        housekeeper = _FakeHousekeeper(reply="已删除第 1 条记忆")
        bridge = Bridge(
            self._config(),
            adapter,
            housekeeper=housekeeper,
            confirmation_guard=ConfirmationGuard(id_provider=lambda: "A1B2C3"),
        )

        await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 880,
                "message": "删除记忆 1",
                "time": 10,
            },
            websocket,
        )
        await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 881,
                "message": "确认执行 A1B2C3",
                "time": 11,
            },
            websocket,
        )

        self.assertEqual(housekeeper.texts, ["删除记忆 1"])
        self.assertEqual(adapter.requests, [])
        self.assertIn("待确认", websocket.sent[0]["params"]["message"])
        self.assertIn("已删除", websocket.sent[1]["params"]["message"])

    async def test_sensitive_agent_request_requires_confirmation(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.security import ConfirmationGuard

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="外发能力暂未启用")
        bridge = Bridge(
            self._config(),
            adapter,
            confirmation_guard=ConfirmationGuard(id_provider=lambda: "A1B2C3"),
        )

        await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 882,
                "message": "帮我给张三发消息说今晚开会",
                "time": 10,
            },
            websocket,
        )
        await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 883,
                "message": "确认执行 A1B2C3",
                "time": 11,
            },
            websocket,
        )

        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.requests[0].text, "帮我给张三发消息说今晚开会")
        self.assertIn("待确认", websocket.sent[0]["params"]["message"])
        self.assertEqual(websocket.sent[1]["params"]["message"], "外发能力暂未启用")

    async def test_identity_question_is_handled_without_calling_agent(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="不应被调用")
        bridge = Bridge(self._config(), adapter)

        sent = await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 780,
                "message": "你是谁",
                "time": 10,
            },
            websocket,
        )

        self.assertTrue(sent)
        self.assertEqual(adapter.requests, [])
        reply = websocket.sent[0]["params"]["message"]
        self.assertIn("chichu", reply)
        self.assertIn("本地 QQ 私聊管家", reply)

    async def test_due_reminder_is_pushed_and_marked_sent(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        reminder = _FakeReminder(todo_id=1, user_id=10001, message="该检查 API 额度了")
        reminder_store = _FakeReminderStore([reminder])
        bridge = Bridge(
            self._config(),
            _FakeAdapter(text="不应被调用"),
            reminder_store=reminder_store,
        )
        now = datetime(2026, 7, 11, 20, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

        await bridge._send_due_reminders(websocket, now)

        self.assertEqual(websocket.sent[0]["action"], "send_private_msg")
        self.assertEqual(
            websocket.sent[0]["params"],
            {"user_id": 10001, "message": "该检查 API 额度了"},
        )
        self.assertEqual(reminder_store.sent, [(1, now)])

    async def test_due_reminder_uses_durable_outbox_before_marking_sent(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            reminder = _FakeReminder(
                todo_id=7, user_id=10001, message="该交报告了"
            )
            reminder_store = _FakeReminderStore([reminder])
            websocket = _FailingWebSocket(failures=1)
            bridge = Bridge(
                self._config(outbox_retry_base_seconds=1),
                _FakeAdapter(text="unused"),
                reminder_store=reminder_store,
                state_store=store,
            )
            now = datetime(2026, 7, 16, 20, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

            await bridge._send_due_reminders(websocket, now)
            self.assertEqual(reminder_store.sent, [])
            self.assertEqual(store.transport_stats()["outbox_pending"], 1)

            await asyncio.sleep(1.05)
            await bridge._send_due_reminders(websocket, now)

            self.assertEqual(len(websocket.sent), 1)
            self.assertEqual(reminder_store.sent, [(7, now)])

    async def test_auto_backup_runs_through_bridge_manager(self):
        from qq_openclaw_bridge.bridge import Bridge

        manager = _FakeBackupManager({"name": "auto-20260714.zip"})
        bridge = Bridge(
            self._config(enable_auto_backup=True),
            _FakeAdapter(text="unused"),
            backup_manager=manager,
        )

        with self.assertLogs("qq_openclaw_bridge", level="INFO") as logs:
            bridge._run_auto_backup_once()

        self.assertEqual(manager.calls, 1)
        self.assertIn("auto-20260714.zip", "\n".join(logs.output))

    async def test_ignored_message_does_not_call_agent_or_send_reply(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="不应被调用")
        bridge = Bridge(self._config(), adapter)

        sent = await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 10001,
                "message": "你好",
            },
            websocket,
        )

        self.assertFalse(sent)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(websocket.sent, [])

    async def test_agent_error_sends_fallback_reply(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(error=RuntimeError("boom"))
        bridge = Bridge(self._config(), adapter)

        with self.assertLogs("qq_openclaw_bridge", level="ERROR"):
            sent = await bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message_id": 777,
                    "message": "你好",
                    "time": 10,
                },
                websocket,
            )

        self.assertTrue(sent)
        self.assertEqual(
            websocket.sent[0]["params"]["message"], "智能体暂时不可用，请稍后再试。"
        )

    async def test_agent_timeout_sends_chichu_timeout_reply(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(
            error=subprocess.TimeoutExpired(["openclaw"], timeout=120)
        )
        bridge = Bridge(self._config(fallback_reply="智能体暂时不可用，请稍后再试。"), adapter)

        with self.assertLogs("qq_openclaw_bridge", level="ERROR"):
            await bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message_id": 777,
                    "message": "你好",
                    "time": 10,
                },
                websocket,
            )

        reply = websocket.sent[0]["params"]["message"]
        self.assertIn("OpenClaw", reply)
        self.assertIn("卡住", reply)

    async def test_agent_proxy_error_sends_chichu_proxy_reply(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(error=RuntimeError("unsupported_region"))
        bridge = Bridge(self._config(), adapter)

        with self.assertLogs("qq_openclaw_bridge", level="ERROR"):
            await bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message_id": 777,
                    "message": "你好",
                    "time": 10,
                },
                websocket,
            )

        reply = websocket.sent[0]["params"]["message"]
        self.assertIn("代理", reply)
        self.assertIn("chichu", reply)

    async def test_long_reply_is_truncated_with_suffix(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="x" * 100)
        bridge = Bridge(self._config(max_reply_chars=20), adapter)

        await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 777,
                "message": "你好",
                "time": 10,
            },
            websocket,
        )

        reply = websocket.sent[0]["params"]["message"]
        self.assertLessEqual(len(reply), 20)
        self.assertTrue(reply.endswith("（已截断）"))

    async def test_unsupported_whitelisted_message_gets_text_only_notice(self):
        from qq_openclaw_bridge.bridge import Bridge

        websocket = _FakeWebSocket()
        adapter = _FakeAdapter(text="不应被调用")
        bridge = Bridge(self._config(), adapter)

        sent = await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 777,
                "message": [{"type": "image", "data": {"file": "a.png"}}],
                "time": 10,
            },
            websocket,
        )

        self.assertTrue(sent)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(websocket.sent[0]["params"]["message"], "第一版只支持文本消息。")

    async def test_enabled_image_only_reaches_agent_with_bounded_media_context(self):
        from qq_openclaw_bridge.application.media_pipeline import MediaArtifact
        from qq_openclaw_bridge.bridge import Bridge, TEXT_ONLY_NOTICE

        pipeline = _FakePipeline((
            MediaArtifact("image", "图片描述", "一张测试图", "ready", "asset-1"),
        ))
        adapter = _FakeAdapter(text="看到了")
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(enable_media_pipeline=True),
            adapter,
            media_runtime=self._media_runtime(pipeline=pipeline),
        )

        self.assertTrue(await bridge.handle_event(self._image_event(), websocket))

        self.assertEqual(len(adapter.requests), 1)
        request = adapter.requests[0]
        self.assertEqual(request.text, "")
        self.assertIn("[不可信图片描述]", request.media_context)
        self.assertIn("一张测试图", request.media_context)
        self.assertNotEqual(websocket.sent[0]["params"]["message"], TEXT_ONLY_NOTICE)

    async def test_quote_and_e3_context_reach_one_request_then_observe_after_delivery(self):
        from qq_openclaw_bridge.application.media_pipeline import MediaArtifact
        from qq_openclaw_bridge.application.quotes import QuoteArtifact
        from qq_openclaw_bridge.bridge import Bridge

        pipeline = _FakePipeline((
            MediaArtifact("image", "图片描述", "图片内容", "ready", "asset-1"),
        ))
        quote = QuoteArtifact("引用原文", "available", "q1")
        memory = _FakeMemoryRuntime()
        adapter = _FakeAdapter(text="联合回复")
        websocket = _FakeWebSocket()
        runtime = self._media_runtime(pipeline=pipeline, quote=quote)
        bridge = Bridge(
            self._config(enable_media_pipeline=True),
            adapter,
            memory_runtime=memory,
            media_runtime=runtime,
        )
        event = self._image_event(text="结合回答")
        event["message"].insert(1, {
            "type": "reply", "data": {"id": "q1"}
        })

        self.assertTrue(await bridge.handle_event(event, websocket))

        request = adapter.requests[0]
        self.assertEqual(request.context, "E3 相关记忆")
        self.assertIn("[不可信引用内容]\n引用原文", request.media_context)
        self.assertEqual(len(runtime.quote_resolver.calls), 1)
        self.assertEqual(len(memory.learning_service.observed), 1)
        observed_request, observed_reply = memory.learning_service.observed[0]
        self.assertIs(observed_request, request)
        self.assertEqual(observed_reply, "联合回复")
        self.assertEqual(len(websocket.sent), 1)

    async def test_embedded_quote_only_reaches_model_as_empty_text_request(self):
        from qq_openclaw_bridge.application.quotes import QuoteResolver
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.runtime.media import MediaRuntime

        class Rpc:
            async def request(self, *args, **kwargs):
                raise AssertionError("embedded quote must not use RPC")

            def consume(self, payload):
                return False

            def fail_pending(self, error=None):
                return None

        rpc = Rpc()
        runtime = MediaRuntime(None, QuoteResolver(rpc), None, None, rpc)
        adapter = _FakeAdapter(text="引用回复")
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(enable_long_tasks=True),
            adapter,
            media_runtime=runtime,
        )
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": 9710,
            "message": [
                {"type": "reply", "data": {"id": "q-embedded", "text": "内嵌引用"}}
            ],
            "time": 10,
        }

        self.assertTrue(await bridge.handle_event(event, websocket))

        self.assertEqual(adapter.requests[0].text, "")
        self.assertIn("内嵌引用", adapter.requests[0].media_context)

    async def test_rpc_fetched_quote_only_reaches_model_context(self):
        from qq_openclaw_bridge.application.quotes import QuoteResolver
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.runtime.media import MediaRuntime

        class Rpc:
            def __init__(self):
                self.calls = []

            async def request(self, websocket, action, params, timeout):
                self.calls.append((action, params, timeout))
                return {
                    "status": "ok",
                    "data": {"message": [{"type": "text", "data": {"text": "RPC 引用"}}]},
                }

            def consume(self, payload):
                return False

            def fail_pending(self, error=None):
                return None

        rpc = Rpc()
        runtime = MediaRuntime(None, QuoteResolver(rpc), None, None, rpc)
        adapter = _FakeAdapter(text="引用回复")
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(enable_long_tasks=True),
            adapter,
            media_runtime=runtime,
        )
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": 9711,
            "message": [{"type": "reply", "data": {"id": "q-rpc"}}],
            "time": 10,
        }

        self.assertTrue(await bridge.handle_event(event, websocket))

        self.assertEqual(adapter.requests[0].text, "")
        self.assertIn("RPC 引用", adapter.requests[0].media_context)
        self.assertEqual(rpc.calls, [("get_msg", {"message_id": "q-rpc"}, 5)])

    async def test_failed_quote_is_controlled_and_duplicate_media_has_no_side_effects(self):
        from qq_openclaw_bridge.application.media_pipeline import MediaArtifact
        from qq_openclaw_bridge.application.quotes import QuoteArtifact
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            pipeline = _FakePipeline((
                MediaArtifact("image", "图片描述", "内容", "ready", "asset-1"),
            ))
            tasks = _FakeTaskService(repository=store.e4_repository)
            adapter = _FakeAdapter(text="回复")
            runtime = self._media_runtime(
                pipeline=pipeline,
                quote=QuoteArtifact("", "unavailable", "q1"),
                task_service=tasks,
                task_commands=_FakeTaskCommands(),
            )
            bridge = Bridge(
                self._config(enable_media_pipeline=True, enable_long_tasks=True),
                adapter,
                state_store=store,
                media_runtime=runtime,
            )
            event = self._image_event(9702)
            event["message"].insert(0, {
                "type": "reply", "data": {"id": "q1"}
            })

            self.assertTrue(await bridge.handle_event(event, _FakeWebSocket()))
            self.assertFalse(await bridge.handle_event(event, _FakeWebSocket()))

            self.assertEqual(len(tasks.runs), 1)
            self.assertEqual(len(pipeline.calls), 1)
            self.assertEqual(len(adapter.requests), 1)
            self.assertIn("引用内容不可用", adapter.requests[0].media_context)

    async def test_task_command_is_local_and_never_creates_task_or_calls_model(self):
        from qq_openclaw_bridge.bridge import Bridge

        tasks = _FakeTaskService()
        commands = _FakeTaskCommands("任务列表为空。")
        adapter = _FakeAdapter(text="不应调用")
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(enable_long_tasks=True),
            adapter,
            media_runtime=self._media_runtime(
                pipeline=None, task_service=tasks, task_commands=commands
            ),
        )
        event = {
            "post_type": "message", "message_type": "private",
            "user_id": 10001, "message_id": 9703,
            "message": "查看任务", "time": 10,
        }

        self.assertTrue(await bridge.handle_event(event, websocket))

        self.assertEqual(len(commands.requests), 1)
        self.assertEqual(tasks.runs, [])
        self.assertEqual(adapter.requests, [])
        self.assertEqual(websocket.sent[0]["params"]["message"], "任务列表为空。")

    async def test_long_task_notice_and_final_use_distinct_outbox_rows(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            tasks = _FakeTaskService(
                notice="任务已接单", repository=store.e4_repository
            )
            websocket = _FakeWebSocket()
            bridge = Bridge(
                self._config(enable_long_tasks=True),
                _FakeAdapter(text="最终结果"),
                state_store=store,
                media_runtime=self._media_runtime(
                    pipeline=None,
                    task_service=tasks,
                    task_commands=_FakeTaskCommands(),
                ),
            )
            event = {
                "post_type": "message", "message_type": "private",
                "user_id": 10001, "message_id": 9704,
                "message": "耗时问题", "time": 10,
            }

            self.assertTrue(await bridge.handle_event(event, websocket))

            self.assertEqual(
                store.get_outbox("task:task-44:notice:1")["status"], "sent"
            )
            self.assertEqual(store.get_outbox("reply:9704")["status"], "sent")
            self.assertEqual(len(websocket.sent), 2)
            self.assertEqual(
                [item["params"]["message"] for item in websocket.sent],
                ["任务已接单", "最终结果"],
            )

    async def test_missing_message_id_direct_long_task_delivery_finishes_succeeded(self):
        from qq_openclaw_bridge.application.long_tasks import LongTaskService
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            task_service = LongTaskService(
                store.e4_repository, ack_seconds=3600
            )
            bridge = Bridge(
                self._config(enable_long_tasks=True),
                _FakeAdapter(text="直接结果"),
                state_store=store,
                media_runtime=self._media_runtime(
                    pipeline=None,
                    task_service=task_service,
                    task_commands=_FakeTaskCommands(),
                ),
            )
            websocket = _FakeWebSocket()
            event = {
                "post_type": "message", "message_type": "private",
                "user_id": 10001, "message": "无稳定消息号", "time": 10,
            }

            self.assertTrue(await bridge.handle_event(event, websocket))

            task = store.e4_repository.list_tasks()[0]
            self.assertEqual((task["status"], task["stage"]), ("succeeded", "succeeded"))
            self.assertEqual(task_service.active_task_ids, ())
            self.assertEqual(
                [item["params"]["message"] for item in websocket.sent],
                ["直接结果"],
            )
            with store._connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0
                )

    async def test_owner_cancellation_sends_and_observes_nothing(self):
        from qq_openclaw_bridge.bridge import Bridge

        memory = _FakeMemoryRuntime()
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(enable_long_tasks=True),
            _FakeAdapter(text="不得发送"),
            memory_runtime=memory,
            media_runtime=self._media_runtime(
                pipeline=None,
                task_service=_FakeTaskService(cancel=True),
                task_commands=_FakeTaskCommands(),
            ),
        )
        event = {
            "post_type": "message", "message_type": "private",
            "user_id": 10001, "message_id": 9705,
            "message": "取消中的任务", "time": 10,
        }

        self.assertFalse(await bridge.handle_event(event, websocket))
        self.assertEqual(websocket.sent, [])
        self.assertEqual(memory.learning_service.observed, [])

    async def test_permanent_final_failure_replay_has_no_side_effect_or_rate_mutation(self):
        from qq_openclaw_bridge.application.delivery import ReliableDeliveryService
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.models import AgentRequest
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            old = datetime(2026, 7, 27, tzinfo=timezone.utc)
            store.claim_inbound("m-replay-failed", 10001, now=old)
            task = store.e4_repository.create_task(
                10001, "m-replay-failed", "long_task", task_id="failed-task",
                now=old,
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=old
            )

            async def offline(*args, **kwargs):
                raise ConnectionError("offline")

            delivery = ReliableDeliveryService(
                store, offline, 0, 1, now_provider=lambda: old
            )
            request = AgentRequest(10001, "m-replay-failed", "旧请求", 10.0)
            self.assertFalse(
                await delivery.send_final(
                    "ws", request, "不得重放", task_id="failed-task"
                )
            )
            tasks = _FakeTaskService(repository=store.e4_repository)
            adapter = _FakeAdapter(text="新请求结果")
            runtime = self._media_runtime(
                pipeline=None, task_service=tasks,
                task_commands=_FakeTaskCommands(),
            )
            bridge = Bridge(
                self._config(
                    enable_long_tasks=True,
                    rate_limit_messages_per_minute=1,
                ),
                adapter,
                state_store=store,
                media_runtime=runtime,
            )
            websocket = _FakeWebSocket()
            replay = {
                "post_type": "message", "message_type": "private",
                "user_id": 10001, "message_id": "m-replay-failed",
                "message": [
                    {"type": "text", "data": {"text": "旧请求"}},
                    {"type": "reply", "data": {"id": "quote-never"}},
                ],
                "time": 10,
            }

            self.assertFalse(await bridge.handle_event(replay, websocket))
            self.assertEqual(tasks.runs, [])
            self.assertEqual(runtime.quote_resolver.calls, [])
            self.assertEqual(adapter.requests, [])
            self.assertEqual(websocket.sent, [])
            with store._connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM long_tasks").fetchone()[0], 1
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2
                )

            fresh = dict(replay)
            fresh["message_id"] = "m-fresh-after-failure"
            fresh["message"] = "新请求"
            self.assertTrue(await bridge.handle_event(fresh, websocket))
            self.assertEqual(len(tasks.runs), 1)
            self.assertEqual(len(adapter.requests), 1)

    async def test_legacy_sent_final_reconciles_before_any_replay_side_effect(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            old = datetime(2026, 7, 30, tzinfo=timezone.utc)
            store.claim_inbound("m-legacy", 10001, now=old)
            store.enqueue_outbox(
                kind="reply", user_id=10001, message="already sent",
                dedupe_key="reply:m-legacy", echo="reply:m-legacy", now=old,
            )
            with store._connect() as connection:
                connection.execute(
                    """
                    UPDATE outbox SET status='sent', attempts=1, sent_at=?
                    WHERE dedupe_key='reply:m-legacy'
                    """,
                    (old.isoformat(),),
                )

            pipeline = _FakePipeline()
            tasks = _FakeTaskService(repository=store.e4_repository)
            task_commands = _FakeTaskCommands()
            runtime = self._media_runtime(
                pipeline=pipeline,
                task_service=tasks,
                task_commands=task_commands,
            )
            memory = _FakeMemoryRuntime()
            adapter = _FakeAdapter(text="must not run")
            bridge = Bridge(
                self._config(
                    enable_media_pipeline=True,
                    enable_long_tasks=True,
                    rate_limit_messages_per_minute=1,
                ),
                adapter,
                state_store=WorkspaceStateStore(store._database.path),
                media_runtime=runtime,
                memory_runtime=memory,
            )
            websocket = _FakeWebSocket()
            replay = {
                "post_type": "message", "message_type": "private",
                "user_id": 10001, "message_id": "m-legacy",
                "message": [
                    {"type": "text", "data": {"text": "/旧命令"}},
                    {"type": "reply", "data": {"id": "quote-never"}},
                    {"type": "image", "data": {"file": "image-never"}},
                ],
                "time": 10,
            }

            self.assertFalse(await bridge.handle_event(replay, websocket))
            self.assertEqual(pipeline.calls, [])
            self.assertEqual(runtime.quote_resolver.calls, [])
            self.assertEqual(task_commands.requests, [])
            self.assertEqual(memory.command_handler.requests, [])
            self.assertEqual(tasks.runs, [])
            self.assertEqual(adapter.requests, [])
            self.assertEqual(websocket.sent, [])
            with store._connect() as connection:
                status = connection.execute(
                    """
                    SELECT status FROM inbound_messages
                    WHERE message_id='m-legacy'
                    """
                ).fetchone()[0]
            self.assertEqual(status, "completed")

            fresh = dict(replay)
            fresh["message_id"] = "m-fresh"
            fresh["message"] = "fresh request"
            self.assertTrue(await bridge.handle_event(fresh, websocket))

    async def test_owner_cancel_replay_has_no_side_effect_or_rate_mutation(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            old = datetime(2026, 7, 27, tzinfo=timezone.utc)
            store.claim_inbound("m-replay-cancel", 10001, now=old)
            task = store.e4_repository.create_task(
                10001, "m-replay-cancel", "long_task", task_id="cancelled-task",
                now=old,
            )
            store.e4_repository.transition_task(
                task["id"], "queued", "running", "running", now=old
            )
            self.assertIsNotNone(
                store.e4_repository.request_cancel(
                    "cancelled-task", 10001, now=old
                )
            )
            store.e4_repository.transition_task(
                "cancelled-task", "running", "cancelled", "cancelled", now=old
            )
            tasks = _FakeTaskService(repository=store.e4_repository)
            adapter = _FakeAdapter(text="新请求结果")
            runtime = self._media_runtime(
                pipeline=None, task_service=tasks,
                task_commands=_FakeTaskCommands(),
            )
            bridge = Bridge(
                self._config(
                    enable_long_tasks=True,
                    rate_limit_messages_per_minute=1,
                ),
                adapter,
                state_store=store,
                media_runtime=runtime,
            )
            websocket = _FakeWebSocket()
            replay = {
                "post_type": "message", "message_type": "private",
                "user_id": 10001, "message_id": "m-replay-cancel",
                "message": [
                    {"type": "text", "data": {"text": "旧请求"}},
                    {"type": "reply", "data": {"id": "quote-never"}},
                ],
                "time": 10,
            }

            self.assertFalse(await bridge.handle_event(replay, websocket))
            self.assertEqual(tasks.runs, [])
            self.assertEqual(runtime.quote_resolver.calls, [])
            self.assertEqual(adapter.requests, [])
            self.assertEqual(websocket.sent, [])
            with store._connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM long_tasks").fetchone()[0], 1
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0
                )

            fresh = dict(replay)
            fresh["message_id"] = "m-fresh-after-cancel"
            fresh["message"] = "新请求"
            self.assertTrue(await bridge.handle_event(fresh, websocket))
            self.assertEqual(len(tasks.runs), 1)
            self.assertEqual(len(adapter.requests), 1)

    async def test_long_task_failure_persists_failed_and_sends_controlled_fallback(self):
        from qq_openclaw_bridge.application.long_tasks import LongTaskService
        from qq_openclaw_bridge.application.quotes import QuoteArtifact
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.runtime.media import MediaRuntime
        from qq_openclaw_bridge.runtime.onebot_rpc import OneBotRpcClient
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            task_service = LongTaskService(
                store.e4_repository, ack_seconds=3600
            )
            runtime = MediaRuntime(
                pipeline=_FakePipeline(
                    error=RuntimeError("https://secret.invalid/?token=leak")
                ),
                quote_resolver=_FakeQuoteResolver(
                    QuoteArtifact("", "absent", None)
                ),
                task_service=task_service,
                task_commands=_FakeTaskCommands(),
                rpc_client=OneBotRpcClient(),
            )
            websocket = _FakeWebSocket()
            bridge = Bridge(
                self._config(enable_media_pipeline=True, enable_long_tasks=True),
                _FakeAdapter(text="不得调用"),
                state_store=store,
                media_runtime=runtime,
            )

            self.assertTrue(await bridge.handle_event(self._image_event(9706), websocket))

            task = store.e4_repository.list_tasks()[0]
            self.assertEqual(task["status"], "failed")
            self.assertEqual(
                websocket.sent[-1]["params"]["message"],
                "智能体暂时不可用，请稍后再试。",
            )
            self.assertNotIn("secret.invalid", repr(task))

    async def test_pure_text_with_media_enabled_never_runs_pipeline(self):
        from qq_openclaw_bridge.bridge import Bridge

        pipeline = _FakePipeline(error=AssertionError("must not process pure text"))
        adapter = _FakeAdapter(text="纯文本回复")
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(enable_media_pipeline=True),
            adapter,
            media_runtime=self._media_runtime(pipeline=pipeline),
        )
        event = {
            "post_type": "message", "message_type": "private",
            "user_id": 10001, "message_id": 9707,
            "message": "纯文本", "time": 10,
        }

        self.assertTrue(await bridge.handle_event(event, websocket))
        self.assertEqual(pipeline.calls, [])
        self.assertEqual(len(adapter.requests), 1)

    async def test_e3_observer_waits_for_final_outbox_retry_and_runs_once(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            memory = _FakeMemoryRuntime()
            websocket = _FailingWebSocket(failures=1)
            bridge = Bridge(
                self._config(
                    enable_media_pipeline=True,
                    outbox_retry_base_seconds=1,
                ),
                _FakeAdapter(text="延迟送达"),
                state_store=store,
                memory_runtime=memory,
                media_runtime=self._media_runtime(pipeline=_FakePipeline()),
            )
            event = {
                "post_type": "message", "message_type": "private",
                "user_id": 10001, "message_id": 9708,
                "message": "需要重试", "time": 10,
            }

            await bridge.handle_event(event, websocket)
            self.assertEqual(memory.learning_service.observed, [])

            await asyncio.sleep(1.05)
            self.assertTrue(await bridge.deliver_outbox_once(websocket))
            self.assertEqual(len(memory.learning_service.observed), 1)
            self.assertEqual(
                memory.learning_service.observed[0][1], "延迟送达"
            )
            self.assertFalse(await bridge.deliver_outbox_once(websocket))
            self.assertEqual(len(memory.learning_service.observed), 1)

    async def test_dispatch_event_does_not_wait_for_slow_agent(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.models import AgentResponse

        started = threading.Event()
        release = threading.Event()

        class SlowAdapter:
            def reply(self, request):
                started.set()
                release.wait(timeout=2)
                return AgentResponse(text="慢回复")

        websocket = _FakeWebSocket()
        bridge = Bridge(self._config(), SlowAdapter())

        task = await bridge.dispatch_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 9001,
                "message": "慢问题",
                "time": 10,
            },
            websocket,
        )
        started_ok = await asyncio.to_thread(started.wait, 1)

        self.assertTrue(started_ok)
        self.assertFalse(task.done())
        release.set()
        await bridge.wait_for_event_tasks()
        self.assertEqual(websocket.sent[0]["params"]["message"], "慢回复")

    async def test_same_user_is_serialized_while_different_users_can_overlap(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.models import AgentResponse

        class TrackingAdapter:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.active_by_user = {}
                self.max_by_user = {}
                self.lock = threading.Lock()

            def reply(self, request):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    active = self.active_by_user.get(request.user_id, 0) + 1
                    self.active_by_user[request.user_id] = active
                    self.max_by_user[request.user_id] = max(
                        self.max_by_user.get(request.user_id, 0), active
                    )
                time.sleep(0.08)
                with self.lock:
                    self.active -= 1
                    self.active_by_user[request.user_id] -= 1
                return AgentResponse(text="ok-%s" % request.message_id)

        adapter = TrackingAdapter()
        bridge = Bridge(
            self._config(
                allowed_qq_users={10001, 10002}, max_concurrent_agent_calls=2
            ),
            adapter,
        )
        websocket = _FakeWebSocket()

        def event(user_id, message_id):
            return {
                "post_type": "message",
                "message_type": "private",
                "user_id": user_id,
                "message_id": message_id,
                "message": "问题 %s" % message_id,
                "time": 10,
            }

        await asyncio.gather(
            bridge.handle_event(event(10001, 9101), websocket),
            bridge.handle_event(event(10001, 9102), websocket),
            bridge.handle_event(event(10002, 9201), websocket),
        )

        self.assertEqual(adapter.max_by_user[10001], 1)
        self.assertEqual(adapter.max_by_user[10002], 1)
        self.assertEqual(adapter.max_active, 2)

    async def test_duplicate_message_id_is_processed_only_once(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            adapter = _FakeAdapter(text="只回复一次")
            websocket = _FakeWebSocket()
            bridge = Bridge(self._config(), adapter, state_store=store)
            event = {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 9301,
                "message": "重复测试",
                "time": 10,
            }

            first = await bridge.handle_event(event, websocket)
            second = await bridge.handle_event(event, websocket)

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(len(adapter.requests), 1)
            self.assertEqual(len(websocket.sent), 1)

    async def test_duplicate_transport_does_not_consume_an_extra_rate_slot(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            adapter = _FakeAdapter(text="ok")
            websocket = _FakeWebSocket()
            bridge = Bridge(
                self._config(rate_limit_messages_per_minute=2),
                adapter,
                state_store=store,
            )

            def event(message_id):
                return {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message_id": message_id,
                    "message": "hello",
                    "time": 10,
                }

            self.assertTrue(await bridge.handle_event(event(9301), websocket))
            self.assertFalse(await bridge.handle_event(event(9301), websocket))
            self.assertTrue(await bridge.handle_event(event(9302), websocket))
            self.assertEqual(len(adapter.requests), 2)

    async def test_outbox_retries_after_websocket_failure(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "chichu.sqlite3")
            websocket = _FailingWebSocket(failures=1)
            bridge = Bridge(
                self._config(outbox_retry_base_seconds=1),
                _FakeAdapter(text="可靠回复"),
                state_store=store,
            )

            await bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message_id": 9401,
                    "message": "可靠发送",
                    "time": 10,
                },
                websocket,
            )
            queued = store.get_outbox("reply:9401")
            self.assertEqual(queued["status"], "pending")

            await asyncio.sleep(1.05)
            await bridge.deliver_outbox_once(websocket)

            self.assertEqual(len(websocket.sent), 1)
            self.assertEqual(store.get_outbox("reply:9401")["status"], "sent")

    async def test_transient_agent_failure_retries_before_fallback(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.models import AgentResponse

        class RetryAdapter:
            def __init__(self):
                self.calls = 0

            def reply(self, request):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("connect timeout")
                return AgentResponse(text="重试成功")

        adapter = RetryAdapter()
        websocket = _FakeWebSocket()
        bridge = Bridge(self._config(agent_retry_attempts=2), adapter)

        await bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message_id": 9501,
                "message": "重试",
                "time": 10,
            },
            websocket,
        )

        self.assertEqual(adapter.calls, 2)
        self.assertEqual(websocket.sent[0]["params"]["message"], "重试成功")

    async def test_subprocess_timeout_does_not_retry_ambiguous_agent_call(self):
        from qq_openclaw_bridge.bridge import Bridge

        class TimeoutAdapter:
            def __init__(self):
                self.calls = 0

            def reply(self, request):
                self.calls += 1
                raise subprocess.TimeoutExpired(["openclaw"], timeout=120)

        adapter = TimeoutAdapter()
        websocket = _FakeWebSocket()
        bridge = Bridge(self._config(agent_retry_attempts=2), adapter)

        with self.assertLogs("qq_openclaw_bridge", level="ERROR"):
            await bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message_id": 9502,
                    "message": "不要重复收费",
                    "time": 10,
                },
                websocket,
            )

        self.assertEqual(adapter.calls, 1)
        self.assertIn("OpenClaw", websocket.sent[0]["params"]["message"])

    async def test_circuit_breaker_skips_agent_during_cooldown(self):
        from qq_openclaw_bridge.bridge import Bridge

        adapter = _FakeAdapter(error=RuntimeError("permanent failure"))
        websocket = _FakeWebSocket()
        bridge = Bridge(
            self._config(agent_circuit_failure_threshold=2), adapter
        )

        for message_id in (9601, 9602, 9603):
            with self.assertLogs("qq_openclaw_bridge", level="ERROR"):
                await bridge.handle_event(
                    {
                        "post_type": "message",
                        "message_type": "private",
                        "user_id": 10001,
                        "message_id": message_id,
                        "message": "故障 %s" % message_id,
                        "time": 10,
                    },
                    websocket,
                )

        self.assertEqual(len(adapter.requests), 2)


if __name__ == "__main__":
    unittest.main()
