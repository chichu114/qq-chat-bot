import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _AttemptRepository:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def record_model_call(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class _AttemptStore:
    def __init__(self, repository):
        self.e4_repository = repository
        self.metrics = {}

    def set_runtime_metric(self, name, value):
        self.metrics[name] = value


class BridgeObservabilityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _config(**overrides):
        from qq_openclaw_bridge.config import Config

        values = {
            "onebot_ws_url": "ws://127.0.0.1:3001",
            "allowed_qq_users": {10001},
            "agent_retry_attempts": 2,
        }
        values.update(overrides)
        return Config(**values)

    @staticmethod
    def _request():
        from qq_openclaw_bridge.models import AgentRequest

        return AgentRequest(10001, "m-observe", "hello", 1.0, task_id="task-1")

    async def test_each_executed_main_model_attempt_is_recorded(self):
        from qq_openclaw_bridge.bridge import Bridge

        class RetryingAdapter:
            def __init__(self):
                self.attempt = 0

            def reply(self, request):
                self.attempt += 1
                if self.attempt == 1:
                    raise TimeoutError("temporary timeout with secret detail")
                return SimpleNamespace(
                    text="ok",
                    provider="openclaw",
                    model="gpt-test",
                    input_tokens=120,
                    output_tokens=30,
                    duration_ms=450,
                )

        repository = _AttemptRepository()
        bridge = Bridge(
            self._config(),
            RetryingAdapter(),
            state_store=_AttemptStore(repository),
        )

        response = await bridge._call_agent(self._request())

        self.assertEqual(response.text, "ok")
        self.assertEqual(len(repository.calls), 2)
        self.assertEqual(repository.calls[0]["call_kind"], "chat")
        self.assertEqual(repository.calls[0]["user_id"], 10001)
        self.assertEqual(repository.calls[0]["task_id"], "task-1")
        self.assertEqual(repository.calls[0]["error_class"], "TimeoutError")
        self.assertEqual(repository.calls[0]["provider"], "unknown")
        self.assertEqual(repository.calls[0]["model"], "unknown")
        self.assertFalse(repository.calls[0]["success"])
        self.assertEqual(repository.calls[1]["provider"], "openclaw")
        self.assertEqual(repository.calls[1]["model"], "gpt-test")
        self.assertEqual(repository.calls[1]["input_tokens"], 120)
        self.assertEqual(repository.calls[1]["output_tokens"], 30)
        self.assertEqual(repository.calls[1]["duration_ms"], 450)
        self.assertTrue(repository.calls[1]["success"])
        self.assertNotIn("secret detail", json.dumps(repository.calls))

    async def test_metrics_failure_never_replaces_agent_result_or_error(self):
        from qq_openclaw_bridge.bridge import Bridge

        class SuccessAdapter:
            def reply(self, request):
                return SimpleNamespace(
                    text="ok",
                    provider="openclaw",
                    model="main",
                    input_tokens=None,
                    output_tokens=None,
                    duration_ms=None,
                )

        class FailureAdapter:
            def reply(self, request):
                raise ValueError("original failure")

        failing_store = _AttemptStore(
            _AttemptRepository(RuntimeError("metrics unavailable"))
        )
        success = await Bridge(
            self._config(agent_retry_attempts=1),
            SuccessAdapter(),
            state_store=failing_store,
        )._call_agent(self._request())
        self.assertEqual(success.text, "ok")

        bridge = Bridge(
            self._config(agent_retry_attempts=1),
            FailureAdapter(),
            state_store=failing_store,
        )
        with self.assertRaisesRegex(ValueError, "original failure"):
            await bridge._call_agent(self._request())
        self.assertEqual(len(failing_store.e4_repository.calls), 2)

    async def test_open_circuit_records_no_model_attempt(self):
        import time

        from qq_openclaw_bridge.bridge import Bridge

        class UnexpectedAdapter:
            def reply(self, request):
                raise AssertionError("adapter must not run while circuit is open")

        repository = _AttemptRepository()
        bridge = Bridge(
            self._config(),
            UnexpectedAdapter(),
            state_store=_AttemptStore(repository),
        )
        bridge._circuit_open_until = time.monotonic() + 60

        with self.assertRaisesRegex(RuntimeError, "circuit breaker"):
            await bridge._call_agent(self._request())

        self.assertEqual(repository.calls, [])


class RepositoryObservabilityTests(unittest.TestCase):
    def test_list_tasks_normalizes_limit_before_bounded_sql_read(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            repository = store.e4_repository
            created_at = "2026-07-30T12:00:00+00:00"
            with repository._database.connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO long_tasks(
                      id, user_id, source_message_id, kind, status, stage,
                      cancel_requested, created_at
                    ) VALUES (?, 1, ?, 'chat', 'queued', 'queued', 0, ?)
                    """,
                    [
                        (
                            "task-%03d" % index,
                            "message-%03d" % index,
                            created_at,
                        )
                        for index in range(101)
                    ],
                )

            statements = []
            original_connect = repository._database.connect

            def traced_connect():
                connection = original_connect()
                connection.set_trace_callback(statements.append)
                return connection

            repository._database.connect = traced_connect
            cases = (
                (100, 100),
                (101, 100),
                (10**100, 100),
                (True, 1),
                (None, 1),
                ("50", 1),
                (50.5, 1),
                (0, 1),
                (-50, 1),
            )
            ordered_ids = ["task-%03d" % index for index in range(100, -1, -1)]

            for supplied, expected in cases:
                with self.subTest(limit=supplied):
                    tasks = repository.list_tasks(limit=supplied)
                    query = next(
                        statement
                        for statement in reversed(statements)
                        if "FROM long_tasks" in statement and "LIMIT" in statement
                    )
                    self.assertEqual(len(tasks), expected)
                    self.assertEqual(
                        [task["id"] for task in tasks], ordered_ids[:expected]
                    )
                    self.assertIn("LIMIT %d" % expected, query)

    def test_health_timeline_normalizes_limit_before_bounded_sql_read(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            repository = store.e4_repository
            created_at = "2026-07-30T12:00:00+00:00"
            with repository._database.connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO health_events(component, status, detail, created_at)
                    VALUES ('media_api', ?, NULL, ?)
                    """,
                    [
                        ("healthy" if index % 2 else "degraded", created_at)
                        for index in range(201)
                    ],
                )

            statements = []
            original_connect = repository._database.connect

            def traced_connect():
                connection = original_connect()
                connection.set_trace_callback(statements.append)
                return connection

            repository._database.connect = traced_connect
            cases = (
                (200, 200),
                (201, 200),
                (10**100, 200),
                (False, 1),
                (None, 1),
                ("50", 1),
                (50.5, 1),
                (0, 1),
                (-50, 1),
            )
            ordered_ids = list(range(201, 0, -1))

            for supplied, expected in cases:
                with self.subTest(limit=supplied):
                    events = repository.health_timeline(limit=supplied)
                    query = next(
                        statement
                        for statement in reversed(statements)
                        if "FROM health_events" in statement and "LIMIT" in statement
                    )
                    self.assertEqual(len(events), expected)
                    self.assertEqual(
                        [event["id"] for event in events], ordered_ids[:expected]
                    )
                    self.assertIn("LIMIT %d" % expected, query)

    def test_model_stats_use_utc_window_and_nearest_rank_percentiles(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            repository = store.e4_repository
            now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
            for duration in (100, 200, 300):
                repository.record_model_call(
                    "chat",
                    "openclaw",
                    "gpt-test",
                    True,
                    input_tokens=10,
                    output_tokens=2,
                    duration_ms=duration,
                    now=now - timedelta(hours=1),
                )
            repository.record_model_call(
                "chat",
                "openclaw",
                "gpt-test",
                False,
                duration_ms=9999,
                error_class="TimeoutError",
                now=now - timedelta(hours=25),
            )

            stats = repository.model_stats(since=now - timedelta(hours=24))[0]

            self.assertEqual(stats["calls"], 3)
            self.assertEqual(stats["p50_latency_ms"], 200)
            self.assertEqual(stats["p95_latency_ms"], 300)
            self.assertEqual(stats["error_rate"], 0.0)
            self.assertEqual(stats["input_tokens"], 30)

    def test_recent_approvals_are_ordered_clamped_and_exposed_by_store(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = WorkspaceStateStore(Path(temp_dir) / "state.sqlite3")
            store.save_approval("PUBLIC-1", 1, "secret command 1", "first", "later", "2026-07-30T10:00:00+00:00")
            store.save_approval("PUBLIC-2", 2, "secret command 2", "second", "later", "2026-07-30T11:00:00+00:00")

            recent = store.list_recent_approvals(limit=0)

            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0]["approval_id"], "PUBLIC-2")
            self.assertEqual(recent[0]["command"], "secret command 2")


if __name__ == "__main__":
    unittest.main()
