from pathlib import Path
import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _CapturedBridge(dict):
    def __init__(self, values):
        super().__init__(values)
        self.message_coordinator = values["message_coordinator"]

    async def _call_agent(self, request):
        raise AssertionError("capture bridge must not execute model calls")


class _FakeApprovalBindingKeyProvider:
    def get(self):
        return b"p" * 32


class CompositionTests(unittest.TestCase):
    def test_disabled_group_runtime_is_inert_before_touching_path_or_store(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.community_style import create_group_runtime

        class ExplodingStore:
            def __getattribute__(self, name):
                raise AssertionError("disabled runtime must not inspect state store")

        runtime = create_group_runtime(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={1},
                enable_group_agent=False,
                style_agent_config_path="/does/not/exist.json",
            ),
            ExplodingStore(),
            config_reloader_factory=lambda path: (_ for _ in ()).throw(
                AssertionError("disabled runtime must not open config")
            ),
            ollama_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("disabled runtime must not create Ollama")
            ),
        )

        self.assertIsNone(runtime)

    def test_enabled_group_runtime_is_frozen_and_uses_dedicated_dependencies(self):
        from dataclasses import FrozenInstanceError
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.community_style import create_group_runtime
        from qq_openclaw_bridge.runtime.community_style_config import (
            CommunityStyleSettings, GroupBindingSettings, OllamaSettings,
        )

        settings = CommunityStyleSettings(
            True,
            OllamaSettings("http://127.0.0.1:11434", "qwen3:8b", False),
            (),
            (GroupBindingSettings(77, "profile", 5, 800),),
            "f" * 64,
        )
        reloader = type("Reloader", (), {
            "current": lambda self: settings,
            "reload_if_changed": lambda self: False,
        })()
        repository = object()
        store = type("Store", (), {"community_style_repository": repository})()
        rpc = type("Rpc", (), {
            "request": lambda *args, **kwargs: None,
            "consume": lambda self, payload: False,
            "fail_pending": lambda self: None,
        })()
        ollama = object()
        coordinator = type("Coordinator", (), {
            "current_group_ids": lambda self: frozenset({77}),
        })()
        delivery = object()

        runtime = create_group_runtime(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={1},
                bot_qq=999,
                enable_group_agent=True,
                style_agent_config_path="config.json",
            ),
            store,
            config_reloader_factory=lambda path: reloader,
            rpc_factory=lambda: rpc,
            ollama_factory=lambda configured, **kwargs: ollama,
            coordinator_factory=lambda repo, loader, model: coordinator,
            delivery_factory=lambda repo, client: delivery,
        )

        self.assertEqual(runtime.allowed_groups_provider, coordinator.current_group_ids)
        self.assertEqual(runtime.allowed_groups_provider(), frozenset({77}))
        self.assertIs(runtime.repository, repository)
        self.assertIs(runtime.rpc_client, rpc)
        self.assertIs(runtime.coordinator, coordinator)
        self.assertIs(runtime.delivery, delivery)
        with self.assertRaises(FrozenInstanceError):
            runtime.delivery = object()

    def test_enabled_group_homophone_filter_is_injected_only_into_group_delivery(self):
        import json

        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.community_style import create_group_runtime
        from qq_openclaw_bridge.runtime.community_style_config import (
            CommunityStyleSettings, GroupBindingSettings, OllamaSettings,
        )

        settings = CommunityStyleSettings(
            True,
            OllamaSettings("http://127.0.0.1:11434", "qwen3:8b", False),
            (),
            (GroupBindingSettings(77, "profile", 5, 800),),
            "f" * 64,
        )
        reloader = type("Reloader", (), {
            "current": lambda self: settings,
            "reload_if_changed": lambda self: False,
        })()
        repository = object()
        store = type("Store", (), {"community_style_repository": repository})()
        rpc = type("Rpc", (), {
            "request": lambda *args, **kwargs: None,
            "consume": lambda self, payload: False,
            "fail_pending": lambda self: None,
        })()
        captured = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-homophones.json"
            path.write_text(
                json.dumps(
                    {"version": 1, "replacements": {"敏感词": "米感词"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            runtime = create_group_runtime(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users={1},
                    bot_qq=999,
                    enable_group_agent=True,
                    enable_group_homophone_filter=True,
                    group_homophone_map_path=str(path),
                    style_agent_config_path="config.json",
                ),
                store,
                config_reloader_factory=lambda _path: reloader,
                rpc_factory=lambda: rpc,
                ollama_factory=lambda configured, **kwargs: object(),
                coordinator_factory=lambda repo, loader, model: type(
                    "Coordinator", (), {
                        "current_group_ids": lambda self: frozenset({77}),
                    }
                )(),
                delivery_factory=lambda repo, client, **kwargs: (
                    captured.update(kwargs) or object()
                ),
            )
            self.assertIsNotNone(runtime)
            self.assertEqual(
                captured["text_filter"]("含有敏感词"), "含有米感词"
            )

    def test_group_interjections_use_same_ollama_and_exact_ten_second_cooldown(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.community_style import create_group_runtime
        from qq_openclaw_bridge.runtime.community_style_config import (
            CommunityStyleSettings, GroupBindingSettings, OllamaSettings,
        )

        settings = CommunityStyleSettings(
            True,
            OllamaSettings("http://127.0.0.1:11434", "qwen3:8b", False),
            (),
            (GroupBindingSettings(77, "profile", 5, 800),),
            "f" * 64,
        )
        reloader = type("Reloader", (), {
            "current": lambda self: settings,
            "reload_if_changed": lambda self: False,
        })()
        repository = object()
        store = type("Store", (), {"community_style_repository": repository})()
        rpc = type("Rpc", (), {
            "request": lambda *args, **kwargs: None,
            "consume": lambda self, payload: False,
            "fail_pending": lambda self: None,
        })()
        ollama = object()
        decider = object()
        decider_inputs = []
        now = [datetime(2026, 10, 2, tzinfo=timezone.utc)]

        runtime = create_group_runtime(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={1},
                bot_qq=999,
                enable_group_agent=True,
                enable_group_interjections=True,
                group_interjection_cooldown_seconds=10,
                style_agent_config_path="config.json",
            ),
            store,
            config_reloader_factory=lambda path: reloader,
            rpc_factory=lambda: rpc,
            ollama_factory=lambda configured, **kwargs: ollama,
            interjection_factory=lambda model: (
                decider_inputs.append(model) or decider
            ),
            coordinator_factory=lambda repo, loader, model, **kwargs: type(
                "Coordinator", (), {
                    "current_group_ids": lambda self: frozenset({77}),
                }
            )(),
            delivery_factory=lambda repo, client, **kwargs: object(),
            clock_factory=lambda: lambda: now[0],
        )

        self.assertIs(runtime.interjection_decider, decider)
        self.assertEqual(decider_inputs, [ollama])
        self.assertTrue(runtime.interjection_limiter.allow("group:77"))
        now[0] += timedelta(seconds=9)
        self.assertFalse(runtime.interjection_limiter.allow("group:77"))
        now[0] += timedelta(seconds=1)
        self.assertTrue(runtime.interjection_limiter.allow("group:77"))
        for _ in range(100):
            now[0] += timedelta(seconds=10)
            self.assertTrue(runtime.interjection_limiter.allow("group:77"))

    def test_private_kanagawave_adapter_reuses_configured_community_model(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.community_style import create_group_runtime
        from qq_openclaw_bridge.runtime.community_style_config import (
            CommunityStyleSettings, GroupBindingSettings, OllamaSettings,
        )

        settings = CommunityStyleSettings(
            True,
            OllamaSettings(
                "http://127.0.0.1:11434",
                "qwen3-4b-kanagawawave-v2:latest",
                False,
            ),
            (),
            (GroupBindingSettings(77, "profile", 5, 800),),
            "f" * 64,
        )
        reloader = type("Reloader", (), {
            "current": lambda self: settings,
            "reload_if_changed": lambda self: False,
        })()
        store = type("Store", (), {
            "community_style_repository": object(),
        })()
        rpc = type("Rpc", (), {
            "request": lambda *args, **kwargs: None,
            "consume": lambda self, payload: False,
            "fail_pending": lambda self: None,
        })()
        ollama = object()
        private_adapter = object()
        factory_calls = []

        runtime = create_group_runtime(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={20002},
                bot_qq=999,
                enable_group_agent=True,
                kanagawave_private_users={20002},
                style_agent_config_path="config.json",
            ),
            store,
            config_reloader_factory=lambda path: reloader,
            rpc_factory=lambda: rpc,
            ollama_factory=lambda configured, **kwargs: ollama,
            private_adapter_factory=lambda client, **kwargs: (
                factory_calls.append((client, kwargs)) or private_adapter
            ),
            coordinator_factory=lambda repo, loader, model: type(
                "Coordinator", (), {
                    "current_group_ids": lambda self: frozenset({77}),
                }
            )(),
            delivery_factory=lambda repo, client: object(),
        )

        self.assertIs(runtime.private_agent_adapter, private_adapter)
        self.assertEqual(factory_calls, [(
            ollama,
            {"model": "qwen3-4b-kanagawawave-v2:latest"},
        )])

    def test_build_application_injects_group_runtime_into_bridge(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        group_runtime = object()
        store = type("Store", (), {
            "action_intent_repository": None,
            "orchestration_repository": None,
        })()
        graph = build_application(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={1},
                bot_qq=999,
                enable_group_agent=True,
                enable_auto_backup=False,
            ),
            state_store_factory=lambda config: store,
            agent_factory=lambda config: object(),
            style_factory=lambda config, state_store=None: None,
            housekeeper_factory=lambda config, state_store=None: None,
            reminder_factory=lambda config, state_store=None: None,
            memory_factory=lambda config, state_store, housekeeper: None,
            backup_factory=lambda config: None,
            group_runtime_factory=lambda config, state_store: group_runtime,
            bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(kwargs),
        )

        self.assertIs(graph["group_runtime"], group_runtime)

    def test_direct_runtime_uses_explicit_config_version(self):
        from qq_openclaw_bridge.application.coordinator_factory import (
            create_direct_runtime,
        )

        runtime = create_direct_runtime(lambda request: None, config_version=7)

        self.assertEqual(runtime._config.version, 7)

    def test_shadow_routing_recovers_and_is_injected_only_when_enabled(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        class Observer:
            def __init__(self):
                self.recovery_calls = 0

            def recover(self):
                self.recovery_calls += 1
                return 1

        observer = Observer()
        store = type("Store", (), {
            "action_intent_repository": None,
            "orchestration_repository": None,
            "routing_repository": object(),
        })()

        with patch("qq_openclaw_bridge.runtime.composition.create_shadow_routing_runtime", return_value=observer) as create_runtime:
            graph = build_application(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users={1},
                    enable_shadow_routing=True,
                    enable_auto_backup=False,
                ),
                state_store_factory=lambda config: store,
                agent_factory=lambda config: object(),
                style_factory=lambda config, state_store=None: None,
                housekeeper_factory=lambda config, state_store=None: None,
                reminder_factory=lambda config, state_store=None: None,
                memory_factory=lambda config, state_store, housekeeper: None,
                backup_factory=lambda config: None,
                bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(kwargs),
            )

        create_runtime.assert_called_once()
        self.assertEqual(observer.recovery_calls, 1)
        self.assertIs(graph.message_coordinator._shadow_routing_observer, observer)

    def test_shadow_routing_recovery_failure_refuses_opted_in_startup(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        observer = type("Observer", (), {
            "recover": lambda self: (_ for _ in ()).throw(RuntimeError("sqlite down")),
        })()
        store = type("Store", (), {
            "action_intent_repository": None,
            "orchestration_repository": None,
            "routing_repository": object(),
        })()

        with patch(
            "qq_openclaw_bridge.runtime.composition.create_shadow_routing_runtime",
            return_value=observer,
        ), self.assertRaisesRegex(RuntimeError, "sqlite down"):
            build_application(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users={1},
                    enable_shadow_routing=True,
                    enable_auto_backup=False,
                ),
                state_store_factory=lambda config: store,
                agent_factory=lambda config: object(),
                style_factory=lambda config, state_store=None: None,
                housekeeper_factory=lambda config, state_store=None: None,
                reminder_factory=lambda config, state_store=None: None,
                memory_factory=lambda config, state_store, housekeeper: None,
                backup_factory=lambda config: None,
                bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(kwargs),
            )

    def test_disabled_composition_leaves_pending_routing_decision_untouched(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            pending = store.routing_repository.start_decision(
                user_id=1,
                source_message_sha256="a" * 64,
                message_sha256="b" * 64,
                message_preview="pending",
                routing_config_version=1,
                rule_version="f1-v1",
                dispatch_overhead_ms=1,
            )
            build_application(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users={1},
                    enable_security_confirmation=False,
                ),
                state_store_factory=lambda config: store,
                agent_factory=lambda config: object(),
                style_factory=lambda config, state_store=None: None,
                housekeeper_factory=lambda config, state_store=None: None,
                reminder_factory=lambda config, state_store=None: None,
                memory_factory=lambda config, state_store, housekeeper: None,
                backup_factory=lambda config: None,
                bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(kwargs),
            )

            self.assertEqual(
                store.routing_repository.recent_decisions(1)[0]["id"], pending["id"]
            )
            self.assertEqual(
                store.routing_repository.recent_decisions(1)[0]["status"], "pending"
            )

    def test_direct_graph_uses_active_orchestration_config_version(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.orchestration_repository.create_config_version(
                {"mode": "legacy_direct"}, activate=True
            )
            active = store.orchestration_repository.create_config_version(
                {"mode": "direct"}, activate=True
            )

            bridge = build_application(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users={1},
                    orchestration_mode="direct",
                    enable_auto_backup=False,
                ),
                state_store_factory=lambda config: store,
                agent_factory=lambda config: object(),
                style_factory=lambda config, state_store=None: None,
                housekeeper_factory=lambda config, state_store=None: None,
                reminder_factory=lambda config, state_store=None: None,
                memory_factory=lambda config, state_store, housekeeper: None,
                backup_factory=lambda config: None,
                approval_binding_key_provider_factory=(
                    _FakeApprovalBindingKeyProvider
                ),
                bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(
                    kwargs
                ),
            )

            runtime = bridge.message_coordinator.orchestration_runtime
            self.assertEqual(runtime._config.version, active["version"])
            self.assertEqual(
                bridge["confirmation_guard"].config_version,
                active["version"],
            )

    def test_direct_graph_creates_missing_active_config(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")

            build_application(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users={1},
                    orchestration_mode="direct",
                    enable_auto_backup=False,
                ),
                state_store_factory=lambda config: store,
                agent_factory=lambda config: object(),
                style_factory=lambda config, state_store=None: None,
                housekeeper_factory=lambda config, state_store=None: None,
                reminder_factory=lambda config, state_store=None: None,
                memory_factory=lambda config, state_store, housekeeper: None,
                backup_factory=lambda config: None,
                approval_binding_key_provider_factory=(
                    _FakeApprovalBindingKeyProvider
                ),
                bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(
                    kwargs
                ),
            )

            active = store.orchestration_repository.active_config()
            self.assertEqual(active["config"], {"mode": "direct"})

    def test_direct_graph_rejects_active_mode_mismatch(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.orchestration_repository.create_config_version(
                {"mode": "legacy_direct"}, activate=True
            )

            with self.assertRaisesRegex(RuntimeError, "does not match"):
                build_application(
                    Config(
                        onebot_ws_url="ws://127.0.0.1:3001",
                        allowed_qq_users={1},
                        orchestration_mode="direct",
                        enable_auto_backup=False,
                    ),
                    state_store_factory=lambda config: store,
                    agent_factory=lambda config: object(),
                    style_factory=lambda config, state_store=None: None,
                    housekeeper_factory=lambda config, state_store=None: None,
                    reminder_factory=lambda config, state_store=None: None,
                    memory_factory=lambda config, state_store, housekeeper: None,
                    backup_factory=lambda config: None,
                    approval_binding_key_provider_factory=(
                        _FakeApprovalBindingKeyProvider
                    ),
                    bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(
                        kwargs
                    ),
                )

    def test_legacy_graph_rejects_active_mode_mismatch(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.orchestration_repository.create_config_version(
                {"mode": "direct"}, activate=True
            )

            with self.assertRaisesRegex(RuntimeError, "does not match"):
                build_application(
                    Config(
                        onebot_ws_url="ws://127.0.0.1:3001",
                        allowed_qq_users={1},
                        orchestration_mode="legacy_direct",
                        enable_auto_backup=False,
                    ),
                    state_store_factory=lambda config: store,
                    agent_factory=lambda config: object(),
                    style_factory=lambda config, state_store=None: None,
                    housekeeper_factory=lambda config, state_store=None: None,
                    reminder_factory=lambda config, state_store=None: None,
                    memory_factory=lambda config, state_store, housekeeper: None,
                    backup_factory=lambda config: None,
                    approval_binding_key_provider_factory=(
                        _FakeApprovalBindingKeyProvider
                    ),
                    bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(
                        kwargs
                    ),
                )

    def test_direct_production_graph_injects_sqlite_trace_sink(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.models import AgentRequest, AgentResponse
        from qq_openclaw_bridge.runtime.composition import build_application

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "state.sqlite3"

            class Adapter:
                def reply(self, request):
                    return AgentResponse("已完成")

            async def execute_message():
                bridge = build_application(
                    Config(
                        onebot_ws_url="ws://127.0.0.1:3001",
                        allowed_qq_users={7},
                        orchestration_mode="direct",
                        state_database_path=str(database_path),
                        enable_auto_backup=False,
                    ),
                    agent_factory=lambda config: Adapter(),
                    style_factory=lambda config, state_store=None: None,
                    housekeeper_factory=lambda config, state_store=None: None,
                    reminder_factory=lambda config, state_store=None: None,
                    memory_factory=lambda config, state_store, housekeeper: None,
                    backup_factory=lambda config: None,
                    approval_binding_key_provider_factory=_FakeApprovalBindingKeyProvider,
                )
                request = AgentRequest(7, "m-trace", "执行", 10)
                resolution = await bridge.message_coordinator.resolve_prepared(
                    bridge.message_coordinator.prepare(request)
                )
                return bridge, resolution

            bridge, resolution = asyncio.run(execute_message())
            run = bridge.state_store.orchestration_repository.latest_run(7)
            self.assertEqual(resolution.reply, "已完成")
            self.assertEqual(run["status"], "accepted")

    def test_both_modes_execute_one_shared_agent_call(self):
        from qq_openclaw_bridge.bridge import Bridge
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.models import AgentRequest, AgentResponse
        from qq_openclaw_bridge.runtime.composition import build_application

        for mode in ("legacy_direct", "direct"):
            with self.subTest(mode=mode):
                calls = []

                class Adapter:
                    def reply(self, request):
                        calls.append(request)
                        return AgentResponse("模型回复")

                class SpyBridge(Bridge):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.agent_funnel_calls = 0

                    async def _call_agent(self, request):
                        self.agent_funnel_calls += 1
                        return await super()._call_agent(request)

                async def execute_message():
                    bridge = build_application(
                        Config(
                            onebot_ws_url="ws://127.0.0.1:3001",
                            allowed_qq_users={1},
                            orchestration_mode=mode,
                            enable_sqlite_store=False,
                            enable_auto_backup=False,
                        ),
                        state_store_factory=lambda config: None,
                        agent_factory=lambda config: Adapter(),
                        style_factory=lambda config, state_store=None: None,
                        housekeeper_factory=lambda config, state_store=None: None,
                        reminder_factory=lambda config, state_store=None: None,
                        memory_factory=lambda config, state_store, housekeeper: None,
                        backup_factory=lambda config: None,
                        bridge_factory=SpyBridge,
                    )
                    request = AgentRequest(1, "m1", "你好", 10)
                    resolution = await bridge.message_coordinator.resolve_prepared(
                        bridge.message_coordinator.prepare(request)
                    )
                    return bridge, resolution

                bridge, resolution = asyncio.run(execute_message())
                self.assertEqual(resolution.reply, "模型回复")
                self.assertEqual(len(calls), 1)
                self.assertEqual(bridge.agent_funnel_calls, 1)

    def test_custom_bridge_without_agent_call_fails_during_composition(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        with self.assertRaisesRegex(RuntimeError, "_call_agent"):
            build_application(
                Config(
                    onebot_ws_url="ws://127.0.0.1:3001",
                    allowed_qq_users={1},
                ),
                state_store_factory=lambda config: None,
                agent_factory=lambda config: object(),
                style_factory=lambda config, state_store=None: None,
                housekeeper_factory=lambda config, state_store=None: None,
                reminder_factory=lambda config, state_store=None: None,
                memory_factory=lambda config, state_store, housekeeper: None,
                backup_factory=lambda config: None,
                bridge_factory=lambda *args, **kwargs: object(),
            )

    def test_agent_call_proxy_rejects_duplicate_binding(self):
        from qq_openclaw_bridge.application.coordinator_factory import AgentCallProxy

        async def target(request):
            return None

        proxy = AgentCallProxy()
        proxy.bind(target)
        with self.assertRaisesRegex(RuntimeError, "already bound"):
            proxy.bind(target)

    def test_direct_mode_injects_runtime_and_creates_agent_once(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        agent_calls = []

        class BridgeFactory:
            def __call__(self, config, adapter, **kwargs):
                return _CapturedBridge(kwargs)

        graph = build_application(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={1},
                orchestration_mode="direct",
                enable_sqlite_store=False,
            ),
            state_store_factory=lambda config: None,
            agent_factory=lambda config: agent_calls.append(config) or object(),
            style_factory=lambda config, state_store=None: None,
            housekeeper_factory=lambda config, state_store=None: None,
            reminder_factory=lambda config, state_store=None: None,
            memory_factory=lambda config, state_store, housekeeper: None,
            backup_factory=lambda config: None,
            bridge_factory=BridgeFactory(),
        )
        self.assertIsNotNone(graph.message_coordinator.orchestration_runtime)
        self.assertEqual(len(agent_calls), 1)

    def test_legacy_direct_mode_injects_compatibility_coordinator(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        class BridgeFactory:
            def __call__(self, config, adapter, **kwargs):
                return _CapturedBridge(kwargs)

        graph = build_application(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={1},
                orchestration_mode="legacy_direct",
                enable_sqlite_store=False,
            ),
            state_store_factory=lambda config: None,
            agent_factory=lambda config: object(),
            style_factory=lambda config, state_store=None: None,
            housekeeper_factory=lambda config, state_store=None: None,
            reminder_factory=lambda config, state_store=None: None,
            memory_factory=lambda config, state_store, housekeeper: None,
            backup_factory=lambda config: None,
            bridge_factory=BridgeFactory(),
        )
        self.assertIsNone(graph.message_coordinator.orchestration_runtime)

    def test_one_shared_state_store_is_injected_everywhere(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        shared = object()
        seen = []

        def component(name):
            def create(config, state_store=None):
                seen.append((name, state_store))
                return name

            return create

        class Guard:
            def __init__(
                self,
                timeout_seconds,
                approval_store,
                binding_key,
                config_version,
            ):
                self.binding_key = binding_key
                seen.append(("security", approval_store))

        class BridgeFactory:
            def __call__(self, config, adapter, **kwargs):
                seen.append(("bridge", kwargs["state_store"]))
                return _CapturedBridge(kwargs)

        build_application(
            Config(onebot_ws_url="ws://127.0.0.1:3001", allowed_qq_users={1}),
            state_store_factory=lambda config: shared,
            agent_factory=lambda config: "agent",
            style_factory=component("style"),
            housekeeper_factory=component("housekeeper"),
            reminder_factory=component("reminder"),
            confirmation_guard_factory=Guard,
            backup_factory=lambda config: "backup",
            bridge_factory=BridgeFactory(),
        )

        self.assertTrue(seen)
        self.assertTrue(all(value is shared for _, value in seen))

    def test_persistent_security_receives_explicit_binding_key_and_recovers_actions(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        class Repository:
            def __init__(self):
                self.recovery_calls = 0

            def recover_executing_as_unknown(self):
                self.recovery_calls += 1
                return 0

        repository = Repository()

        class Store:
            action_intent_repository = repository

        captured = []

        class Guard:
            def __init__(
                self,
                timeout_seconds,
                approval_store,
                binding_key,
                config_version,
            ):
                captured.append((approval_store, binding_key, config_version))

        class Provider:
            def get(self):
                return b"p" * 32

        build_application(
            Config(onebot_ws_url="ws://127.0.0.1:3001", allowed_qq_users={1}),
            state_store_factory=lambda config: Store(),
            agent_factory=lambda config: "agent",
            style_factory=lambda config, state_store=None: None,
            housekeeper_factory=lambda config, state_store=None: None,
            reminder_factory=lambda config, state_store=None: None,
            memory_factory=lambda config, state_store, housekeeper: None,
            confirmation_guard_factory=Guard,
            approval_binding_key_provider_factory=Provider,
            backup_factory=lambda config: None,
            bridge_factory=lambda config, adapter, **kwargs: _CapturedBridge(kwargs),
        )

        self.assertEqual(captured, [(captured[0][0], b"p" * 32, 1)])
        self.assertEqual(repository.recovery_calls, 1)

    def test_memory_runtime_receives_state_store_and_housekeeper(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        shared = object()
        captured = []

        class BridgeFactory:
            def __call__(self, config, adapter, **kwargs):
                return _CapturedBridge(kwargs)

        result = build_application(
            Config(onebot_ws_url="ws://127.0.0.1:3001", allowed_qq_users={1}),
            state_store_factory=lambda config: shared,
            agent_factory=lambda config: "agent",
            style_factory=lambda config, state_store=None: "style",
            housekeeper_factory=lambda config, state_store=None: "housekeeper",
            reminder_factory=lambda config, state_store=None: "reminder",
            memory_factory=lambda config, state_store, housekeeper: captured.append(
                (state_store, housekeeper)
            )
            or "memory-runtime",
            backup_factory=lambda config: "backup",
            bridge_factory=BridgeFactory(),
        )

        self.assertEqual(captured, [(shared, "housekeeper")])
        self.assertEqual(result["memory_runtime"], "memory-runtime")

    def test_default_graph_builds_e3_memory_runtime_on_sqlite(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            workspace = root / "workspace"
            config = Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={1},
                enable_auto_backup=False,
                state_database_path=str(workspace / "chichu.sqlite3"),
                housekeeper_memory_path=str(workspace / "MEMORY.md"),
                housekeeper_todo_path=str(workspace / "TODO.md"),
                style_memory_path=str(workspace / "STYLE.md"),
                reminder_state_path=str(workspace / "REMINDER_STATE.md"),
            )

            async def verify_graph():
                bridge = build_application(
                    config,
                    approval_binding_key_provider_factory=_FakeApprovalBindingKeyProvider,
                )
                self.assertIsNotNone(bridge.memory_runtime)
                self.assertEqual(
                    bridge.state_store.memory_repository.stats(1)["confirmed"], 0
                )

            asyncio.run(verify_graph())

    def test_disabled_e4_passes_none_without_constructing_media_runtime(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        class BridgeFactory:
            def __call__(self, config, adapter, **kwargs):
                return _CapturedBridge(kwargs)

        result = build_application(
            Config(onebot_ws_url="ws://127.0.0.1:3001", allowed_qq_users={1}),
            state_store_factory=lambda config: object(),
            agent_factory=lambda config: "agent",
            style_factory=lambda config, state_store=None: "style",
            housekeeper_factory=lambda config, state_store=None: "housekeeper",
            reminder_factory=lambda config, state_store=None: "reminder",
            memory_factory=lambda config, state_store, housekeeper: "memory",
            backup_factory=lambda config: "backup",
            media_factory=lambda config, state_store: (_ for _ in ()).throw(
                AssertionError("disabled E4 must not construct media runtime")
            ),
            bridge_factory=BridgeFactory(),
        )

        self.assertIsNone(result["media_runtime"])

    def test_enabled_e4_requires_sqlite_state_store(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.composition import build_application

        config = Config(
            onebot_ws_url="ws://127.0.0.1:3001",
            allowed_qq_users={1},
            enable_sqlite_store=False,
            enable_long_tasks=True,
        )
        with self.assertRaisesRegex(RuntimeError, "SQLite"):
            build_application(
                config,
                state_store_factory=lambda config: None,
                agent_factory=lambda config: "agent",
                style_factory=lambda config, state_store=None: "style",
                housekeeper_factory=lambda config, state_store=None: "housekeeper",
                reminder_factory=lambda config, state_store=None: "reminder",
                memory_factory=lambda config, state_store, housekeeper: "memory",
                backup_factory=lambda config: "backup",
                bridge_factory=lambda *args, **kwargs: kwargs,
            )

    def test_media_runtime_uses_one_repository_secret_and_rpc_client(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.media import create_media_runtime

        repository = object()

        class Store:
            e4_repository = repository

        seen = []

        class Resolver:
            def resolve(self, reference, fallback):
                seen.append(("secret", reference, fallback))
                return "resolved-secret"

        def factory(name, result):
            def create(*args, **kwargs):
                seen.append((name, args, kwargs))
                return result

            return create

        rpc = object()
        quote = object()
        ingestion = object()
        extractor = object()
        budget = object()
        api = object()
        pipeline = object()
        task_service = object()
        task_commands = object()
        config = Config(
            onebot_ws_url="ws://127.0.0.1:3001",
            allowed_qq_users={1},
            enable_media_pipeline=True,
            enable_long_tasks=True,
            media_cache_dir="~/task-7-cache",
            media_api_base_url="https://media.invalid",
            media_api_secret_ref="keychain:service:account",
            media_image_model="image-model",
            media_audio_model="audio-model",
            media_remote_allowed_hosts=("media.invalid",),
        )
        with patch.dict(os.environ, {"MEDIA_API_KEY": "env-secret"}):
            runtime = create_media_runtime(
                config,
                Store(),
                secret_resolver_factory=lambda: Resolver(),
                rpc_client_factory=lambda: rpc,
                quote_resolver_factory=factory("quote", quote),
                ingestion_factory=factory("ingestion", ingestion),
                document_extractor_factory=factory("extractor", extractor),
                budget_policy_factory=factory("budget", budget),
                media_api_factory=factory("api", api),
                pipeline_factory=factory("pipeline", pipeline),
                task_service_factory=factory("task_service", task_service),
                task_commands_factory=factory("task_commands", task_commands),
            )

        self.assertIs(runtime.pipeline, pipeline)
        self.assertIs(runtime.quote_resolver, quote)
        self.assertIs(runtime.task_service, task_service)
        self.assertIs(runtime.task_commands, task_commands)
        self.assertIs(runtime.rpc_client, rpc)
        self.assertEqual(seen[0], (
            "secret", "keychain:service:account", "env-secret"
        ))
        repository_uses = sum(
            1 for item in seen[1:]
            if repository in item[1] or repository in item[2].values()
        )
        self.assertGreaterEqual(repository_uses, 4)
        self.assertNotIn("resolved-secret", repr(runtime))

    def test_long_tasks_only_never_resolve_secret_or_build_media_components(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.runtime.media import create_media_runtime

        repository = object()
        store = type("Store", (), {"e4_repository": repository})()
        task_service = object()
        runtime = create_media_runtime(
            Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users={1},
                enable_long_tasks=True,
            ),
            store,
            secret_resolver_factory=lambda: (_ for _ in ()).throw(
                AssertionError("long tasks must not resolve media secrets")
            ),
            ingestion_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("long tasks must not create media cache")
            ),
            media_api_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("long tasks must not create media API")
            ),
            rpc_client_factory=lambda: "rpc",
            quote_resolver_factory=lambda rpc: "quote",
            task_service_factory=lambda repo, **kwargs: task_service,
            task_commands_factory=lambda repo, service: "commands",
        )

        self.assertIsNone(runtime.pipeline)
        self.assertIs(runtime.task_service, task_service)

    def test_default_media_factories_process_real_file_from_trusted_local_root(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.domain.inbound import AttachmentRef, InboundEnvelope
        from qq_openclaw_bridge.runtime.media import create_media_runtime
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        class Result:
            text = "a tiny local image"

        class FakeMediaApi:
            def describe_image(self, media, prompt):
                self.seen = (media, prompt)
                return Result()

            def transcribe_audio(self, media):
                raise AssertionError("audio API is not expected")

        class Cancellation:
            def raise_if_cancelled(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            trusted = root / "napcat"
            trusted.mkdir()
            image_path = trusted / "tiny.png"
            image_path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\rIHDR"
                b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
                b"\x90wS\xde"
                b"\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00"
                b"\x18\xdd\x8d\xb4"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            store = WorkspaceStateStore(root / "state.sqlite3")
            api = FakeMediaApi()
            envelope = InboundEnvelope(
                user_id=1,
                message_id="local-media-1",
                timestamp=1.0,
                attachments=(
                    AttachmentRef(
                        kind="image",
                        file_id="tiny.png",
                        url="https://not-allowed.invalid/tiny.png",
                        name="tiny.png",
                        declared_mime="image/png",
                    ),
                ),
            )

            async def process_media():
                runtime = create_media_runtime(
                    Config(
                        onebot_ws_url="ws://127.0.0.1:3001",
                        allowed_qq_users={1},
                        enable_media_pipeline=True,
                        media_cache_dir=str(root / "cache"),
                        napcat_media_roots=(str(trusted),),
                        media_api_base_url="https://media.invalid",
                        media_api_secret_ref="test-only",
                        media_image_model="image-model",
                        media_audio_model="audio-model",
                    ),
                    store,
                    secret_resolver_factory=lambda: type(
                        "Resolver", (), {"resolve": lambda self, ref, fallback: "test-key"}
                    )(),
                    media_api_factory=lambda **kwargs: api,
                )
                return await runtime.pipeline.process(
                    envelope, None, Cancellation()
                )

            artifacts = asyncio.run(process_media())

        self.assertEqual(artifacts[0].status, "ready")
        self.assertEqual(artifacts[0].content, "a tiny local image")

    def test_media_composition_rejects_symlinked_cache_and_napcat_roots(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_bridge.domain.media import MediaRejected
        from qq_openclaw_bridge.runtime.media import create_media_runtime
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir).resolve()
            real_cache = base / "real-cache"
            real_napcat = base / "real-napcat"
            real_cache.mkdir()
            real_napcat.mkdir()
            cache_link = base / "cache-link"
            napcat_link = base / "napcat-link"
            cache_link.symlink_to(real_cache, target_is_directory=True)
            napcat_link.symlink_to(real_napcat, target_is_directory=True)
            store = WorkspaceStateStore(base / "state.sqlite3")

            for label, cache_root, napcat_root in (
                ("cache root", cache_link, real_napcat),
                ("NapCat root", real_cache, napcat_link),
            ):
                with self.subTest(label=label):
                    config = Config(
                        onebot_ws_url="ws://127.0.0.1:3001",
                        allowed_qq_users={1},
                        enable_media_pipeline=True,
                        media_cache_dir=str(cache_root),
                        napcat_media_roots=(str(napcat_root),),
                        media_api_secret_ref="test-only",
                    )
                    with self.assertRaisesRegex(MediaRejected, "symbolic link"):
                        create_media_runtime(
                            config,
                            store,
                            secret_resolver_factory=lambda: type(
                                "Resolver", (), {
                                    "resolve": lambda self, ref, fallback: "test-key"
                                }
                            )(),
                            media_api_factory=lambda **kwargs: object(),
                            pipeline_factory=lambda *args, **kwargs: object(),
                        )


if __name__ == "__main__":
    unittest.main()
