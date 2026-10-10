# F0 Orchestration Control Plane Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不增加模型调用或改变 QQ 回复的前提下，把现有 Direct 单模型链路装入可配置、可追踪、可回滚的编排 Runtime，并补齐结构化 ActionIntent、正式服务管理、日志轮转和加密备份。

**Architecture:** 生产对象统一在 `runtime/composition.py` 装配，`Bridge` 只接收已经构造好的消息协调器与服务。`ORCHESTRATION_MODE=legacy_direct` 保留当前兼容路径，`direct` 通过新的 `OrchestrationRuntime` 执行同一组 Direct 策略；两条路径共享 Agent、CommandChain、记忆和 Outbox。SQLite v5 追加编排、配置和动作意图表，所有安全动作绑定不可变 payload 哈希。

**Tech Stack:** Python 3.9、`asyncio`、SQLite WAL、OpenClaw Gateway、NapCat OneBot v11、原生 HTML/CSS/JavaScript、macOS launchd、`cryptography` AESGCM、`unittest`。

## Global Constraints

- 保留白名单私聊、限流、消息去重、每用户串行、跨用户并发和 Outbox 语义。
- F0 的 `legacy_direct` 与 `direct` 均只能对普通模型消息调用一次当前 OpenClaw `qq` Agent。
- F0 不允许启用 `shadow` 或 `active`；配置这些值必须拒绝启动。
- QQ Agent 的 Docker 沙箱、只读工作区、无网络和工具 deny 列表不得扩大。
- SQLite schema 只从 v4 追加到 v5，不删除、重命名或改变旧表语义。
- `ActionIntent` 存储或校验失败必须 fail closed；普通 trace 写失败不得阻断 Direct 文本回复。
- Dashboard mutation 继续要求 loopback Host、可信 Origin 和随机 Dashboard Token。
- 新日志文件为 `0600`，单文件最大 10 MiB，保留 5 代。
- 新自动备份必须使用 AES-256-GCM；加密失败时禁止静默生成明文自动备份。
- 旧普通 ZIP 只能标记为 `legacy-unencrypted`，不得显示为已加密。
- 所有手工代码修改使用 `apply_patch`，不覆盖用户现有未提交改动。
- 每个任务先写失败测试，最小实现通过后再运行相关回归并单独提交。

---

## File Map

### New production modules

- `src/qq_openclaw_bridge/orchestration/runtime.py`：统一 Direct 编排运行入口、状态推进和 trace 降级。
- `src/qq_openclaw_bridge/orchestration/config.py`：运行模式和规范化编排配置。
- `src/qq_openclaw_bridge/persistence/schema.py`：v1-v5 追加式 schema 初始化和迁移入口。
- `src/qq_openclaw_bridge/persistence/orchestration.py`：运行、事件和配置版本 Repository。
- `src/qq_openclaw_bridge/domain/actions.py`：ActionIntent 值对象、规范化 payload 和状态规则。
- `src/qq_openclaw_bridge/persistence/actions.py`：ActionIntent 原子审批、消费和终态 Repository。
- `src/qq_openclaw_bridge/runtime/logging.py`：进程级安全日志和轮转配置。
- `src/qq_openclaw_bridge/runtime/backup_crypto.py`：AES-GCM envelope 和 Keychain 密钥适配器。
- `src/qq_openclaw_dashboard/static/orchestration.js`：编排页面渲染和 mutation。
- `launchd/com.chichu.qq-bridge.plist.template`：Bridge LaunchAgent 模板。
- `launchd/com.chichu.qq-dashboard.plist.template`：Dashboard LaunchAgent 模板。
- `launchd/com.chichu.qq-watchdog.plist.template`：Watchdog LaunchAgent 模板。
- `scripts/install_launch_agents.py`：使用 `plistlib` 生成和安装受管 plist。

### Existing modules to modify

- `src/qq_openclaw_bridge/config.py`：读取并验证 `ORCHESTRATION_MODE` 与日志设置。
- `src/qq_openclaw_bridge/orchestration/models.py`：补充 run identity 和结构化结果字段。
- `src/qq_openclaw_bridge/orchestration/ports.py`：补充 Runtime/trace/config Protocol。
- `src/qq_openclaw_bridge/application/coordinator.py`：支持 legacy 与 Runtime 两条可注入路径。
- `src/qq_openclaw_bridge/bridge.py`：移除具体 planner/executor/verifier 构造。
- `src/qq_openclaw_bridge/runtime/composition.py`：创建唯一 Runtime 和 Coordinator。
- `src/qq_openclaw_bridge/persistence/database.py`：委托 schema 模块并把版本升级到 5。
- `src/qq_openclaw_bridge/storage.py`：暴露新 Repository 兼容属性。
- `src/qq_openclaw_bridge/security.py`：高风险文本转换为 ActionIntent 并绑定审批。
- `src/qq_openclaw_bridge/backups.py`：生成/恢复加密格式并识别 legacy ZIP。
- `src/qq_openclaw_bridge/__main__.py`、Dashboard/Watchdog `__main__.py`：启动最早期配置安全日志。
- `src/qq_openclaw_dashboard/queries.py`、`commands.py`、`server.py`：编排查询和受限模式切换。
- `src/qq_openclaw_dashboard/static/index.html`、`app.js`、`app.css`：编排控制页面。
- `scripts/bridge_service.sh`、`dashboard_service.sh`、`watchdog_service.sh`：改为 launchctl 兼容外观。
- `apps/*QQ智能体*.app/Contents/MacOS/*`：继续调用服务脚本，不直接拥有进程。
- `pyproject.toml`、`requirements.txt`：同步 `pypdf>=4,<7` 与受约束的 `cryptography`。
- `.env.example`、`README.md`：记录模式、备份、LaunchAgent 和回滚流程。

---

### Task 1: Freeze F0 configuration and production baseline

**Files:**
- Modify: `src/qq_openclaw_bridge/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Produces: `Config.orchestration_mode: str` with only `legacy_direct` or `direct` accepted in F0.
- Produces: `Config.log_max_bytes: int = 10485760` and `Config.log_backup_count: int = 5`.
- Consumes: existing `Config.from_env()` and `OpenClawCliAgentAdapter` behavior.

- [ ] **Step 1: Add failing configuration tests**

```python
def test_f0_orchestration_mode_defaults_to_legacy_direct(self):
    config = Config.from_env(self.base_env())
    self.assertEqual(config.orchestration_mode, "legacy_direct")

def test_f0_accepts_direct_and_rejects_unimplemented_modes(self):
    env = self.base_env(ORCHESTRATION_MODE="direct")
    self.assertEqual(Config.from_env(env).orchestration_mode, "direct")
    for value in ("shadow", "active", "unknown", ""):
        with self.subTest(value=value), self.assertRaises(ValueError):
            Config.from_env(self.base_env(ORCHESTRATION_MODE=value))

def test_log_rotation_values_must_be_positive(self):
    env = self.base_env(LOG_MAX_BYTES="10485760", LOG_BACKUP_COUNT="5")
    config = Config.from_env(env)
    self.assertEqual((config.log_max_bytes, config.log_backup_count), (10485760, 5))
```

- [ ] **Step 2: Run the tests and verify the new fields are missing**

Run: `.venv/bin/python -m unittest tests.test_config.ConfigTests.test_f0_orchestration_mode_defaults_to_legacy_direct tests.test_config.ConfigTests.test_f0_accepts_direct_and_rejects_unimplemented_modes tests.test_config.ConfigTests.test_log_rotation_values_must_be_positive -v`

Expected: FAIL because `Config` has no `orchestration_mode`, `log_max_bytes`, or `log_backup_count`.

- [ ] **Step 3: Implement exact F0 mode validation**

```python
def _get_f0_orchestration_mode(source: Mapping[str, str]) -> str:
    value = source.get("ORCHESTRATION_MODE", "legacy_direct").strip()
    if value not in ("legacy_direct", "direct"):
        raise ValueError("ORCHESTRATION_MODE must be legacy_direct or direct in F0")
    return value
```

Add the three immutable fields to `Config` and parse log values with the existing positive integer helper. Add the three keys to `.env.example` without changing production `.env`.

- [ ] **Step 4: Re-run configuration and adapter regression tests**

Run: `.venv/bin/python -m unittest tests.test_config tests.test_agent -v`

Expected: PASS; Gateway command construction and current Agent call count remain unchanged.

- [ ] **Step 5: Commit Task 1**

```bash
git add .env.example src/qq_openclaw_bridge/config.py tests/test_config.py
git commit -m "feat: validate F0 orchestration mode"
```

---

### Task 2: Add schema v5 orchestration repositories

**Files:**
- Create: `src/qq_openclaw_bridge/persistence/schema.py`
- Create: `src/qq_openclaw_bridge/persistence/orchestration.py`
- Modify: `src/qq_openclaw_bridge/persistence/database.py`
- Modify: `src/qq_openclaw_bridge/storage.py`
- Test: `tests/test_persistence_orchestration.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `OrchestrationRepository.create_run(...) -> dict`.
- Produces: `OrchestrationRepository.transition_run(run_id, expected, target, event_type, detail="", now=None) -> dict`.
- Produces: `OrchestrationRepository.append_event(...) -> dict`.
- Produces: `OrchestrationRepository.create_config_version(config: dict, activate: bool) -> dict`.
- Produces: `WorkspaceStateStore.orchestration_repository`.

- [ ] **Step 1: Write v4-to-v5 migration and state transition tests**

```python
def test_schema_v5_preserves_v4_and_creates_orchestration_tables(self):
    store = WorkspaceStateStore(self.database_path)
    self.assertEqual(self.metadata_value(store, "schema_version"), "5")
    self.assertEqual(
        self.table_names(store) & {
            "orchestration_runs", "orchestration_events",
            "orchestration_config_versions", "action_intents",
        },
        {"orchestration_runs", "orchestration_events",
         "orchestration_config_versions", "action_intents"},
    )

def test_transition_is_compare_and_swap_and_appends_event_atomically(self):
    run = self.repository.create_run(user_id=7, source_message_id="m1",
                                     session_key="s1", mode="direct",
                                     risk_level="normal", config_version=1)
    updated = self.repository.transition_run(run["run_id"], "received", "planned", "planned")
    self.assertEqual(updated["status"], "planned")
    with self.assertRaises(ValueError):
        self.repository.transition_run(run["run_id"], "received", "executing", "executing")

def test_activating_config_is_atomic_and_rejects_sensitive_fields(self):
    first = self.repository.create_config_version({"mode": "legacy_direct"}, activate=True)
    second = self.repository.create_config_version({"mode": "direct"}, activate=True)
    self.assertEqual(self.repository.active_config()["version"], second["version"])
    self.assertEqual(self.repository.get_config(first["version"])["status"], "retired")
    with self.assertRaises(ValueError):
        self.repository.create_config_version({"OPENAI_API_KEY": "secret"}, activate=True)
```

- [ ] **Step 2: Run the repository tests and verify missing tables**

Run: `.venv/bin/python -m unittest tests.test_persistence_orchestration -v`

Expected: FAIL because schema v5 and `OrchestrationRepository` do not exist.

- [ ] **Step 3: Extract schema initialization and create v5 tables**

Move the existing additive SQL from `database.py` into `schema.py` without changing v1-v4 table definitions. Set `SCHEMA_VERSION = "5"` and add:

```sql
CREATE TABLE IF NOT EXISTS orchestration_runs (
  run_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  source_message_id TEXT,
  session_key TEXT NOT NULL,
  mode TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL,
  config_version INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  fallback_reason TEXT,
  safe_error_class TEXT
);
CREATE TABLE IF NOT EXISTS orchestration_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  stage TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES orchestration_runs(run_id)
);
CREATE TABLE IF NOT EXISTS orchestration_config_versions (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  config_json TEXT NOT NULL,
  config_sha256 TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  activated_at TEXT
);
```

The `action_intents` table is created now for atomic schema v5 migration; Task 6 owns its repository and exact behavior.

- [ ] **Step 4: Implement repository CAS transactions and bounded queries**

Use the existing `SqliteDatabase.locked()` and `harden_files()` patterns. Validate allowed run transitions in Python and repeat the expected-state condition in SQL `WHERE run_id=? AND status=?`. Insert the event in the same connection transaction. Activating a configuration retires the previous active version and activates the new version in one transaction; a partial unique index or equivalent transaction invariant must prevent two active rows. Reject configuration keys matching secret/key/token/password/credential patterns before canonical JSON is persisted; only SecretRef identifiers may be stored.

- [ ] **Step 5: Re-run persistence tests**

Run: `.venv/bin/python -m unittest tests.test_persistence_orchestration tests.test_persistence_e4 tests.test_persistence_memory tests.test_storage -v`

Expected: PASS; v4 data remains readable and v5 files remain `0600`.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/qq_openclaw_bridge/persistence/schema.py src/qq_openclaw_bridge/persistence/orchestration.py src/qq_openclaw_bridge/persistence/database.py src/qq_openclaw_bridge/storage.py tests/test_persistence_orchestration.py tests/test_storage.py
git commit -m "feat: add orchestration schema v5"
```

---

### Task 3: Implement the Direct `OrchestrationRuntime`

**Files:**
- Create: `src/qq_openclaw_bridge/orchestration/config.py`
- Create: `src/qq_openclaw_bridge/orchestration/runtime.py`
- Modify: `src/qq_openclaw_bridge/orchestration/models.py`
- Modify: `src/qq_openclaw_bridge/orchestration/ports.py`
- Modify: `src/qq_openclaw_bridge/orchestration/__init__.py`
- Test: `tests/test_orchestration_runtime.py`
- Test: `tests/test_orchestration.py`

**Interfaces:**
- Consumes: existing `TaskContract`, `PlanningStrategy`, `ExecutionEngine`, `VerificationStrategy`, `BudgetPolicy`, and `CapabilityRegistry`.
- Produces: `OrchestrationRuntime.execute(contract: TaskContract) -> ExecutionResult`.
- Produces: `OrchestrationTraceSink.start(contract, mode, config_version) -> str`, `transition(...) -> None`, and `finish(...) -> None`.
- Produces: `OrchestrationConfig(mode: str, version: int)`.

- [ ] **Step 1: Write failing Runtime behavior tests**

```python
async def test_direct_runtime_calls_each_stage_once_and_returns_result(self):
    runtime = OrchestrationRuntime(
        planner=self.planner, executor=self.executor, verifier=self.verifier,
        budget_policy=self.budget, capability_registry=self.registry,
        trace_sink=self.trace, config=OrchestrationConfig("direct", 1),
    )
    result = await runtime.execute(TaskContract.from_request(self.request))
    self.assertEqual(result.text, "reply")
    self.assertEqual(self.events, ["budget", "capability", "plan", "execute", "verify"])

async def test_trace_failure_does_not_replace_a_valid_direct_result(self):
    self.trace.raise_on_transition = True
    result = await self.runtime.execute(TaskContract.from_request(self.request))
    self.assertEqual(result.text, "reply")

async def test_budget_rejection_never_calls_executor(self):
    self.budget.allowed = False
    with self.assertRaises(BudgetRejected):
        await self.runtime.execute(TaskContract.from_request(self.request))
    self.assertEqual(self.executor.calls, 0)
```

- [ ] **Step 2: Run tests and verify Runtime is undefined**

Run: `.venv/bin/python -m unittest tests.test_orchestration_runtime -v`

Expected: FAIL importing `OrchestrationRuntime`.

- [ ] **Step 3: Implement the minimal Runtime pipeline**

```python
class OrchestrationRuntime:
    async def execute(self, contract: TaskContract) -> ExecutionResult:
        run_id = self._safe_start(contract)
        if not self._budget_policy.allows(contract):
            self._safe_finish(run_id, "rejected", "budget_rejected")
            raise BudgetRejected("orchestration budget rejected")
        self._capability_registry.select(contract)
        plan = self._planner.plan(contract)
        result = await self._executor.execute(plan)
        report = self._verifier.verify(contract, result)
        if not report.accepted:
            self._safe_finish(run_id, "rejected", report.reason)
            raise VerificationRejected(report.reason or "result rejected")
        self._safe_finish(run_id, "accepted", "")
        return result
```

Only trace operations are wrapped by `_safe_*`; budget, planning, execution and verification errors retain explicit semantics.

- [ ] **Step 4: Run Runtime and existing orchestration tests**

Run: `.venv/bin/python -m unittest tests.test_orchestration_runtime tests.test_orchestration -v`

Expected: PASS; Direct executor still requires exactly one work item.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/qq_openclaw_bridge/orchestration tests/test_orchestration.py tests/test_orchestration_runtime.py
git commit -m "feat: add direct orchestration runtime"
```

---

### Task 4: Move production orchestration wiring into the composition root

**Files:**
- Modify: `src/qq_openclaw_bridge/application/coordinator.py`
- Create: `src/qq_openclaw_bridge/application/coordinator_factory.py`
- Modify: `src/qq_openclaw_bridge/runtime/composition.py`
- Modify: `src/qq_openclaw_bridge/bridge.py`
- Test: `tests/test_application_coordinator.py`
- Test: `tests/test_composition.py`
- Test: `tests/test_architecture.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Consumes: `OrchestrationRuntime.execute(TaskContract) -> ExecutionResult` from Task 3.
- Produces: `create_message_coordinator(config, agent_call, command_chain, ...) -> MessageCoordinator`.
- Produces: optional `message_coordinator` injection into `Bridge` with no concrete strategy imports in `bridge.py`.

- [ ] **Step 1: Add failing architecture and dual-mode composition tests**

```python
def test_bridge_does_not_import_or_construct_concrete_orchestration_strategies(self):
    source = (SRC / "bridge.py").read_text(encoding="utf-8")
    for name in ("DirectPlanningStrategy", "SingleAgentExecutionEngine",
                 "CompatibilityVerificationStrategy"):
        self.assertNotIn(name, source)

def test_direct_mode_injects_one_runtime_and_one_shared_agent(self):
    graph = build_application(self.config(ORCHESTRATION_MODE="direct"),
                              bridge_factory=self.capture_bridge)
    self.assertIsNotNone(graph.message_coordinator.orchestration_runtime)
    self.assertEqual(self.agent_factory_calls, 1)

def test_legacy_direct_mode_uses_compatibility_pipeline(self):
    graph = build_application(self.config(ORCHESTRATION_MODE="legacy_direct"),
                              bridge_factory=self.capture_bridge)
    self.assertIsNone(graph.message_coordinator.orchestration_runtime)
```

- [ ] **Step 2: Run focused tests and verify Bridge still owns strategies**

Run: `.venv/bin/python -m unittest tests.test_architecture tests.test_composition tests.test_application_coordinator -v`

Expected: FAIL on concrete strategy imports and missing injected coordinator.

- [ ] **Step 3: Add the coordinator factory and optional Runtime path**

Update `MessageCoordinator.resolve_prepared()` so model work uses exactly one of two mutually exclusive paths:

```python
if self._orchestration_runtime is not None:
    result = await self._orchestration_runtime.execute(contract)
else:
    plan = self._planner.plan(contract)
    result = await self._executor.execute(plan)
    report = self._verifier.verify(contract, result)
    if not report.accepted:
        raise RuntimeError(report.reason or "agent reply rejected")
```

Constructor validation must reject both paths being supplied simultaneously or neither path being supplied.

- [ ] **Step 4: Inject the production coordinator from `build_application()`**

Create the Agent once, create the command chain once, create the legacy or Runtime coordinator according to config, then pass it to `Bridge`. Remove concrete orchestration imports from `bridge.py`.

- [ ] **Step 5: Run coordinator, composition, Bridge and architecture regressions**

Run: `.venv/bin/python -m unittest tests.test_application_coordinator tests.test_composition tests.test_bridge tests.test_architecture -v`

Expected: PASS; existing direct Bridge construction remains available through the compatibility factory without concrete imports in `bridge.py`.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/qq_openclaw_bridge/application/coordinator.py src/qq_openclaw_bridge/application/coordinator_factory.py src/qq_openclaw_bridge/runtime/composition.py src/qq_openclaw_bridge/bridge.py tests/test_application_coordinator.py tests/test_composition.py tests/test_bridge.py tests/test_architecture.py
git commit -m "refactor: inject orchestration runtime"
```

---

### Task 5: Persist orchestration traces without changing replies

**Files:**
- Modify: `src/qq_openclaw_bridge/persistence/orchestration.py`
- Modify: `src/qq_openclaw_bridge/orchestration/runtime.py`
- Modify: `src/qq_openclaw_bridge/runtime/composition.py`
- Test: `tests/test_orchestration_runtime.py`
- Test: `tests/test_persistence_orchestration.py`
- Test: `tests/test_e4_observability.py`

**Interfaces:**
- Consumes: repository API from Task 2 and Runtime trace port from Task 3.
- Produces: `SqliteOrchestrationTraceSink` with bounded, sanitized event details.
- Produces: one run row per Runtime model request; local commands create no run.

- [ ] **Step 1: Write failing trace integration tests**

```python
async def test_runtime_persists_received_planned_executing_verifying_accepted(self):
    result = await self.runtime.execute(self.contract)
    run = self.repository.latest_run(user_id=self.contract.request.user_id)
    self.assertEqual(run["status"], "accepted")
    self.assertEqual(
        [event["stage"] for event in self.repository.events(run["run_id"])],
        ["received", "planned", "executing", "verifying", "accepted"],
    )

async def test_local_command_creates_no_orchestration_run(self):
    await self.coordinator.handle(self.identity_request, self.reply_sink)
    self.assertEqual(self.repository.list_runs(limit=10), [])
```

- [ ] **Step 2: Run tests and verify no SQLite trace sink exists**

Run: `.venv/bin/python -m unittest tests.test_orchestration_runtime tests.test_persistence_orchestration -v`

Expected: FAIL importing or constructing `SqliteOrchestrationTraceSink`.

- [ ] **Step 3: Implement sanitized trace sink**

Allow event details only from fixed identifiers and bounded error classes. Do not persist request text, response text, Prompt, media context, URL, path or exception message. A trace write exception is caught by Runtime and logged as `orchestration_trace_degraded`.

- [ ] **Step 4: Verify trace failures do not alter model results**

Run: `.venv/bin/python -m unittest tests.test_orchestration_runtime tests.test_persistence_orchestration tests.test_e4_observability -v`

Expected: PASS; a valid Agent response remains the final reply even when trace persistence raises.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/qq_openclaw_bridge/persistence/orchestration.py src/qq_openclaw_bridge/orchestration/runtime.py src/qq_openclaw_bridge/runtime/composition.py tests/test_orchestration_runtime.py tests/test_persistence_orchestration.py tests/test_e4_observability.py
git commit -m "feat: persist orchestration traces"
```

---

### Task 6: Add immutable ActionIntent domain and repository

**Files:**
- Create: `src/qq_openclaw_bridge/domain/actions.py`
- Create: `src/qq_openclaw_bridge/persistence/actions.py`
- Modify: `src/qq_openclaw_bridge/storage.py`
- Test: `tests/test_domain_actions.py`
- Test: `tests/test_persistence_actions.py`

**Interfaces:**
- Produces: `ActionIntent.create(...) -> ActionIntent`.
- Produces: `canonical_payload(payload: Mapping[str, object]) -> str` and `payload_sha256(...) -> str`.
- Produces: `ActionIntentRepository.propose(intent)`, `approve(...)`, `consume(...)`, `finish(...)`, and `cancel(...)`.
- Produces: `WorkspaceStateStore.action_intent_repository`.

- [ ] **Step 1: Write failing canonicalization and state-machine tests**

```python
def test_payload_hash_is_stable_across_mapping_order(self):
    left = payload_sha256({"command": "删除记忆 1", "index": 1})
    right = payload_sha256({"index": 1, "command": "删除记忆 1"})
    self.assertEqual(left, right)

def test_payload_rejects_float_bytes_and_nested_unknown_types(self):
    for payload in ({"x": 1.2}, {"x": b"raw"}, {"x": object()}):
        with self.assertRaises(ValueError):
            canonical_payload(payload)

def test_approved_intent_is_consumed_once_with_exact_owner_hash_and_version(self):
    intent = self.proposed_intent()
    self.repository.propose(intent)
    self.repository.approve(intent.intent_id, intent.approval_id, intent.user_id,
                            intent.payload_sha256, intent.config_version)
    consumed = self.repository.consume(intent.intent_id, intent.user_id,
                                       intent.payload_sha256, intent.config_version)
    self.assertEqual(consumed["status"], "executing")
    with self.assertRaises(ValueError):
        self.repository.consume(intent.intent_id, intent.user_id,
                               intent.payload_sha256, intent.config_version)

def test_expired_or_cross_user_intent_cannot_be_approved_or_consumed(self):
    expired = self.proposed_intent(expires_at=self.now - timedelta(seconds=1))
    self.repository.propose(expired)
    with self.assertRaises(ValueError):
        self.repository.approve(expired.intent_id, expired.approval_id,
                                expired.user_id, expired.payload_sha256,
                                expired.config_version, now=self.now)

    active = self.proposed_intent()
    self.repository.propose(active)
    with self.assertRaises(ValueError):
        self.repository.approve(active.intent_id, active.approval_id,
                                active.user_id + 1, active.payload_sha256,
                                active.config_version, now=self.now)
```

- [ ] **Step 2: Run tests and verify missing domain types**

Run: `.venv/bin/python -m unittest tests.test_domain_actions tests.test_persistence_actions -v`

Expected: FAIL importing `ActionIntent`.

- [ ] **Step 3: Implement strict canonical JSON and immutable dataclass**

Use UTF-8 JSON with `ensure_ascii=False`, `sort_keys=True`, compact separators, exact integers, booleans, strings, null, lists and string-key mappings only. Reject floats to avoid cross-runtime numeric canonicalization ambiguity.

```python
@dataclass(frozen=True)
class ActionIntent:
    intent_id: str
    approval_id: str
    user_id: int
    source_message_id: str
    session_key: str
    action_type: str
    adapter_id: str
    schema_version: int
    payload_json: str
    payload_sha256: str
    config_version: int
    risk_tier: str
    impact_summary: str
    created_at: datetime
    expires_at: datetime
```

- [ ] **Step 4: Implement repository CAS transitions**

Every mutation includes current status, owner, hash and config version in the SQL `WHERE`. Approval and consumption also require `expires_at > now`; cross-user access, expiry, payload/config drift and replay all fail closed. `consume()` transitions `approved -> executing` in one transaction. `finish()` only accepts `executing -> succeeded/failed`. Terminal states reject all mutation.

- [ ] **Step 5: Run domain, persistence and ownership regressions**

Run: `.venv/bin/python -m unittest tests.test_domain_actions tests.test_persistence_actions tests.test_persistence_approvals tests.test_storage -v`

Expected: PASS; malformed ownership types are rejected before SQL writes.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/qq_openclaw_bridge/domain/actions.py src/qq_openclaw_bridge/persistence/actions.py src/qq_openclaw_bridge/storage.py tests/test_domain_actions.py tests/test_persistence_actions.py
git commit -m "feat: add immutable action intents"
```

---

### Task 7: Migrate current destructive commands to ActionIntent

**Files:**
- Modify: `src/qq_openclaw_bridge/security.py`
- Modify: `src/qq_openclaw_bridge/application/coordinator.py`
- Modify: `src/qq_openclaw_bridge/runtime/composition.py`
- Modify: `src/qq_openclaw_bridge/persistence/approvals.py`
- Test: `tests/test_security.py`
- Test: `tests/test_application_coordinator.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Consumes: ActionIntent repository from Task 6.
- Produces: `SecurityDecision.intent_id`, `SecurityDecision.command`, and `SecurityDecision.execution_token` compatibility fields.
- Produces: exact mapping from destructive command grammar to fixed `action_type`, `adapter_id="local.housekeeper"`, and canonical payload.

- [ ] **Step 1: Add failing binding and tamper tests**

```python
def test_destructive_command_creates_hash_bound_intent(self):
    decision = self.guard.evaluate(7, "删除记忆 1", message_id="m1", session_key="s1")
    intent = self.repository.get_by_approval(decision.approval_id)
    self.assertEqual(intent["action_type"], "memory.delete")
    self.assertEqual(json.loads(intent["payload_json"]), {"index": 1})

def test_approval_rejects_payload_owner_and_config_tampering(self):
    pending = self.guard.evaluate(7, "删除待办 2", message_id="m2", session_key="s1")
    self.repository.force_test_mutation(pending.intent_id, payload_json='{"index":3}')
    decision = self.guard.evaluate(7, "确认执行 %s" % pending.approval_id)
    self.assertIsNone(decision.command)
    self.assertIn("重新发起", decision.reply)

async def test_local_action_finishes_succeeded_only_after_handler_returns(self):
    result = await self.coordinator.handle(self.confirm_request, self.reply_sink)
    self.assertTrue(result)
    self.assertEqual(self.repository.get(self.intent_id)["status"], "succeeded")
```

- [ ] **Step 2: Run security tests and verify raw-command approvals still exist**

Run: `.venv/bin/python -m unittest tests.test_security tests.test_application_coordinator -v`

Expected: FAIL because `ConfirmationGuard` does not create or consume ActionIntent.

- [ ] **Step 3: Implement fixed local action mappings**

Use explicit regex-to-payload functions for:

```python
LOCAL_ACTIONS = {
    "memory.delete": "local.housekeeper",
    "memory.clear": "local.housekeeper",
    "todo.delete": "local.housekeeper",
    "todo.completed.clear": "local.housekeeper",
    "style.delete": "local.style",
    "style.clear": "local.style",
}
```

Do not add a generic `shell.command`, `http.request`, or arbitrary text adapter.

- [ ] **Step 4: Bind approval and execution completion**

On confirmation, atomically approve and consume the exact intent, reconstruct the existing local command from fixed fields, execute the existing CommandChain, then mark succeeded or failed. If no handler accepts the approved action, mark failed and return a stable user message; never send it to the model.

- [ ] **Step 5: Run security, Bridge and local command regressions**

Run: `.venv/bin/python -m unittest tests.test_security tests.test_application_coordinator tests.test_bridge tests.test_housekeeper tests.test_style_memory -v`

Expected: PASS; existing Chinese approval wording remains compatible, but persisted approvals are hash-bound.

- [ ] **Step 6: Commit Task 7**

```bash
git add src/qq_openclaw_bridge/security.py src/qq_openclaw_bridge/application/coordinator.py src/qq_openclaw_bridge/runtime/composition.py src/qq_openclaw_bridge/persistence/approvals.py tests/test_security.py tests/test_application_coordinator.py tests/test_bridge.py
git commit -m "feat: bind approvals to action intents"
```

---

### Task 8: Add orchestration controls and observability to Dashboard

**Files:**
- Modify: `src/qq_openclaw_dashboard/queries.py`
- Modify: `src/qq_openclaw_dashboard/commands.py`
- Modify: `src/qq_openclaw_dashboard/controller.py`
- Modify: `src/qq_openclaw_dashboard/server.py`
- Modify: `src/qq_openclaw_dashboard/static/index.html`
- Modify: `src/qq_openclaw_dashboard/static/app.js`
- Modify: `src/qq_openclaw_dashboard/static/app.css`
- Create: `src/qq_openclaw_dashboard/static/orchestration.js`
- Test: `tests/test_dashboard_orchestration.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: bounded run/config queries from Task 2.
- Produces: `GET /api/state` fields `orchestration.mode`, `config_version`, `recent_runs`, `fallback_count`, `ops_status`.
- Produces: token-protected `POST /api/orchestration/mode` accepting only `legacy_direct` or `direct`.

- [ ] **Step 1: Write failing query, mutation and static UI tests**

```python
def test_state_exposes_bounded_sanitized_orchestration_runs(self):
    state = self.controller.get_state()["orchestration"]
    self.assertEqual(state["mode"], "direct")
    self.assertLessEqual(len(state["recent_runs"]), 100)
    self.assertNotIn("request_text", json.dumps(state, ensure_ascii=False))

def test_mode_mutation_accepts_only_f0_modes_and_requires_token(self):
    self.assertEqual(self.post("/api/orchestration/mode", {"mode": "direct"}, token=""), 403)
    self.assertEqual(self.post("/api/orchestration/mode", {"mode": "shadow"}), 400)
    self.assertEqual(self.post("/api/orchestration/mode", {"mode": "legacy_direct"}), 200)

def test_ui_has_orchestration_view_without_model_role_controls(self):
    html = self.index_html()
    self.assertIn("编排控制", html)
    self.assertNotIn("架构师模型", html)
```

- [ ] **Step 2: Run Dashboard tests and verify routes are missing**

Run: `.venv/bin/python -m unittest tests.test_dashboard_orchestration -v`

Expected: FAIL on missing state and endpoint.

- [ ] **Step 3: Implement bounded safe query and allowlisted mutation**

Reuse atomic `.env` writing and fixed key allowlist. Mutation writes only `ORCHESTRATION_MODE`, creates a config version, and returns “重启后生效”; it does not restart automatically.

- [ ] **Step 4: Build the orchestration view**

Show mode, active version, recent run status/duration/fallback, LaunchAgent state, logging state and backup encryption state. Use existing dashboard styles, 8px maximum card radius, horizontal table scroll on mobile, and no nested cards.

- [ ] **Step 5: Run Dashboard HTTP and static tests outside sandbox when required**

Run: `.venv/bin/python -m unittest tests.test_dashboard_orchestration tests.test_dashboard tests.test_dashboard_e4 -v`

Expected: PASS. If loopback bind is blocked by the tool sandbox, rerun the same command with approved unsandboxed execution; do not skip the HTTP assertions.

- [ ] **Step 6: Commit Task 8**

```bash
git add src/qq_openclaw_dashboard tests/test_dashboard.py tests/test_dashboard_orchestration.py
git commit -m "feat: add orchestration dashboard controls"
```

---

### Task 9: Add safe rotating process logs

**Files:**
- Create: `src/qq_openclaw_bridge/runtime/logging.py`
- Modify: `src/qq_openclaw_bridge/__main__.py`
- Modify: `src/qq_openclaw_dashboard/__main__.py`
- Modify: `src/qq_openclaw_watchdog/__main__.py`
- Modify: `src/qq_openclaw_bridge/config.py`
- Test: `tests/test_runtime_logging.py`
- Test: `tests/test_scripts.py`

**Interfaces:**
- Produces: `configure_process_logging(component: str, log_dir: Path, level: str, max_bytes: int, backup_count: int) -> logging.Logger`.
- Produces: JSONL fields `timestamp`, `component`, `level`, `message`, and optional safe IDs.

- [ ] **Step 1: Write failing rotation, permission and redaction tests**

```python
def test_logging_rotates_at_configured_size_and_hardens_files(self):
    logger = configure_process_logging("bridge", self.log_dir, "info", 512, 2)
    for index in range(100):
        logger.info("line-%03d %s", index, "x" * 40)
    self.assertLessEqual(len(list(self.log_dir.glob("bridge.log*"))), 3)
    for path in self.log_dir.glob("bridge.log*"):
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

def test_formatter_redacts_known_sensitive_fields(self):
    record = self.format_record(extra={"api_key": "secret", "run_id": "run-1"})
    self.assertNotIn("secret", record)
    self.assertIn("run-1", record)
```

- [ ] **Step 2: Run tests and verify logging module is missing**

Run: `.venv/bin/python -m unittest tests.test_runtime_logging -v`

Expected: FAIL importing `configure_process_logging`.

- [ ] **Step 3: Implement `RotatingFileHandler` with fixed fields**

Use `logging.handlers.RotatingFileHandler(maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")`. Do not serialize arbitrary `record.__dict__`; copy only approved identifiers. Harden the active file after configuration and rotated files after rollover.

- [ ] **Step 4: Configure logging before application composition**

Each `__main__` determines its project log directory from a fixed CLI/project path, configures logging, then loads full application state. Startup exceptions are logged with a safe error class before exiting nonzero.

- [ ] **Step 5: Run logging and entrypoint regressions**

Run: `.venv/bin/python -m unittest tests.test_runtime_logging tests.test_scripts tests.test_dashboard_services tests.test_watchdog -v`

Expected: PASS; no test observes a raw secret or world-readable log file.

- [ ] **Step 6: Commit Task 9**

```bash
git add src/qq_openclaw_bridge/runtime/logging.py src/qq_openclaw_bridge/__main__.py src/qq_openclaw_dashboard/__main__.py src/qq_openclaw_watchdog/__main__.py src/qq_openclaw_bridge/config.py tests/test_runtime_logging.py tests/test_scripts.py
git commit -m "feat: rotate and harden process logs"
```

---

### Task 10: Encrypt new backups with AES-256-GCM

**Files:**
- Create: `src/qq_openclaw_bridge/runtime/backup_crypto.py`
- Modify: `src/qq_openclaw_bridge/backups.py`
- Modify: `src/qq_openclaw_bridge/runtime/secrets.py`
- Modify: `src/qq_openclaw_dashboard/queries.py`
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Test: `tests/test_backup_crypto.py`
- Test: `tests/test_backups.py`

**Interfaces:**
- Produces: `BackupEnvelope.encrypt(plaintext: bytes, key: bytes) -> bytes` and `decrypt(ciphertext: bytes, key: bytes) -> bytes`.
- Produces: `BackupKeyProvider.get_or_create() -> bytes` using fixed Keychain service `qq-openclaw-backup` and account `backup-master-v1`.
- Produces: encrypted backup suffix `.chichu-backup` and explicit legacy ZIP classification.

- [ ] **Step 1: Add dependency consistency and crypto behavior tests**

```python
def test_envelope_round_trip_and_random_nonce(self):
    key = bytes(range(32))
    first = BackupEnvelope.encrypt(b"payload", key)
    second = BackupEnvelope.encrypt(b"payload", key)
    self.assertNotEqual(first, second)
    self.assertEqual(BackupEnvelope.decrypt(first, key), b"payload")

def test_tamper_and_wrong_key_fail_closed(self):
    encrypted = bytearray(BackupEnvelope.encrypt(b"payload", b"a" * 32))
    encrypted[-1] ^= 1
    with self.assertRaises(BackupIntegrityError):
        BackupEnvelope.decrypt(bytes(encrypted), b"a" * 32)
    with self.assertRaises(BackupIntegrityError):
        BackupEnvelope.decrypt(BackupEnvelope.encrypt(b"payload", b"a" * 32), b"b" * 32)

def test_requirements_and_pyproject_include_same_pdf_and_crypto_bounds(self):
    self.assertDependency("pypdf>=4,<7")
    self.assertDependency("cryptography>=42,<47")
```

- [ ] **Step 2: Run crypto and backup tests and verify missing dependency/module**

Run: `.venv/bin/python -m unittest tests.test_backup_crypto tests.test_backups -v`

Expected: FAIL importing `BackupEnvelope` or `cryptography` in the development venv.

- [ ] **Step 3: Synchronize dependencies and install in the development venv**

Add both exact bounds to `pyproject.toml` and `requirements.txt`, then run:

```bash
.venv/bin/python -m pip install -e .
```

Verify: `.venv/bin/python -c 'import cryptography,pypdf; print(cryptography.__version__, pypdf.__version__)'`

- [ ] **Step 4: Implement the versioned AEAD envelope**

Use fixed magic `b"CHICHU-BACKUP\x00"`, version byte `1`, 12-byte random nonce, and `AESGCM(key).encrypt(nonce, plaintext, header)`. Decrypt validates magic and version before AESGCM verification. Map all cryptographic failures to `BackupIntegrityError` without leaking details.

- [ ] **Step 5: Integrate Keychain and encrypted backup lifecycle**

Inject the key provider in tests. Production invokes `/usr/bin/security` with fixed arguments and `shell=False`. Build the ZIP in a `0600` temporary file, read bytes, encrypt, write a `0600` temporary encrypted file, atomically replace final output, then delete the plaintext temporary file in `finally`.

Legacy `.zip` files remain listable as `legacy-unencrypted`; new automatic/manual/pre-restore backups use `.chichu-backup`. Automatic backup aborts on encryption failure.

- [ ] **Step 6: Run backup, Dashboard and restore regressions**

Run: `.venv/bin/python -m unittest tests.test_backup_crypto tests.test_backups tests.test_dashboard tests.test_dashboard_services -v`

Expected: PASS; encrypted files do not begin with ZIP magic and tampering is rejected before manifest parsing.

- [ ] **Step 7: Commit Task 10**

```bash
git add pyproject.toml requirements.txt src/qq_openclaw_bridge/runtime/backup_crypto.py src/qq_openclaw_bridge/runtime/secrets.py src/qq_openclaw_bridge/backups.py src/qq_openclaw_dashboard/queries.py tests/test_backup_crypto.py tests/test_backups.py
git commit -m "feat: encrypt managed backups"
```

---

### Task 11: Replace nohup ownership with managed LaunchAgents

**Files:**
- Create: `launchd/com.chichu.qq-bridge.plist.template`
- Create: `launchd/com.chichu.qq-dashboard.plist.template`
- Create: `launchd/com.chichu.qq-watchdog.plist.template`
- Create: `scripts/install_launch_agents.py`
- Modify: `scripts/bridge_service.sh`
- Modify: `scripts/dashboard_service.sh`
- Modify: `scripts/watchdog_service.sh`
- Modify: `apps/启动QQ智能体.app/Contents/MacOS/start`
- Modify: `apps/停止QQ智能体.app/Contents/MacOS/stop`
- Test: `tests/test_launchd.py`
- Test: `tests/test_scripts.py`

**Interfaces:**
- Produces: `render_launch_agents(project_dir: Path, home_dir: Path) -> tuple[Path, ...]`.
- Produces: labels `com.chichu.qq-bridge`, `com.chichu.qq-dashboard`, `com.chichu.qq-watchdog`.
- Service scripts preserve `start|stop|restart|status` CLI while delegating to launchctl.

- [ ] **Step 1: Write failing plist rendering and script ownership tests**

```python
def test_rendered_plists_use_fixed_absolute_programs_and_loopback_dashboard(self):
    paths = render_launch_agents(self.project_dir, self.home_dir)
    values = [plistlib.loads(path.read_bytes()) for path in paths]
    self.assertEqual({item["Label"] for item in values}, EXPECTED_LABELS)
    for item in values:
        self.assertTrue(Path(item["WorkingDirectory"]).is_absolute())
        self.assertNotIn("/bin/sh -c", " ".join(item["ProgramArguments"]))

def test_service_scripts_do_not_use_nohup_or_background_ampersand(self):
    for name in ("bridge_service.sh", "dashboard_service.sh", "watchdog_service.sh"):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        self.assertNotIn("nohup", source)
        self.assertIn("launchctl", source)
```

- [ ] **Step 2: Run tests and verify LaunchAgent files are missing**

Run: `.venv/bin/python -m unittest tests.test_launchd tests.test_scripts -v`

Expected: FAIL importing installer helpers and finding `nohup` in service scripts.

- [ ] **Step 3: Render plist safely with `plistlib`**

Templates contain data fields only. The installer resolves project and Python executable paths, validates all paths are absolute and within the expected project/home locations, writes temporary plist files with `0600`, validates with `plutil -lint`, then atomically installs under `~/Library/LaunchAgents`.

Bridge and Watchdog use `KeepAlive` with bounded restart behavior; Dashboard uses `RunAtLoad`. All set `WorkingDirectory`, a minimal PATH, and `/dev/null` for stdout/stderr because Task 9 owns application logs.

- [ ] **Step 4: Convert service scripts to launchctl compatibility wrappers**

Use fixed labels and current GUI domain `gui/$(id -u)`. `start` uses bootstrap if absent then `kickstart -k`; `stop` uses bootout; `status` uses `launchctl print`. PID files become informational snapshots only and are never process ownership sources.

- [ ] **Step 5: Preserve double-click app behavior**

The existing Apps continue to call the service scripts with fixed arguments. They must not invoke `kill -9`, start Python directly, or create a second Bridge.

- [ ] **Step 6: Run launchd and script tests**

Run: `.venv/bin/python -m unittest tests.test_launchd tests.test_scripts tests.test_dashboard_services tests.test_watchdog -v`

Expected: PASS with a fake launchctl runner; no test writes to the real `~/Library/LaunchAgents`.

- [ ] **Step 7: Commit Task 11**

```bash
git add launchd scripts/install_launch_agents.py scripts/bridge_service.sh scripts/dashboard_service.sh scripts/watchdog_service.sh apps tests/test_launchd.py tests/test_scripts.py
git commit -m "feat: manage services with launchd"
```

---

### Task 12: Full equivalence, deployment documentation and production acceptance

**Files:**
- Create: `tests/test_f0_acceptance.py`
- Modify: `tests/test_e4_acceptance.py`
- Modify: `scripts/check_ready.sh`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-02-f0-orchestration-control-plane-foundation-design.md` only if implementation reveals a factual correction
- Create: `.superpowers/sdd/f0-progress.md`

**Interfaces:**
- Consumes: all Task 1-11 production APIs.
- Produces: repeatable Direct equivalence harness, readiness output, deployment checklist and rollback evidence.

- [ ] **Step 1: Write failing end-to-end Direct equivalence tests**

```python
async def test_legacy_and_runtime_direct_are_behaviorally_equivalent(self):
    for request in self.fixed_corpus():
        legacy = await self.run_mode("legacy_direct", request)
        direct = await self.run_mode("direct", request)
        self.assertEqual(direct.reply, legacy.reply)
        self.assertEqual(direct.agent_calls, legacy.agent_calls)
        self.assertEqual(direct.outbox_rows, legacy.outbox_rows)
        self.assertEqual(direct.inbound_status, legacy.inbound_status)

async def test_direct_mode_calls_agent_once_for_plain_text_and_zero_for_local_commands(self):
    plain = await self.run_mode("direct", self.request("你好"))
    identity = await self.run_mode("direct", self.request("你是谁"))
    self.assertEqual(plain.agent_calls, 1)
    self.assertEqual(identity.agent_calls, 0)
```

The fixed corpus must include identity, memory, todo, reminder, style, approval, normal text, long reply, timeout, proxy error, duplicate message, Outbox retry, E3 context and E4-disabled media notice.

- [ ] **Step 2: Run acceptance tests and fix only observed equivalence differences**

Run: `.venv/bin/python -m unittest tests.test_f0_acceptance tests.test_e4_acceptance -v`

Expected before final wiring: FAIL on missing mode harness or trace state. After minimal corrections: PASS with zero reply/call/outbox differences.

- [ ] **Step 3: Extend readiness checks**

`scripts/check_ready.sh` must print:

```text
[INFO] orchestration mode: legacy_direct|direct
[INFO] orchestration config version: <integer>
[INFO] orchestration active backlog: <integer>
[INFO] launchd bridge/dashboard/watchdog: healthy|unhealthy
[INFO] backup encryption: configured|unconfigured
```

Normal readiness must not call a model. Add an explicit `--smoke-orchestration` that performs one paid Direct model call only when requested and verifies no embedded Gateway fallback.

- [ ] **Step 4: Update Chinese operations documentation**

Document mode meanings, ActionIntent semantics, Dashboard page, Keychain backup key, LaunchAgent install/uninstall, log locations, full test command, deployment sequence, rollback command and the fact that F1 routing remains unimplemented.

- [ ] **Step 5: Run the complete development suite outside sandbox where needed**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`

Expected: all 596 baseline tests plus every F0 test PASS. Loopback HTTP and PDF worker tests must be rerun outside the restricted sandbox if the sandbox denies required OS primitives.

- [ ] **Step 6: Create and verify the first encrypted pre-deploy backup**

Run through the project backup API, then verify:

- file mode is `0600`;
- file magic is not ZIP;
- AEAD and manifest validation succeed;
- SQLite is included;
- raw `*_API_KEY` values and media cache are absent.

- [ ] **Step 7: Deploy in `legacy_direct` and install LaunchAgents**

Preserve production `.env`, workspace and database. Install code/dependencies, render plists, bootstrap one job per component, and verify exactly one Bridge, Dashboard and Watchdog process exists. Do not enable E4 or F1.

- [ ] **Step 8: Execute production smoke and switch to `direct`**

Verify OneBot, Gateway, proxy, SQLite, queues, Dashboard and watchdog. Send one allowed QQ text message and one local identity command. Switch to `direct`, restart Bridge, repeat both messages, and compare reply/call/outbox evidence.

- [ ] **Step 9: Execute the 60-second rollback drill**

Switch to `legacy_direct`, restart through LaunchAgent, and verify readiness plus one QQ text reply within 60 seconds. Record timestamps and results in `.superpowers/sdd/f0-progress.md`.

- [ ] **Step 10: Run final production verification**

Run the production venv full suite, `PRAGMA quick_check`, schema version check, file permission scan, ActionIntent tamper test, queue counts, backup decrypt verification, process uniqueness and dashboard state secret scan.

- [ ] **Step 11: Commit Task 12**

```bash
git add README.md scripts/check_ready.sh tests/test_f0_acceptance.py tests/test_e4_acceptance.py .superpowers/sdd/f0-progress.md docs/superpowers/specs/2026-08-02-f0-orchestration-control-plane-foundation-design.md
git commit -m "docs: verify F0 orchestration rollout"
```

---

## Final Completion Checklist

- [ ] Production baseline 596 tests and all new F0 tests pass.
- [ ] `Bridge` contains no concrete planner/executor/verifier construction.
- [ ] `legacy_direct` and `direct` fixed-corpus reply, model-call and Outbox differences are zero.
- [ ] Schema v5 preserves all v4 data and `PRAGMA quick_check` is `ok`.
- [ ] ActionIntent payload, owner, approval, hash, version and terminal-state protections pass tamper tests.
- [ ] Dashboard shows only safe orchestration state and only permits F0 modes.
- [ ] Logs rotate at 10 MiB with five backups and `0600` permissions.
- [ ] New backups are AES-256-GCM encrypted and legacy ZIPs are labeled accurately.
- [ ] Exactly one LaunchAgent-managed Bridge, Dashboard and Watchdog are running.
- [ ] Real QQ Direct smoke and 60-second rollback drill pass.
- [ ] E4 remains disabled and F1 `shadow/active` modes remain unavailable.
