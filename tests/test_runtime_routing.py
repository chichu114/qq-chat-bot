from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ShadowRoutingRuntimeTests(unittest.TestCase):
    def setUp(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = WorkspaceStateStore(Path(self.temp_dir.name) / "state.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def config(self, enabled):
        return SimpleNamespace(
            enable_shadow_routing=enabled,
            openclaw_command="openclaw",
            openclaw_use_gateway=True,
            routing_agent_id="routing",
            routing_classifier_transport="openclaw",
        )

    def test_runtime_selects_openclaw_classifier_transport_by_default(self):
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        created = []

        class Adapter:
            def __init__(self, **kwargs):
                created.append(kwargs)

        runtime = create_shadow_routing_runtime(
            self.config(True), self.store, adapter_factory=Adapter
        )
        adapter = runtime._classifier._adapter_factory({
            "classifier_timeout_seconds": 15,
            "classifier_model": "openai/gpt-5.4-nano",
        })

        self.assertIsInstance(adapter, Adapter)
        self.assertEqual(created, [{
            "command": "openclaw",
            "timeout_seconds": 15,
            "model": "openai/gpt-5.4-nano",
            "agent_id": "routing",
            "session_prefix": "agent:routing:qq",
            "use_gateway": True,
            "cancellable": True,
        }])

    def test_runtime_selects_responses_api_classifier_transport(self):
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        created = []

        class Adapter:
            def __init__(self, **kwargs):
                created.append(kwargs)

        config = self.config(True)
        config.routing_classifier_transport = "responses_api"
        runtime = create_shadow_routing_runtime(
            config, self.store, responses_adapter_factory=Adapter
        )
        adapter = runtime._classifier._adapter_factory({
            "classifier_timeout_seconds": 15,
            "classifier_model": "openai/gpt-5.4-nano",
        })

        self.assertIsInstance(adapter, Adapter)
        self.assertEqual(created, [{
            "timeout_seconds": 15,
            "model": "openai/gpt-5.4-nano",
            "routing_agent_id": "routing",
            "cancellable": True,
        }])

    def test_runtime_is_absent_when_feature_is_disabled_or_sqlite_unavailable(self):
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        self.assertIsNone(create_shadow_routing_runtime(self.config(False), self.store))
        self.assertIsNone(create_shadow_routing_runtime(self.config(True), None))

    def test_runtime_creates_conservative_active_config(self):
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        runtime = create_shadow_routing_runtime(self.config(True), self.store)

        self.assertIsNotNone(runtime)
        active = self.store.routing_repository.active_config()
        self.assertEqual(
            active["config"],
            {
                "enabled": True,
                "classifier_enabled": False,
                "classifier_model": "openai/gpt-5.4-nano",
                "confidence_threshold_percent": 75,
                "classifier_timeout_seconds": 15,
                "max_daily_calls": 100,
                "failure_threshold": 3,
                "cooldown_seconds": 300,
                "rule_version": "f1-v1",
            },
        )

    def test_bootstrap_reuses_an_existing_valid_active_config(self):
        from qq_openclaw_bridge.runtime.routing import (
            bootstrap_shadow_routing_config,
        )

        expected = {
            "enabled": True,
            "classifier_enabled": False,
            "classifier_model": "openai/gpt-5.4-nano",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 15,
            "max_daily_calls": 100,
            "failure_threshold": 3,
            "cooldown_seconds": 300,
            "rule_version": "f1-v1",
        }
        created = self.store.routing_repository.create_config(
            expected, activate=True
        )

        first = bootstrap_shadow_routing_config(self.store)
        second = bootstrap_shadow_routing_config(self.store)
        with self.store._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM routing_config_versions"
            ).fetchone()[0]

        self.assertEqual(first["version"], created["version"])
        self.assertEqual(second["version"], created["version"])
        self.assertEqual(first["config"], expected)
        self.assertEqual(count, 1)

    def test_opted_in_runtime_rejects_wrong_type_in_active_config(self):
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        invalid = {
            "enabled": True,
            "classifier_enabled": "false",
            "classifier_model": "openai/gpt-5.4-nano",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 15,
            "max_daily_calls": 100,
            "failure_threshold": 3,
            "cooldown_seconds": 300,
            "rule_version": "f1-v1",
        }
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO routing_config_versions("
                "config_json, config_sha256, status, created_at, activated_at"
                ") VALUES (?, ?, 'active', ?, ?)",
                (
                    json.dumps(invalid),
                    "0" * 64,
                    "2026-08-21T00:00:00+00:00",
                    "2026-08-21T00:00:00+00:00",
                ),
            )

        with self.assertRaisesRegex(ValueError, "classifier_enabled must be boolean"):
            create_shadow_routing_runtime(self.config(True), self.store)

    def test_opted_in_runtime_rejects_unbound_active_config_digest(self):
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        valid = {
            "enabled": True,
            "classifier_enabled": False,
            "classifier_model": "openai/gpt-5.4-nano",
            "confidence_threshold_percent": 75,
            "classifier_timeout_seconds": 15,
            "max_daily_calls": 100,
            "failure_threshold": 3,
            "cooldown_seconds": 300,
            "rule_version": "f1-v1",
        }

        class Repository:
            def __init__(self, digest):
                self.digest = digest

            def active_config(self):
                return {"config": valid, "config_sha256": self.digest}

        cases = (None, "bad", "A" * 64, "0" * 64)
        for digest in cases:
            with self.subTest(digest=digest):
                state_store = SimpleNamespace(
                    routing_repository=Repository(digest)
                )
                with self.assertRaises(ValueError):
                    create_shadow_routing_runtime(self.config(True), state_store)

    def test_opted_in_runtime_rejects_malformed_active_json(self):
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO routing_config_versions("
                "config_json, config_sha256, status, created_at, activated_at"
                ") VALUES ('{', ?, 'active', ?, ?)",
                (
                    "0" * 64,
                    "2026-08-21T00:00:00+00:00",
                    "2026-08-21T00:00:00+00:00",
                ),
            )

        with self.assertRaises(json.JSONDecodeError):
            create_shadow_routing_runtime(self.config(True), self.store)

    def test_runtime_builds_fresh_adapter_from_live_routing_config(self):
        from qq_openclaw_bridge.models import AgentResponse
        from qq_openclaw_bridge.runtime.routing import create_shadow_routing_runtime

        created = []

        class Adapter:
            def reply(self, request):
                return AgentResponse(
                    '{"level":"L1","confidence":0.9,"task_kind":"smalltalk",'
                    '"reasons":[],"brainstorm_required":false}',
                    provider="openai", model="gpt-5.4-nano", duration_ms=1,
                )

        def adapter_factory(**kwargs):
            created.append(kwargs)
            return Adapter()

        runtime = create_shadow_routing_runtime(
            self.config(True), self.store, adapter_factory=adapter_factory
        )
        routing_config = dict(self.store.routing_repository.active_config()["config"])
        routing_config.update(
            classifier_enabled=True,
            classifier_model="openai/live-model",
            classifier_timeout_seconds=31,
            rule_version="f1-v2",
        )
        self.store.routing_repository.create_config(routing_config, activate=True)
        from qq_openclaw_bridge.models import AgentRequest

        import asyncio

        async def observe():
            runtime.schedule(
                AgentRequest(1, "live-1", "分析", 1.0), local_handled=False
            )
            self.assertEqual(len(created), 0)
            await runtime.wait()

        asyncio.run(observe())
        self.assertEqual(created[0]["model"], "openai/live-model")
        self.assertEqual(created[0]["timeout_seconds"], 31)
        self.assertTrue(created[0]["cancellable"])


if __name__ == "__main__":
    unittest.main()
