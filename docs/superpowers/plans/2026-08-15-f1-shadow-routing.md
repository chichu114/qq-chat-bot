# F1 四级影子路由实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有 QQ 回复、主模型调用次数和审批边界的前提下，实现可观测、可标注、可回滚的 L0-L3 混合影子路由。

**Architecture:** 在现有 `MessageCoordinator` 旁路注入 `ShadowRoutingObserver`。确定性规则先识别 L0 和强制 L3，模糊消息由独立只读 OpenClaw `routing` Agent 补判；所有结果写入 SQLite v6，并在管理台完成配置、标注和准入统计。路由结果在 F1 中没有正式执行权，任何故障都不得阻断 Direct、Outbox 或 QQ 收发。

**Tech Stack:** Python 3.9、`asyncio`、SQLite WAL、OpenClaw CLI/Gateway、原生 `unittest`、本地 `ThreadingHTTPServer`、原生 HTML/CSS/JavaScript、macOS launchd。

## Global Constraints

- F1 默认关闭；未配置时生产行为必须与当前 F0 `direct` 完全一致。
- 影子结果不得选择正式模型、改变回复文本、增加主模型调用或执行高风险动作。
- 强制高风险规则拥有最终升级权，轻量模型不能把 L3 降级。
- 分类 Agent 固定为独立 `routing` Agent，工具集为空且 elevated=false。
- 路由故障失败开放给 Direct，安全审批仍保持失败关闭。
- 新 schema 只追加，回滚不得要求删除表或降级数据库。
- Dashboard 继续只绑定 loopback，所有写 API 使用既有 Host、Origin 和 Token 校验。
- `.env`、SQLite、日志和生成的 LaunchAgent 配置权限保持 `0600`。
- 所有手工编辑使用 `apply_patch`；不得覆盖用户现有未提交改动。
- 每项任务必须先运行新增测试观察失败，再写最小实现。

---

### Task 1: 路由领域契约、确定性规则与隐私脱敏

**Files:**
- Create: `src/qq_openclaw_bridge/domain/routing.py`
- Modify: `src/qq_openclaw_bridge/domain/__init__.py`
- Create: `tests/test_domain_routing.py`

**Interfaces:**
- Produces: `RoutingContract.from_request(request, local_handled, config_version)`
- Produces: `DeterministicRiskRouter.evaluate(contract) -> RuleDecision`
- Produces: `parse_classifier_response(text) -> ClassifierDecision`
- Produces: `ConservativeRouteResolver.resolve(rule, classifier, threshold) -> RouteResolution`
- Produces: `redacted_preview(text, limit=160) -> str`
- Consumes: existing `AgentRequest`

- [ ] **Step 1: Write failing tests for L0, forced L3, ambiguity and redaction**

```python
class RoutingDomainTests(unittest.TestCase):
    def test_local_command_is_l0_without_classifier(self):
        contract = self.contract("查看任务", local_handled=True)
        decision = DeterministicRiskRouter().evaluate(contract)
        self.assertEqual(decision.level, "L0")
        self.assertFalse(decision.needs_classifier)

    def test_external_send_and_secret_operations_are_forced_l3(self):
        router = DeterministicRiskRouter()
        for text in ("替我把这段发给客户", "删除这个文件", "把 API Key 写进配置"):
            decision = router.evaluate(self.contract(text))
            self.assertEqual(decision.level, "L3")
            self.assertTrue(decision.forced)

    def test_ordinary_message_requires_classifier(self):
        decision = DeterministicRiskRouter().evaluate(self.contract("分析这个方案的优缺点"))
        self.assertIsNone(decision.level)
        self.assertTrue(decision.needs_classifier)

    def test_redacted_preview_masks_tokens_queries_and_long_numbers(self):
        preview = redacted_preview(
            "Bearer abcdefghijklmnop https://x.test?a=secret 123456789012345678"
        )
        self.assertNotIn("abcdefghijklmnop", preview)
        self.assertNotIn("secret", preview)
        self.assertNotIn("123456789012345678", preview)
        self.assertLessEqual(len(preview), 160)
```

- [ ] **Step 2: Run the domain tests and verify the import failure**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_domain_routing.py' -v`

Expected: FAIL because `qq_openclaw_bridge.domain.routing` does not exist.

- [ ] **Step 3: Implement immutable contracts and bounded rules**

Implement these exact immutable types:

```python
@dataclass(frozen=True)
class RoutingContract:
    user_id: int
    source_message_id: str
    text: str
    local_handled: bool
    config_version: int

@dataclass(frozen=True)
class RuleDecision:
    level: Optional[str]
    needs_classifier: bool
    forced: bool = False
    risk_flags: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()

@dataclass(frozen=True)
class ClassifierDecision:
    level: str
    confidence: float
    task_kind: str
    reasons: Tuple[str, ...]
    brainstorm_required: bool

@dataclass(frozen=True)
class RouteResolution:
    final_level: str
    confidence: float
    task_kind: str
    reasons: Tuple[str, ...]
    brainstorm_required: bool
    brainstorm_mode: str
```

Use bounded normalized text and explicit phrase groups for deletion, external sending,
publishing, finance, credentials and system control. Reject classifier JSON with unknown
keys, invalid L1-L3 enums, boolean confidence, non-finite numbers, reasons longer than
64 characters, more than eight reasons, or task kinds outside a fixed allowlist.

- [ ] **Step 4: Add resolver tests for low confidence, invalid JSON and blocked downgrade**

```python
def test_low_confidence_falls_back_to_l2(self):
    rule = RuleDecision(None, True)
    model = ClassifierDecision("L1", 0.60, "smalltalk", (), False)
    result = ConservativeRouteResolver().resolve(rule, model, 0.75)
    self.assertEqual(result.final_level, "L2")

def test_forced_l3_cannot_be_downgraded(self):
    rule = RuleDecision("L3", False, True, ("external_send",), ("forced_risk",))
    model = ClassifierDecision("L1", 0.99, "smalltalk", (), False)
    result = ConservativeRouteResolver().resolve(rule, model, 0.75)
    self.assertEqual(result.final_level, "L3")
    self.assertIn("model_downgrade_blocked", result.reasons)
```

- [ ] **Step 5: Run focused tests and existing domain regressions**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_domain_routing.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_domain_*.py'`

Expected: all tests PASS.

- [ ] **Step 6: Commit the domain slice**

```bash
git add src/qq_openclaw_bridge/domain/routing.py src/qq_openclaw_bridge/domain/__init__.py tests/test_domain_routing.py
git commit -m "feat: add shadow routing domain rules"
```

---

### Task 2: SQLite v6 路由决策、配置和人工标签仓库

**Files:**
- Modify: `src/qq_openclaw_bridge/persistence/schema.py`
- Modify: `src/qq_openclaw_bridge/persistence/database.py`
- Create: `src/qq_openclaw_bridge/persistence/routing.py`
- Modify: `src/qq_openclaw_bridge/persistence/__init__.py`
- Modify: `src/qq_openclaw_bridge/storage.py`
- Create: `tests/test_persistence_routing.py`
- Modify: `tests/test_storage.py`

**Interfaces:**
- Consumes: Task 1 routing contracts and resolutions
- Produces: `RoutingRepository.create_config(config, activate=False)`
- Produces: `RoutingRepository.active_config()` and `activate_config(version)`
- Produces: `start_decision(*, user_id: int, source_message_sha256: str, message_sha256: str, message_preview: str, routing_config_version: int, rule_version: str, dispatch_overhead_ms: int, now: Optional[datetime] = None) -> Dict[str, object]`
- Produces: `finish_decision(decision_id: int, expected_updated_at: str, *, status: str, rule_level: Optional[str], model_level: Optional[str], final_level: str, confidence: float, task_kind: str, risk_flags: Tuple[str, ...], reasons: Tuple[str, ...], brainstorm_required: bool, brainstorm_mode: str, classifier_provider: Optional[str], classifier_model: Optional[str], classifier_duration_ms: Optional[int], now: Optional[datetime] = None) -> Dict[str, object]`
- Produces: `fail_decision(decision_id: int, expected_updated_at: str, safe_error_class: str, final_level: str = "L2", now: Optional[datetime] = None) -> Dict[str, object]`
- Produces: `recover_pending_as_interrupted()`
- Produces: `label(decision_id, user_id, verdict, correct_level, note, expected_updated_at)`
- Produces: `recent_decisions(limit, user_id=None)` and `metrics(now)`
- Produces: `WorkspaceStateStore.routing_repository`

- [ ] **Step 1: Write failing schema and repository tests**

```python
def test_schema_v6_creates_routing_tables_and_one_active_config(self):
    store = WorkspaceStateStore(self.path)
    with sqlite3.connect(str(self.path)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
    self.assertEqual(version, "6")
    self.assertTrue({"route_decisions", "route_labels", "routing_config_versions"} <= tables)

def test_decision_is_idempotent_per_owner_source_and_config(self):
    first = self.repository.start_decision(**self.pending_values())
    second = self.repository.start_decision(**self.pending_values())
    self.assertEqual(first["id"], second["id"])

def test_label_uses_owner_and_compare_and_swap(self):
    decision = self.repository.start_decision(**self.pending_values())
    label = self.repository.label(
        decision["id"], 363129743, "correct", "L2", "", decision["updated_at"]
    )
    self.assertEqual(label["verdict"], "correct")
    with self.assertRaises(ValueError):
        self.repository.label(
            decision["id"], 363129743, "upgrade", "L3", "", decision["updated_at"]
        )
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_persistence_routing.py' -v`

Expected: FAIL because schema v6 and `RoutingRepository` do not exist.

- [ ] **Step 3: Add append-only v6 migration**

Set `SCHEMA_VERSION = "6"`. Keep `migrate_to_v5(connection)` unchanged and call it before
new `migrate_to_v6(connection)`. Add the three approved tables plus indexes for active config,
decision owner/time, decision status/time and label decision ownership. Include
`dispatch_overhead_ms INTEGER` and `updated_at TEXT NOT NULL` in `route_decisions` so the
approved latency gate and CAS can be computed without inference.

- [ ] **Step 4: Implement strict repository transitions and canonical config hashing**

Canonical routing config must contain exactly:

```python
{
    "enabled": bool,
    "classifier_enabled": bool,
    "classifier_model": str,
    "confidence_threshold_percent": int,
    "classifier_timeout_seconds": int,
    "max_daily_calls": int,
    "failure_threshold": int,
    "cooldown_seconds": int,
    "rule_version": str,
}
```

Validate bounds: confidence 50-100, timeout 1-120, daily calls 0-10000, failure threshold
1-20, cooldown 1-3600, and model/rule identifiers with the existing bounded identifier style.
Only `pending` may transition to `classified`, `rule_only` or `error`. Store JSON arrays
canonically and expose parsed tuples in returned dictionaries.

- [ ] **Step 5: Implement metrics with an explicit confusion matrix**

Return counts, first/last decision time, label accuracy, forced-risk misses, classifier error
rate, P50/P95 classifier duration, P95 dispatch overhead, per-level counts and a 4x4
`predicted -> correct` confusion matrix. An unlabeled decision must not affect accuracy.

- [ ] **Step 6: Wire the repository through `WorkspaceStateStore` and test recovery**

Instantiate `RoutingRepository(self._database)` beside the orchestration repository, expose
the property, and call `recover_pending_as_interrupted()` only from production composition,
not from the generic store constructor.

- [ ] **Step 7: Run persistence and storage regressions**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_persistence_routing.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_persistence_*.py'`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_storage.py'`

Expected: all tests PASS.

- [ ] **Step 8: Commit the persistence slice**

```bash
git add src/qq_openclaw_bridge/persistence/schema.py src/qq_openclaw_bridge/persistence/database.py src/qq_openclaw_bridge/persistence/routing.py src/qq_openclaw_bridge/persistence/__init__.py src/qq_openclaw_bridge/storage.py tests/test_persistence_routing.py tests/test_storage.py
git commit -m "feat: persist shadow routing decisions"
```

---

### Task 3: 轻量分类器、调用保险丝与影子任务生命周期

**Files:**
- Create: `src/qq_openclaw_bridge/application/shadow_routing.py`
- Create: `src/qq_openclaw_bridge/runtime/routing.py`
- Modify: `src/qq_openclaw_bridge/application/__init__.py`
- Modify: `src/qq_openclaw_bridge/runtime/__init__.py`
- Create: `tests/test_shadow_routing.py`
- Create: `tests/test_runtime_routing.py`

**Interfaces:**
- Consumes: Task 1 domain functions, Task 2 repository, existing `OpenClawCliAgentAdapter`
- Produces: `LightweightRouteClassifier.classify(contract, config) -> ClassifierCallResult`
- Produces: `RouteCallFuse.allow(now, calls_today)` / `succeed()` / `fail(now)`
- Produces: `ShadowRoutingObserver.schedule(request, local_handled) -> float`
- Produces: `await ShadowRoutingObserver.wait()` and `recover()`
- Produces: `create_shadow_routing_runtime(config, state_store)`

- [ ] **Step 1: Write failing classifier parsing and fuse tests**

```python
def test_classifier_uses_dedicated_agent_and_session(self):
    adapter = RecordingAdapter('{"level":"L1","confidence":0.9,"task_kind":"smalltalk","reasons":["short"],"brainstorm_required":false}')
    classifier = LightweightRouteClassifier(adapter)
    result = classifier.classify(self.contract(), self.routing_config())
    self.assertEqual(result.decision.level, "L1")
    self.assertTrue(adapter.requests[0].session_key.startswith("agent:routing:qq-"))

def test_fuse_blocks_after_threshold_and_recovers_after_cooldown(self):
    fuse = RouteCallFuse(failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        fuse.fail(now=100.0)
    self.assertFalse(fuse.allow(now=120.0, calls_today=0, max_daily_calls=100))
    self.assertTrue(fuse.allow(now=161.0, calls_today=0, max_daily_calls=100))
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_shadow_routing.py' -v`

Expected: FAIL because the observer and classifier do not exist.

- [ ] **Step 3: Implement the classifier and safe prompt**

Build an `AgentRequest` with session key
`agent:routing:qq-{user_id}-{source_hash_prefix}`. The prompt must include the exact L1-L3
schema, forced-risk warning, original message bounded to 4000 characters, and “只输出 JSON”.
Use the adapter response metadata for provider, model, tokens and duration; never persist raw
stdout or classifier text.

- [ ] **Step 4: Implement the observer as a tracked best-effort task set**

`schedule()` must only create and register an `asyncio.Task`, measure synchronous dispatch
overhead with `time.perf_counter_ns()`, and return milliseconds. The task performs repository
I/O and blocking classifier work with `asyncio.to_thread`. Done callbacks consume exceptions,
record bounded logs and remove tasks from the set. `wait()` gathers all current tasks during
controlled shutdown.

- [ ] **Step 5: Test non-blocking behavior, interruption and budget exhaustion**

```python
async def test_schedule_does_not_wait_for_classifier(self):
    observer = self.observer(classifier=BlockingClassifier())
    started = time.perf_counter()
    overhead = observer.schedule(self.request(), local_handled=False)
    self.assertLess(time.perf_counter() - started, 0.05)
    self.assertLess(overhead, 50)

async def test_budget_exhaustion_records_rule_only_l2(self):
    observer = self.observer(max_daily_calls=0)
    observer.schedule(self.request(), local_handled=False)
    await observer.wait()
    row = self.repository.recent_decisions(1)[0]
    self.assertEqual(row["final_level"], "L2")
    self.assertIn("classifier_budget_exhausted", row["reasons"])
```

- [ ] **Step 6: Build runtime composition with default active config creation**

`create_shadow_routing_runtime` returns `None` unless `config.enable_shadow_routing` is true
and SQLite is available. On first enable, create and activate a conservative config with
classifier disabled, model `openai/gpt-5.4-nano`, confidence 75, timeout 15, max daily calls
100, failure threshold 3, cooldown 300 and rule version `f1-v1`.

- [ ] **Step 7: Run focused and agent adapter regressions**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_shadow_routing.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_runtime_routing.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_agent.py'`

Expected: all tests PASS.

- [ ] **Step 8: Commit the application runtime slice**

```bash
git add src/qq_openclaw_bridge/application/shadow_routing.py src/qq_openclaw_bridge/runtime/routing.py src/qq_openclaw_bridge/application/__init__.py src/qq_openclaw_bridge/runtime/__init__.py tests/test_shadow_routing.py tests/test_runtime_routing.py
git commit -m "feat: add nonblocking shadow router"
```

---

### Task 4: MessageCoordinator、Composition Root 与 Bridge 生命周期接线

**Files:**
- Modify: `src/qq_openclaw_bridge/config.py`
- Modify: `.env.example`
- Modify: `src/qq_openclaw_bridge/application/coordinator.py`
- Modify: `src/qq_openclaw_bridge/application/coordinator_factory.py`
- Modify: `src/qq_openclaw_bridge/runtime/composition.py`
- Modify: `src/qq_openclaw_bridge/bridge.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_application_coordinator.py`
- Modify: `tests/test_composition.py`
- Modify: `tests/test_bridge.py`
- Create: `tests/test_f1_shadow_acceptance.py`

**Interfaces:**
- Consumes: `create_shadow_routing_runtime` from Task 3
- Adds: `Config.enable_shadow_routing: bool`
- Adds: `Config.routing_agent_id: str`
- Adds: optional `shadow_routing_observer` to `MessageCoordinator`
- Adds: `MessageCoordinator.wait_for_shadow_tasks()`

- [ ] **Step 1: Write failing configuration tests**

```python
def test_shadow_routing_defaults_off(self):
    config = Config.from_env(self.base_env())
    self.assertFalse(config.enable_shadow_routing)
    self.assertEqual(config.routing_agent_id, "routing")

def test_shadow_routing_requires_sqlite(self):
    with self.assertRaisesRegex(ValueError, "SQLite"):
        Config.from_env(dict(
            self.base_env(), ENABLE_SHADOW_ROUTING="true", ENABLE_SQLITE_STORE="false"
        ))
```

- [ ] **Step 2: Write failing coordinator acceptance tests**

Test these exact invariants:

- A local command schedules L0 and calls neither classifier nor main agent.
- A normal message schedules one shadow decision and calls the main agent exactly once.
- A classifier exception does not change the Direct reply.
- `legacy_direct` and `direct` both permit shadow observation without changing their reply.
- `wait_for_shadow_tasks()` drains observers on shutdown.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_f1_shadow_acceptance.py' -v`

Expected: FAIL because no production observer is wired.

- [ ] **Step 4: Add minimal config flags and validation**

Add only:

```dotenv
ENABLE_SHADOW_ROUTING=false
ROUTING_AGENT_ID=routing
```

Reject enabling F1 without SQLite. Validate `ROUTING_AGENT_ID` with the same bounded agent ID
rules as `OPENCLAW_AGENT_ID`. Do not place model thresholds in `.env`; those belong to the
versioned routing config table.

- [ ] **Step 5: Wire observation without changing orchestration semantics**

Call `observer.schedule(active_request, local_handled=reply_text is not None)` exactly once in
`resolve_prepared` before the existing Direct/legacy branch. Wrap only the schedule call in a
best-effort exception boundary. Do not await classification before delivery. Store the returned
dispatch overhead on the pending route decision through the observer.

- [ ] **Step 6: Wire startup recovery and controlled shutdown**

In `runtime/composition.py`, recover stale pending decisions and create the observer only when
enabled. Inject it into the coordinator. Extend Bridge shutdown/wait logic to await tracked
shadow tasks with a bounded timeout; timeout cancellation marks pending rows interrupted on the
next startup.

- [ ] **Step 7: Run focused and F0 regression suites**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_f1_shadow_acceptance.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_application_coordinator.py'`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_composition.py'`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_f0_acceptance.py'`

Expected: all tests PASS and main model call count remains one.

- [ ] **Step 8: Commit the production wiring slice**

```bash
git add .env.example src/qq_openclaw_bridge/config.py src/qq_openclaw_bridge/application/coordinator.py src/qq_openclaw_bridge/application/coordinator_factory.py src/qq_openclaw_bridge/runtime/composition.py src/qq_openclaw_bridge/bridge.py tests/test_config.py tests/test_application_coordinator.py tests/test_composition.py tests/test_bridge.py tests/test_f1_shadow_acceptance.py
git commit -m "feat: wire shadow routing beside direct runtime"
```

---

### Task 5: 独立只读 OpenClaw routing Agent 与启动检查

**Files:**
- Create: `scripts/install_routing_agent.py`
- Modify: `scripts/check_ready.sh`
- Modify: `tests/test_scripts.py`
- Create: `tests/test_install_routing_agent.py`

**Interfaces:**
- Produces: CLI `scripts/install_routing_agent.py --config PATH --model MODEL`
- Produces: readiness check for agent existence, exact empty tool profile and allowed model
- Consumes: existing OpenClaw JSON structure and `ROUTING_AGENT_ID`

- [ ] **Step 1: Write failing structured-config tests**

```python
def test_installer_adds_dedicated_toolless_routing_agent(self):
    install_routing_agent(self.config_path, "openai/gpt-5.4-nano")
    config = json.loads(self.config_path.read_text())
    routing = next(agent for agent in config["agents"]["list"] if agent["id"] == "routing")
    self.assertEqual(routing["model"], "openai/gpt-5.4-nano")
    self.assertEqual(routing["skills"], [])
    self.assertEqual(routing["tools"]["alsoAllow"], [])
    self.assertFalse(routing["tools"]["elevated"]["enabled"])
```

- [ ] **Step 2: Run installer tests and verify failure**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_install_routing_agent.py' -v`

Expected: FAIL because the installer does not exist.

- [ ] **Step 3: Implement atomic JSON update and backup**

Load JSON structurally, reject duplicate `routing` IDs, add the selected model to
`agents.defaults.models`, and create/update exactly one `routing` Agent. Write through a sibling
temporary file, `fsync`, `chmod 0600`, preserve a timestamped backup, then `os.replace`.
Explicitly deny exec, process, code execution, write/edit, browser, message, Gateway,
sessions, subagents and elevated tools. Also deny read, file fetch, directory listing, memory,
web search/fetch and session status so `profile=minimal` cannot accidentally expose a read-only
tool. Do not print tokens or the whole config.

- [ ] **Step 4: Extend readiness without making F1 mandatory when disabled**

When `ENABLE_SHADOW_ROUTING=false`, report `[SKIP]`. When enabled, verify the routing Agent,
active routing config and model allowlist. Add `--probe-routing` to make one classification call
with a unique session and validate strict JSON without sending QQ messages.

- [ ] **Step 5: Test failure-safe output and script regressions**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_install_routing_agent.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_scripts.py'`

Expected: all tests PASS and captured output contains no configured secret values.

- [ ] **Step 6: Commit the routing Agent tooling**

```bash
git add scripts/install_routing_agent.py scripts/check_ready.sh tests/test_install_routing_agent.py tests/test_scripts.py
git commit -m "feat: provision restricted routing agent"
```

---

### Task 6: 管理台路由查询、配置版本、标签和回滚 API

**Files:**
- Modify: `src/qq_openclaw_dashboard/queries.py`
- Modify: `src/qq_openclaw_dashboard/commands.py`
- Modify: `src/qq_openclaw_dashboard/controller.py`
- Modify: `src/qq_openclaw_dashboard/server.py`
- Create: `tests/test_dashboard_routing.py`

**Interfaces:**
- Adds state key: `routing`
- Adds POST: `/api/routing/config`
- Adds POST: `/api/routing/config/rollback`
- Adds POST: `/api/routing/label`
- Consumes: Task 2 `RoutingRepository`
- Consumes: an injected provider that returns only model identifiers from
  `agents.defaults.models`; it must never return the rest of OpenClaw configuration

- [ ] **Step 1: Write failing query-service tests for safe public state**

Assert that `get_state()["routing"]` contains config version, public config, metrics,
confusion matrix, recent decisions and promotion gate. Assert it excludes message hashes,
classifier raw text, error details, tokens and repository-private fields.

- [ ] **Step 2: Write failing authenticated mutation tests**

```python
def test_label_and_config_mutations_require_dashboard_token(self):
    for path, payload in (
        ("/api/routing/label", {"decision_id": 1, "verdict": "correct", "correct_level": "L2", "expected_updated_at": "2026-08-15T00:00:00+00:00"}),
        ("/api/routing/config", {"classifier_enabled": True, "classifier_model": "openai/gpt-5.4-nano", "confidence_threshold_percent": 75, "classifier_timeout_seconds": 15, "max_daily_calls": 100, "failure_threshold": 3, "cooldown_seconds": 300, "rule_version": "f1-v1"}),
    ):
        status, _ = self.post(path, payload, token=None)
        self.assertEqual(status, 403)
```

- [ ] **Step 3: Run focused tests and verify failure**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_dashboard_routing.py' -v`

Expected: FAIL because routing state and endpoints do not exist.

- [ ] **Step 4: Implement public projection and promotion gate**

Compute gate booleans independently for 7 days, 100 decisions, 30 labels, 90% accuracy,
zero forced-risk misses and P95 dispatch overhead <=1500 ms. `eligible` is true only when all
six pass. Return per-level distributions and the full 4x4 confusion matrix, even for zero cells.

- [ ] **Step 5: Implement strict command validation and versioned updates**

The config endpoint accepts only the eight canonical config fields from Task 2 and always
creates a new active version. Reject `classifier_model` unless it is present in the injected
OpenClaw model identifier allowlist; do not read or serialize sibling auth and Gateway fields.
Rollback accepts a positive target version and reactivates its canonical payload as a new
version so audit history remains append-only. Label requests require decision ID, verdict,
correct level and expected update timestamp; note is optional and bounded to 300 printable
characters.

- [ ] **Step 6: Run Dashboard backend regressions**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_dashboard_routing.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_dashboard*.py'`

Expected: all tests PASS.

- [ ] **Step 7: Commit the Dashboard backend slice**

```bash
git add src/qq_openclaw_dashboard/queries.py src/qq_openclaw_dashboard/commands.py src/qq_openclaw_dashboard/controller.py src/qq_openclaw_dashboard/server.py tests/test_dashboard_routing.py
git commit -m "feat: expose shadow routing controls"
```

---

### Task 7: 管理台影子路由页面与人工标注交互

**Files:**
- Modify: `src/qq_openclaw_dashboard/static/index.html`
- Modify: `src/qq_openclaw_dashboard/static/app.css`
- Modify: `src/qq_openclaw_dashboard/static/app.js`
- Create: `src/qq_openclaw_dashboard/static/routing.js`
- Modify: `tests/test_dashboard_routing.py`

**Interfaces:**
- Consumes: `state.routing` and Task 6 POST endpoints
- Produces: sidebar “影子路由” view, config form, decision table and label controls

- [ ] **Step 1: Add failing static contract tests**

Assert the HTML contains `view-routing`, `routingConfigForm`, `routingDecisionTableBody`,
`routingGateList` and the four correction levels. Assert `routing.js` is served through the
existing `/assets/` route and contains no inline secret or external URL.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_dashboard_routing.py' -v`

Expected: FAIL because routing view elements are absent.

- [ ] **Step 3: Build a dense operational view following the existing design system**

Add one sidebar entry. The view contains: compact gate status, six metrics, one config form,
one confusion matrix table and one recent-decision table. Do not nest cards or add marketing
copy. Decision previews wrap; tables use the existing responsive scroll container.

- [ ] **Step 4: Implement rendering and authenticated mutations**

`routing.js` exports `window.renderRoutingView(routing, helpers)`. Render unknown or missing
values as `-`, never use `innerHTML` with unescaped preview, reasons or notes, and disable label
buttons for already-labeled rows unless the row version is current. Config save, rollback and
labels call the existing `api()` helper and refresh state after success.

- [ ] **Step 5: Add browser-width and empty-state tests**

Extend static tests for empty decisions, zero confusion cells, long redacted preview and all
four levels. Start the Dashboard test server and inspect at 1280x800 and 390x844; verify no
overlap, clipping or horizontal page overflow outside table containers.

- [ ] **Step 6: Run Dashboard tests**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_dashboard_routing.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_dashboard*.py'`

Expected: all tests PASS.

- [ ] **Step 7: Commit the Dashboard frontend slice**

```bash
git add src/qq_openclaw_dashboard/static/index.html src/qq_openclaw_dashboard/static/app.css src/qq_openclaw_dashboard/static/app.js src/qq_openclaw_dashboard/static/routing.js tests/test_dashboard_routing.py
git commit -m "feat: add shadow routing dashboard"
```

---

### Task 8: F1 可观测性、验收门禁与运维文档

**Files:**
- Modify: `src/qq_openclaw_bridge/runtime/readiness.py`
- Modify: `src/qq_openclaw_watchdog/probes.py`
- Modify: `src/qq_openclaw_watchdog/watchdog.py`
- Modify: `tests/test_watchdog_probes.py`
- Modify: `tests/test_watchdog.py`
- Modify: `README.md`
- Create: `docs/f1-shadow-routing-runbook-zh.md`
- Create: `tests/test_f1_operational_acceptance.py`

**Interfaces:**
- Consumes: route metrics and active config from earlier tasks
- Produces: stale-pending and classifier-circuit health signals
- Produces: Chinese enable, label, rollback and incident runbook

- [ ] **Step 1: Write failing health tests**

Add a routing probe that is healthy when F1 is disabled, and when enabled requires an active
config, no stale pending decisions older than the configured timeout plus 60 seconds, and a
classifier circuit state of `closed` or cooling for less than one configured cooldown. It must
not call a model from the 30-second watchdog loop.

- [ ] **Step 2: Run watchdog tests and verify failure**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_watchdog*.py' -v`

Expected: FAIL because routing health is not checked.

- [ ] **Step 3: Add semantic routing health without changing restart ownership**

Expose `routing` in watchdog checks and health timeline. A routing failure degrades health but
does not restart Bridge while Direct, OneBot, Gateway and Outbox remain healthy. This prevents
an optional shadow observer from causing a production restart loop.

- [ ] **Step 4: Write the Chinese runbook with exact commands**

Document: install routing Agent, run `check_ready.sh --probe-routing`, enable pure-rule mode,
enable classifier through Dashboard, inspect labels, export gate evidence, disable F1, rollback
config and confirm Direct E2E. Include database queries that select counts and statuses without
printing previews or secrets.

- [ ] **Step 5: Add operational acceptance tests**

Verify disabled-by-default startup, pure-rule startup without routing Agent, classifier-enabled
readiness failure when Agent missing, stale pending recovery, watchdog degradation isolation and
F0 rollback compatibility.

- [ ] **Step 6: Run operational and complete test suites**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_f1_operational_acceptance.py' -v`

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`

Expected: all tests PASS; the test count is greater than the pre-F1 baseline of 745.

- [ ] **Step 7: Commit observability and documentation**

```bash
git add src/qq_openclaw_bridge/runtime/readiness.py src/qq_openclaw_watchdog/probes.py src/qq_openclaw_watchdog/watchdog.py tests/test_watchdog_probes.py tests/test_watchdog.py tests/test_f1_operational_acceptance.py README.md docs/f1-shadow-routing-runbook-zh.md
git commit -m "docs: add F1 routing operations and health"
```

---

### Task 9: 生产部署、纯规则烟测与轻量分类启用

**Files:**
- Modify: `${HOME}/qq-chat-bot/.env`
- Modify: `${HOME}/.openclaw/openclaw.json`
- Verify: `${HOME}/.openclaw/workspace/chichu.sqlite3`
- Verify: `${HOME}/qq-chat-bot/logs/bridge.log`
- Verify: `${HOME}/qq-chat-bot/logs/health.json`

**Interfaces:**
- Consumes: all implementation tasks
- Produces: running F1 shadow mode with active versioned config and rollback evidence

- [ ] **Step 1: Create and verify an encrypted pre-deployment backup**

Run the existing manual backup command, verify the resulting `.chichu-backup`, and record its
filename. Confirm `.env`, database and OpenClaw config permissions are `0600` before deployment.

- [ ] **Step 2: Synchronize only reviewed project files to the production mirror**

Use the existing deployment procedure without deleting production-only logs, `.env`, venv,
backups or LaunchAgent state. Compare hashes for the new routing modules and Dashboard assets.

- [ ] **Step 3: Install the routing Agent and keep classifier disabled**

Run:

```bash
${HOME}/qq-chat-bot/.venv/bin/python ${HOME}/qq-chat-bot/scripts/install_routing_agent.py --config ${HOME}/.openclaw/openclaw.json --model openai/gpt-5.4-nano
```

Restart the OpenClaw Gateway and confirm the QQ Agent tool policy is unchanged.

- [ ] **Step 4: Enable F1 in pure-rule mode and restart managed services**

Set `ENABLE_SHADOW_ROUTING=true`, leave the active routing config
`classifier_enabled=false`, then restart Bridge, Dashboard and watchdog through their existing
LaunchAgent service scripts.

- [ ] **Step 5: Run pure-rule end-to-end evidence**

Send one local command, one ordinary question and one high-risk test phrase that does not approve
or execute an action. Verify L0, unresolved-to-L2 and forced L3 records respectively; verify QQ
replies still use the Direct path, Outbox rows are sent and main model calls did not increase for
the local command.

- [ ] **Step 6: Enable classifier and run the dedicated probe**

Use Dashboard to create a new active routing config with `classifier_enabled=true`. Run
`scripts/check_ready.sh --probe-routing`, send one low-risk chat and one architecture question,
then verify classifier metadata, L1/L3 predictions and `brainstorm_mode=ask_user` without any QQ
interruption.

- [ ] **Step 7: Exercise failure isolation and rollback**

Temporarily activate a deliberately unavailable classifier model, send a normal message and
verify Direct still replies while the route decision becomes error/L2. Roll back the routing
config in Dashboard, then disable `ENABLE_SHADOW_ROUTING` and restart once to prove F0 Direct
continues without reading F1 decisions. Re-enable the last verified routing config afterward.

- [ ] **Step 8: Capture final production evidence**

Record service states, active routing config version, route status counts, latest Direct run,
Outbox status counts, watchdog health and backup filename. Do not print message previews,
credentials, Gateway token or API keys.

- [ ] **Step 9: Run the final full suite and inspect the F1 commit history**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`

Expected: all tests PASS. Commit only deployment documentation or safe configuration examples;
do not commit production `.env`, OpenClaw config, SQLite files, logs or backups.

```bash
git status --short
git log --oneline -9
```

The final report must state that F1 is collecting evidence and has not activated F2 model
routing, dual-model council, DAG execution or audit models.
