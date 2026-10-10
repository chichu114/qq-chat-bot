from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


ROUTING_FIELDS = {
    "classifier_enabled",
    "classifier_model",
    "confidence_threshold_percent",
    "classifier_timeout_seconds",
    "max_daily_calls",
    "failure_threshold",
    "cooldown_seconds",
    "rule_version",
}


def routing_payload(**overrides):
    payload = {
        "classifier_enabled": True,
        "classifier_model": "openai/gpt-5.4-nano",
        "confidence_threshold_percent": 75,
        "classifier_timeout_seconds": 15,
        "max_daily_calls": 100,
        "failure_threshold": 3,
        "cooldown_seconds": 300,
        "rule_version": "f1-v1",
    }
    payload.update(overrides)
    return payload


class _Backups:
    interval_hours = 24
    retention_days = 30
    max_auto_backups = 30

    @staticmethod
    def list_backups():
        return []


class DashboardRoutingQueryTests(unittest.TestCase):
    def test_routing_state_is_a_safe_projection_with_independent_gate_checks(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_dashboard.queries import DashboardQueryService

        now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            workspace = project / "workspace"
            workspace.mkdir()
            (project / "logs").mkdir()
            store = WorkspaceStateStore(workspace / "state.sqlite3")
            active = store.routing_repository.create_config(
                dict(routing_payload(), enabled=True), activate=True, now=now
            )
            decision = store.routing_repository.start_decision(
                user_id=10001,
                source_message_sha256="a" * 64,
                message_sha256="b" * 64,
                message_preview=(
                    "Bearer CLASSIFIER-RAW-TOKEN api_key=TOKEN-SECRET "
                    "https://example.invalid/?query=QUERY-SECRET "
                    "12345678901234567890"
                ),
                routing_config_version=active["version"],
                rule_version="f1-v1",
                dispatch_overhead_ms=1600,
                now=now - timedelta(days=6),
            )
            decision = store.routing_repository.finish_decision(
                decision["id"],
                decision["updated_at"],
                status="classified",
                rule_level="L2",
                model_level="L2",
                final_level="L2",
                confidence=0.91,
                task_kind="analysis",
                risk_flags=("credentials",),
                reasons=("CLASSIFIER-RAW-TEXT",),
                brainstorm_required=False,
                brainstorm_mode="standard",
                classifier_provider="openai",
                classifier_model="gpt-5.4-nano",
                classifier_duration_ms=123,
                now=now - timedelta(days=6),
            )
            store.routing_repository.label(
                decision["id"], 10001, "correct", "L2", "", decision["updated_at"]
            )
            service = DashboardQueryService(
                project,
                workspace / "MEMORY.md",
                workspace / "TODO.md",
                workspace / "STYLE.md",
                workspace / "REMINDER_STATE.md",
                store,
                _Backups(),
                lambda: now,
                onebot_probe=lambda: False,
                launch_agent_probe=lambda label: (False, None),
            )

            routing = service.get_state()["routing"]
            serialized = json.dumps(routing, ensure_ascii=False)

            self.assertEqual(routing["config_version"], active["version"])
            self.assertEqual(set(routing["config"]), ROUTING_FIELDS)
            self.assertEqual(
                set(routing["metrics"]["confusion_matrix"]), {"L0", "L1", "L2", "L3"}
            )
            self.assertTrue(
                all(
                    set(row) == {"L0", "L1", "L2", "L3"}
                    for row in routing["metrics"]["confusion_matrix"].values()
                )
            )
            self.assertEqual(routing["recent_decisions"][0]["id"], decision["id"])
            public_decision = routing["recent_decisions"][0]
            self.assertEqual(
                set(public_decision),
                {
                    "id",
                    "routing_config_version",
                    "rule_version",
                    "status",
                    "rule_level",
                    "model_level",
                    "final_level",
                    "confidence",
                    "task_kind",
                    "classifier_duration_ms",
                    "created_at",
                    "updated_at",
                    "message_preview",
                    "labeled",
                },
            )
            self.assertIs(type(public_decision["labeled"]), bool)
            self.assertTrue(public_decision["labeled"])
            self.assertLessEqual(len(public_decision["message_preview"]), 160)
            self.assertIn("[REDACTED]", public_decision["message_preview"])
            self.assertEqual(
                set(routing["promotion_gate"]),
                {
                    "seven_days",
                    "hundred_decisions",
                    "thirty_labels",
                    "ninety_percent_accuracy",
                    "no_forced_risk_misses",
                    "dispatch_overhead_p95",
                    "eligible",
                },
            )
            self.assertFalse(routing["promotion_gate"]["seven_days"])
            self.assertFalse(routing["promotion_gate"]["hundred_decisions"])
            self.assertFalse(routing["promotion_gate"]["thirty_labels"])
            self.assertTrue(routing["promotion_gate"]["ninety_percent_accuracy"])
            self.assertTrue(routing["promotion_gate"]["no_forced_risk_misses"])
            self.assertFalse(routing["promotion_gate"]["dispatch_overhead_p95"])
            self.assertFalse(routing["promotion_gate"]["eligible"])
            for private_value in (
                "a" * 64,
                "b" * 64,
                "CLASSIFIER-RAW-TOKEN",
                "TOKEN-SECRET",
                "QUERY-SECRET",
                "12345678901234567890",
                "safe_error_class",
                "risk_flags_json",
                "reasons_json",
                "config_sha256",
            ):
                self.assertNotIn(private_value, serialized)

    def test_public_routing_decision_redacts_legacy_long_previews_defensively(self):
        from qq_openclaw_dashboard.queries import public_routing_decision

        raw_preview = (
            "Bearer LEGACY-TOKEN api_key=LEGACY-KEY "
            "https://example.invalid/?secret=LEGACY-QUERY "
            "12345678901234567890 "
            + "x" * 240
        )
        result = public_routing_decision(
            {"message_preview": raw_preview, "labeled": 1}
        )

        self.assertIn("labeled", result)
        self.assertIn("message_preview", result)
        self.assertEqual(result["labeled"], True)
        self.assertLessEqual(len(result["message_preview"]), 160)
        self.assertIn("[REDACTED]", result["message_preview"])
        for secret in (
            "LEGACY-TOKEN",
            "LEGACY-KEY",
            "LEGACY-QUERY",
            "12345678901234567890",
        ):
            self.assertNotIn(secret, result["message_preview"])


class DashboardRoutingHttpTests(unittest.TestCase):
    def setUp(self):
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_dashboard.controller import DashboardController
        from qq_openclaw_dashboard.server import create_server

        self.temp_dir = tempfile.TemporaryDirectory()
        project = Path(self.temp_dir.name) / "project"
        workspace = Path(self.temp_dir.name) / "workspace"
        project.mkdir()
        workspace.mkdir()
        (project / "logs").mkdir()
        (project / ".env").write_text("ALLOWED_QQ_USERS=10001\n", encoding="utf-8")
        self.store = WorkspaceStateStore(workspace / "state.sqlite3")
        self.controller = DashboardController(
            project_dir=project,
            memory_path=workspace / "MEMORY.md",
            todo_path=workspace / "TODO.md",
            style_path=workspace / "STYLE.md",
            reminder_state_path=workspace / "REMINDER_STATE.md",
            state_store=self.store,
            onebot_probe=lambda: False,
            launch_agent_probe=lambda label: (False, None),
            routing_model_provider=lambda: ("openai/gpt-5.4-nano",),
        )
        self.server = create_server(self.controller, "127.0.0.1", 0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def post(self, path, payload, token="test-token"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Dashboard-Token"] = token
        http_request = request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with request.urlopen(http_request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post_with_headers(self, path, payload, headers):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers)
        http_request = request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=request_headers,
        )
        try:
            with request.urlopen(http_request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_label_and_config_mutations_require_dashboard_token(self):
        for path, payload in (
            (
                "/api/routing/label",
                {
                    "decision_id": 1,
                    "verdict": "correct",
                    "correct_level": "L2",
                    "expected_updated_at": "2026-08-15T00:00:00+00:00",
                },
            ),
            ("/api/routing/config", routing_payload()),
            ("/api/routing/config/rollback", {"target_version": 1}),
        ):
            status, _ = self.post(path, payload, token=None)
            self.assertEqual(status, 403)

    def test_routing_mutations_reject_bad_token_host_and_origin(self):
        endpoints = (
            (
                "/api/routing/config",
                routing_payload(),
            ),
            ("/api/routing/config/rollback", {"target_version": 1}),
            (
                "/api/routing/label",
                {
                    "decision_id": 1,
                    "verdict": "correct",
                    "correct_level": "L2",
                    "expected_updated_at": "2026-08-15T00:00:00+00:00",
                },
            ),
        )
        rejected_headers = (
            {},
            {"X-Dashboard-Token": "wrong-token"},
            {"X-Dashboard-Token": "test-token", "Host": "evil.invalid"},
            {
                "X-Dashboard-Token": "test-token",
                "Origin": "http://evil.invalid",
            },
        )
        for path, payload in endpoints:
            for headers in rejected_headers:
                with self.subTest(path=path, headers=headers):
                    status, _ = self.post_with_headers(path, payload, headers)
                    self.assertEqual(status, 403)

    def test_routing_internal_errors_are_generic_for_every_endpoint(self):
        methods = (
            ("update_routing_config", "/api/routing/config", routing_payload()),
            (
                "rollback_routing_config",
                "/api/routing/config/rollback",
                {"target_version": 1},
            ),
            (
                "label_routing_decision",
                "/api/routing/label",
                {
                    "decision_id": 1,
                    "verdict": "correct",
                    "correct_level": "L2",
                    "expected_updated_at": "2026-08-15T00:00:00+00:00",
                },
            ),
        )
        for method, path, payload in methods:
            def fail(*args, **kwargs):
                raise RuntimeError("SENTINEL-INTERNAL-SECRET")

            original = getattr(self.controller, method)
            setattr(self.controller, method, fail)
            try:
                status, response = self.post(path, payload)
            finally:
                setattr(self.controller, method, original)
            with self.subTest(path=path):
                self.assertEqual(status, 500)
                self.assertEqual(response["error"], "服务器内部错误，请稍后重试。")
                self.assertNotIn("SENTINEL-INTERNAL-SECRET", json.dumps(response))

    def test_config_is_strict_allowlisted_and_always_appends_a_version(self):
        status, response = self.post("/api/routing/config", routing_payload())
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        first = self.store.routing_repository.active_config()

        status, _ = self.post(
            "/api/routing/config", dict(routing_payload(), unexpected="nope")
        )
        self.assertEqual(status, 400)
        status, _ = self.post(
            "/api/routing/config", routing_payload(classifier_model="openai/not-allowed")
        )
        self.assertEqual(status, 400)
        status, _ = self.post(
            "/api/routing/config", routing_payload(max_daily_calls=True)
        )
        self.assertEqual(status, 400)

        status, response = self.post("/api/routing/config", routing_payload())
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        second = self.store.routing_repository.active_config()
        self.assertGreater(second["version"], first["version"])
        self.assertEqual(
            self.store.routing_repository.get_config(first["version"])["status"],
            "retired",
        )

        status, response = self.post(
            "/api/routing/config/rollback", {"target_version": first["version"]}
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        rollback = self.store.routing_repository.active_config()
        self.assertGreater(rollback["version"], second["version"])
        self.assertEqual(rollback["config"], first["config"])

    def test_label_requires_exact_values_and_preserves_owner_cas(self):
        active = self.store.routing_repository.create_config(
            dict(routing_payload(), enabled=True), activate=True
        )
        decision = self.store.routing_repository.start_decision(
            user_id=10001,
            source_message_sha256="c" * 64,
            message_sha256="d" * 64,
            message_preview="safe",
            routing_config_version=active["version"],
            rule_version="f1-v1",
            dispatch_overhead_ms=1,
        )
        decision = self.store.routing_repository.finish_decision(
            decision["id"], decision["updated_at"], status="rule_only",
            rule_level="L2", model_level=None, final_level="L2", confidence=0,
            task_kind="other", risk_flags=(), reasons=(), brainstorm_required=False,
            brainstorm_mode="disabled", classifier_provider=None,
            classifier_model=None, classifier_duration_ms=None,
        )
        payload = {
            "decision_id": decision["id"],
            "verdict": "correct",
            "correct_level": "L2",
            "expected_updated_at": decision["updated_at"],
            "note": "checked",
        }
        status, response = self.post("/api/routing/label", payload)
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        status, _ = self.post("/api/routing/label", payload)
        self.assertEqual(status, 400)
        status, _ = self.post(
            "/api/routing/label", dict(payload, note="x" * 301)
        )
        self.assertEqual(status, 400)
        status, _ = self.post(
            "/api/routing/label", dict(payload, unexpected="nope")
        )
        self.assertEqual(status, 400)


class DashboardRoutingCompositionTests(unittest.TestCase):
    def test_from_project_uses_only_valid_openclaw_model_identifiers(self):
        from qq_openclaw_dashboard.controller import DashboardController

        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            workspace = Path(temp_dir) / "workspace"
            project.mkdir()
            workspace.mkdir()
            config_path = project / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {
                                "models": {
                                    "openai/gpt-5.4-nano": {},
                                    "not a model": {},
                                }
                            },
                            "list": [],
                        },
                        "gateway": {"token": "OPENCLAW-SENTINEL-TOKEN"},
                        "auth": {"api_key": "OPENCLAW-SENTINEL-AUTH"},
                    }
                ),
                encoding="utf-8",
            )
            (project / ".env").write_text(
                "OPENCLAW_WORKSPACE=%s\n"
                "STATE_DATABASE_PATH=%s\n"
                "OPENCLAW_CONFIG_PATH=%s\n"
                "ALLOWED_QQ_USERS=10001\n"
                % (workspace, workspace / "state.sqlite3", config_path),
                encoding="utf-8",
            )

            controller = DashboardController.from_project(project)
            controller.update_routing_config(routing_payload())
            serialized = json.dumps(controller.get_state(), ensure_ascii=False)

            self.assertEqual(
                controller.state_store.routing_repository.active_config()["config"]["classifier_model"],
                "openai/gpt-5.4-nano",
            )
            self.assertNotIn("OPENCLAW-SENTINEL-TOKEN", serialized)
            self.assertNotIn("OPENCLAW-SENTINEL-AUTH", serialized)


class DashboardRoutingStaticTests(unittest.TestCase):
    def test_routing_view_contract_and_asset_are_available(self):
        static = ROOT / "src" / "qq_openclaw_dashboard" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app = (static / "app.js").read_text(encoding="utf-8")
        routing_path = static / "routing.js"
        self.assertTrue(routing_path.is_file())
        routing = routing_path.read_text(encoding="utf-8")

        for identifier in (
            'id="view-routing"',
            'id="routingConfigForm"',
            'id="routingDecisionTableBody"',
            'id="routingGateList"',
        ):
            self.assertIn(identifier, html)
        self.assertIn('data-view="routing"', html)
        self.assertIn('/assets/routing.js', html)
        self.assertIn("renderRoutingView(state.routing", app)
        self.assertIn("window.renderRoutingView", routing)
        self.assertNotIn("http://", routing)
        self.assertNotIn("https://", routing)
        self.assertNotIn("DASHBOARD_TOKEN", routing)

    def test_routing_renderer_renders_safe_empty_and_labeled_decisions(self):
        routing = (
            ROOT / "src" / "qq_openclaw_dashboard" / "static" / "routing.js"
        ).read_text(encoding="utf-8")
        engine = shutil.which("node") or shutil.which("osascript")
        self.assertIsNotNone(engine, "Node or macOS JavaScriptCore is required")
        command = [engine]
        if Path(engine).name == "osascript":
            command.extend(("-l", "JavaScript"))
        harness = r'''
var elements = {};
var window = {};
function Element(tag) {
  this.tagName = tag.toUpperCase(); this.children = []; this.className = '';
  this.textContent = ''; this.value = ''; this.checked = false; this.disabled = false;
}
Element.prototype.append = function() { for (var i = 0; i < arguments.length; i += 1) this.children.push(arguments[i]); };
Element.prototype.replaceChildren = function() { this.children = []; this.append.apply(this, arguments); };
Element.prototype.addEventListener = function() {};
Object.defineProperty(Element.prototype, 'innerHTML', {set: function() { throw new Error('unsafe HTML insertion'); }});
var document = {
  createElement: function(tag) { return new Element(tag); },
  getElementById: function(id) { return elements[id]; }
};
[
  'routingGateList', 'routingDecisionCount', 'routingLabeledCount', 'routingAccuracy',
  'routingForcedRiskMisses', 'routingClassifierP95', 'routingOverheadP95',
  'routingConfigVersion', 'routingEnabledInput', 'routingModelInput',
  'routingConfidenceInput', 'routingTimeoutInput', 'routingDailyCallsInput',
  'routingFailureThresholdInput', 'routingCooldownInput', 'routingRuleVersionInput',
  'routingRollbackVersionInput', 'routingConfigForm', 'routingMatrixTableBody',
  'routingDecisionTableBody'
].forEach(function(id) { elements[id] = new Element(id === 'routingConfigForm' ? 'form' : 'div'); });
function assert(condition, message) { if (!condition) throw new Error(message); }
function descendants(root, tag, result) {
  result = result || [];
  root.children.forEach(function(child) { if (child.tagName === tag) result.push(child); descendants(child, tag, result); });
  return result;
}
function classNodes(root, name, result) {
  result = result || [];
  root.children.forEach(function(child) {
    if (child.className.split(/\s+/).indexOf(name) >= 0) result.push(child);
    classNodes(child, name, result);
  });
  return result;
}
var zeroMatrix = {L0:{L0:0,L1:0,L2:0,L3:0},L1:{L0:0,L1:0,L2:0,L3:0},L2:{L0:0,L1:0,L2:0,L3:0},L3:{L0:0,L1:0,L2:0,L3:0}};
var helpers = {formatDateTime:function(value) { return value || '-'; }, api:function(){}, loadState:function(){}, toast:function(){}};
'''
        assertions = r'''
window.renderRoutingView({
  config_version: 7,
  config: {classifier_enabled:true,classifier_model:'model',confidence_threshold_percent:75,classifier_timeout_seconds:15,max_daily_calls:100,failure_threshold:3,cooldown_seconds:300,rule_version:'f1'},
  promotion_gate: {},
  metrics: {decision_count:0,labeled_count:0,label_accuracy:0,forced_risk_misses:0,classifier_duration_p95_ms:0,dispatch_overhead_p95_ms:0,confusion_matrix:zeroMatrix},
  recent_decisions: []
}, helpers);
assert(elements.routingGateList.children.length === 7, 'renders every promotion gate');
elements.routingGateList.children.forEach(function(gate) {
  assert(gate.children[0].textContent === '-', 'unknown gate status is a dash');
  assert(gate.children[0].className === 'neutral', 'unknown gate is neutral');
});
assert(elements.routingMatrixTableBody.children.length === 4, 'renders four matrix rows');
elements.routingMatrixTableBody.children.forEach(function(row) {
  assert(row.children.length === 5, 'renders four matrix cells per row');
  for (var i = 1; i < 5; i += 1) assert(String(row.children[i].textContent) === '0', 'renders zero matrix cells');
});
assert(elements.routingDecisionTableBody.children[0].children[0].textContent === '还没有近期影子路由决策。', 'renders empty decisions');
window.renderRoutingView({
  config_version: 7,
  config: {}, promotion_gate: {}, metrics: {confusion_matrix:zeroMatrix},
  recent_decisions: [
    {id:1,routing_config_version:7,status:'classified',final_level:'L2',confidence:0.5,created_at:'2026-08-15T00:00:00+00:00',updated_at:'2026-08-15T00:00:00+00:00',labeled:true,message_preview:'<img src=x onerror=alert(1)> safe preview',preview:'raw preview',reasons:'raw reason',note:'raw note'},
    {id:2,routing_config_version:6,status:'classified',final_level:'L1',confidence:0.5,created_at:'2026-08-15T00:00:00+00:00',updated_at:'2026-08-15T00:00:00+00:00',labeled:false,message_preview:'old preview'}
  ]
}, helpers);
var previews = classNodes(elements.routingDecisionTableBody, 'routing-preview');
assert(previews[0].textContent === '<img src=x onerror=alert(1)> safe preview', 'keeps dynamic preview as text');
assert(descendants(elements.routingDecisionTableBody, 'IMG').length === 0, 'does not execute preview HTML');
var buttons = descendants(elements.routingDecisionTableBody, 'BUTTON');
assert(buttons.length === 2 && buttons[0].disabled && buttons[1].disabled, 'disables labeled and old-version rows');
var selects = descendants(elements.routingDecisionTableBody, 'SELECT');
assert(selects[1].children.map(function(option) { return option.value; }).join(',') === 'L0,L1,L2,L3', 'renders all correction levels');
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "routing_harness.js"
            script_path.write_text(harness + routing + assertions, encoding="utf-8")
            completed = subprocess.run(
                command + [str(script_path)],
                text=True,
                capture_output=True,
                timeout=10,
            )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

if __name__ == "__main__":
    unittest.main()
