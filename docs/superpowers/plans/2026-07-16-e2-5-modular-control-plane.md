# E2.5 模块化控制平面实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保持所有现有行为和单模型调用方式不变的前提下，把 QQ 智能体重构为可测试、可替换的模块，并建立 A++ 多模型控制平面的稳定接口。

**Architecture:** 使用兼容外观逐步提取职责。`Bridge` 最终委托事件调度、消息协调、可靠投递和后台服务；`Housekeeper`、`WorkspaceStateStore`、`DashboardController` 继续保留旧 API，但内部委托领域服务与 Repository。E2.5 只启用 `DirectPlanningStrategy + SingleAgentExecutionEngine`，不启用双模型议会或 DAG 并行。

**Tech Stack:** Python 3.9、asyncio、typing.Protocol、dataclasses、sqlite3 WAL、websockets、unittest、原生 HTML/CSS/JavaScript。

## Global Constraints

- 所有 E2 及之前功能、回复文案、环境变量、HTTP 路径和 SQLite schema v2 保持兼容。
- 双模型议会、智能四级路由、DAG 并行和 Token 预算在 E2.5 只定义接口，默认不启用且不得增加模型调用次数。
- 领域与编排契约不得导入 WebSocket、SQLite、OpenClaw CLI、HTTP Server 或 macOS 服务脚本。
- `Bridge`、`Housekeeper`、`WorkspaceStateStore`、`DashboardController` 保留兼容入口。
- 每次只迁移一个已被测试覆盖的职责；业务变化必须停止并另立功能设计。
- 当前工作区包含用户与前序阶段的未提交改动；不要暂存、提交、回退或覆盖不属于本任务的改动。
- 手工编辑使用 `apply_patch`；批量格式化和纯机械移动可使用对应工具。

---

## File Map

### New orchestration and application modules

- `src/qq_openclaw_bridge/orchestration/models.py`：TaskContract、ExecutionPlan、WorkItem、ExecutionResult、VerificationReport。
- `src/qq_openclaw_bridge/orchestration/ports.py`：PlanningStrategy、ExecutionEngine、VerificationStrategy、BudgetPolicy、CapabilityRegistry、BrainstormGate、OrchestrationTraceSink。
- `src/qq_openclaw_bridge/orchestration/direct.py`：E2.5 当前单模型兼容实现。
- `src/qq_openclaw_bridge/application/commands.py`：有序命令处理链。
- `src/qq_openclaw_bridge/application/coordinator.py`：单条消息用例。
- `src/qq_openclaw_bridge/application/dispatch.py`：容量、任务和每用户顺序。
- `src/qq_openclaw_bridge/application/delivery.py`：可靠回复与 Outbox drain。
- `src/qq_openclaw_bridge/application/background.py`：提醒、备份和指标后台服务。
- `src/qq_openclaw_bridge/runtime/composition.py`：集中装配依赖。
- `src/qq_openclaw_bridge/runtime/supervisor.py`：WebSocket 生命周期。

### New persistence modules

- `src/qq_openclaw_bridge/persistence/database.py`：连接、锁、schema 初始化和权限。
- `src/qq_openclaw_bridge/persistence/documents.py`：文档仓储。
- `src/qq_openclaw_bridge/persistence/approvals.py`：审批仓储。
- `src/qq_openclaw_bridge/persistence/messaging.py`：入站、Outbox 和指标仓储。

### New domain modules

- `src/qq_openclaw_bridge/domain/models.py`：MemoryItem、TodoDraft、TodoItem。
- `src/qq_openclaw_bridge/domain/memory.py`：记忆解析与操作。
- `src/qq_openclaw_bridge/domain/todos.py`：待办编解码与操作。
- `src/qq_openclaw_bridge/domain/scheduling.py`：日期时间、重复提醒和改期。
- `src/qq_openclaw_bridge/domain/housekeeper.py`：命令路由与服务编排。

### Dashboard and watchdog modules

- `src/qq_openclaw_dashboard/queries.py`：只读状态组装。
- `src/qq_openclaw_dashboard/commands.py`：受限修改与服务命令。
- `src/qq_openclaw_watchdog/probes.py`：独立健康探针。

### Compatibility files to modify

- `src/qq_openclaw_bridge/bridge.py`
- `src/qq_openclaw_bridge/housekeeper.py`
- `src/qq_openclaw_bridge/storage.py`
- `src/qq_openclaw_bridge/__main__.py`
- `src/qq_openclaw_dashboard/controller.py`
- `src/qq_openclaw_watchdog/watchdog.py`
- `README.md`

---

### Task 1: 冻结行为基线并建立 A++ 核心契约

**Files:**
- Create: `tests/test_orchestration.py`
- Create: `src/qq_openclaw_bridge/orchestration/__init__.py`
- Create: `src/qq_openclaw_bridge/orchestration/models.py`
- Create: `src/qq_openclaw_bridge/orchestration/ports.py`
- Create: `src/qq_openclaw_bridge/orchestration/direct.py`
- Modify: `tests/test_bridge.py`

**Interfaces:**
- Produces: `TaskContract.from_request(request: AgentRequest) -> TaskContract`
- Produces: `DirectPlanningStrategy.plan(contract: TaskContract) -> ExecutionPlan`
- Produces: `SingleAgentExecutionEngine.execute(plan: ExecutionPlan) -> ExecutionResult`
- Produces: `CompatibilityVerificationStrategy.verify(contract, result) -> VerificationReport`

- [ ] **Step 1: Add characterization tests for handler priority**

Add a bridge test whose security guard, identity handler path, housekeeper, style handler and adapter can be observed. Assert the first matching local handler prevents all later handlers and the adapter from running. Preserve the order `security -> identity -> housekeeper -> style command -> style feedback -> model`.

- [ ] **Step 2: Run the characterization test**

Run: `.venv/bin/python -m unittest tests.test_bridge -v`

Expected: PASS before production changes.

- [ ] **Step 3: Add failing orchestration contract tests**

```python
class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_plan_preserves_request_and_calls_executor_once(self):
        request = AgentRequest(user_id=1, message_id="m1", text="你好", timestamp=1.0)
        contract = TaskContract.from_request(request)
        plan = DirectPlanningStrategy().plan(contract)
        calls = []

        async def reply_call(actual):
            calls.append(actual)
            return AgentResponse(text="回复")

        result = await SingleAgentExecutionEngine(reply_call).execute(plan)
        self.assertEqual(result.text, "回复")
        self.assertEqual(calls, [request])

    def test_compatibility_verifier_rejects_empty_reply(self):
        report = CompatibilityVerificationStrategy().verify(
            TaskContract.from_request(_request()), ExecutionResult(text="")
        )
        self.assertFalse(report.accepted)
```

- [ ] **Step 4: Run the new tests and verify RED**

Run: `.venv/bin/python -m unittest tests.test_orchestration -v`

Expected: FAIL because `qq_openclaw_bridge.orchestration` does not exist.

- [ ] **Step 5: Implement immutable orchestration models and Protocols**

Use frozen dataclasses and Python 3.9-compatible `Tuple` types. `TaskContract.from_request` must preserve the exact `AgentRequest`; `DirectPlanningStrategy` creates one `WorkItem` named `direct`; `SingleAgentExecutionEngine` accepts an async reply callable and rejects plans with anything other than one item.

- [ ] **Step 6: Implement compatibility verification and inert extension ports**

`CompatibilityVerificationStrategy` accepts non-empty text and returns a reason for empty text. Define Protocols for budget, capability, brainstorm and trace without creating model calls or persistence.

- [ ] **Step 7: Run Task 1 tests**

Run: `.venv/bin/python -m unittest tests.test_orchestration tests.test_bridge -v`

Expected: all selected tests PASS and the adapter call count remains one.

- [ ] **Step 8: Record checkpoint**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; no unrelated file is staged or reverted.

---

### Task 2: 提取有序命令链和消息协调器

**Files:**
- Create: `tests/test_application_commands.py`
- Create: `tests/test_application_coordinator.py`
- Create: `src/qq_openclaw_bridge/application/__init__.py`
- Create: `src/qq_openclaw_bridge/application/commands.py`
- Create: `src/qq_openclaw_bridge/application/coordinator.py`
- Modify: `src/qq_openclaw_bridge/bridge.py`

**Interfaces:**
- Consumes: Task 1 orchestration contracts and compatibility implementations.
- Produces: `CommandHandler.handle(request: AgentRequest) -> Optional[str]`
- Produces: `CommandChain.handle(request: AgentRequest) -> Optional[str]`
- Produces: `MessageCoordinator.handle(request, reply_sink) -> bool`

- [ ] **Step 1: Write failing command-chain tests**

Test strict ordering, first-match short circuit and `None` fallthrough using three fake handlers. Assert exceptions are not swallowed.

- [ ] **Step 2: Write failing coordinator tests**

Cover local reply bypassing planning, model reply passing through direct planning/execution/verification, and model exception mapping through the existing fallback callable.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_application_commands tests.test_application_coordinator -v`

Expected: import failures for the new application modules.

- [ ] **Step 4: Implement CommandChain**

```python
class CommandChain:
    def __init__(self, handlers: Sequence[CommandHandler]) -> None:
        self._handlers = tuple(handlers)

    def handle(self, request: AgentRequest) -> Optional[str]:
        for handler in self._handlers:
            reply = handler.handle(request)
            if reply is not None:
                return reply
        return None
```

Provide `CallableCommandHandler` for existing identity, housekeeper and style functions.

- [ ] **Step 5: Implement MessageCoordinator**

The coordinator accepts injected security interceptor, command chain, contract factory, planner, executor, verifier, fallback mapper, truncator and async reply sink. It must not import OneBot or SQLite.

- [ ] **Step 6: Convert Bridge's claimed-request method into delegation**

Build compatibility handlers from existing objects in the constructor. Keep `_handle_claimed_request` as a thin call into `MessageCoordinator`; preserve its public behavior for existing tests.

- [ ] **Step 7: Run application and bridge tests**

Run: `.venv/bin/python -m unittest tests.test_application_commands tests.test_application_coordinator tests.test_bridge -v`

Expected: all PASS; local commands still produce zero model calls.

- [ ] **Step 8: Record checkpoint**

Run: `git diff --check && .venv/bin/python -m unittest tests.test_agent tests.test_security -v`

Expected: PASS and no whitespace errors.

---

### Task 3: 提取事件调度与可靠投递

**Files:**
- Create: `tests/test_dispatch.py`
- Create: `tests/test_delivery.py`
- Create: `src/qq_openclaw_bridge/application/dispatch.py`
- Create: `src/qq_openclaw_bridge/application/delivery.py`
- Modify: `src/qq_openclaw_bridge/bridge.py`

**Interfaces:**
- Produces: `EventDispatcher.dispatch(user_id, operation) -> asyncio.Task`
- Produces: `EventDispatcher.wait() -> None`
- Produces: `ReliableDeliveryService.send_reply(websocket, request, message) -> None`
- Produces: `ReliableDeliveryService.deliver_once(websocket) -> bool`

- [ ] **Step 1: Add failing EventDispatcher tests**

Reuse the E2 concurrency expectations: dispatch returns before a slow operation finishes, same-user operations serialize, different users overlap, and capacity is released after exceptions.

- [ ] **Step 2: Add failing ReliableDeliveryService tests**

Cover no-store direct send, Outbox enqueue with `reply:<message_id>`, successful inbound completion, send failure retry scheduling and sent-row dedupe.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_dispatch tests.test_delivery -v`

Expected: import failures.

- [ ] **Step 4: Implement EventDispatcher**

Move capacity semaphore, per-user locks and task tracking from `Bridge`. The done callback must consume task exceptions through logging rather than leaving unobserved task warnings.

- [ ] **Step 5: Implement ReliableDeliveryService**

Move Outbox methods without changing dedupe keys, retry configuration, echo values, OneBot action shape or inbound completion timing.

- [ ] **Step 6: Delegate Bridge methods**

Keep `dispatch_event`, `wait_for_event_tasks`, `_send_reply_reliably` and `deliver_outbox_once` as compatibility methods that call the extracted services.

- [ ] **Step 7: Run delivery and bridge tests**

Run: `.venv/bin/python -m unittest tests.test_dispatch tests.test_delivery tests.test_bridge tests.test_storage -v`

Expected: PASS with the same logged retry messages as before.

- [ ] **Step 8: Record checkpoint**

Run: `git diff --check`

Expected: no whitespace errors.

---

### Task 4: 提取后台服务和 WebSocket 运行时

**Files:**
- Create: `tests/test_background_services.py`
- Create: `tests/test_runtime_supervisor.py`
- Create: `src/qq_openclaw_bridge/application/background.py`
- Create: `src/qq_openclaw_bridge/runtime/__init__.py`
- Create: `src/qq_openclaw_bridge/runtime/supervisor.py`
- Modify: `src/qq_openclaw_bridge/bridge.py`

**Interfaces:**
- Produces: `ReminderPushService.run_once(websocket, now=None) -> None`
- Produces: `BackupService.run_once() -> None`
- Produces: `WebSocketSupervisor.run_forever() -> None`

- [ ] **Step 1: Write failing background-service tests**

Move existing reminder and backup behavioral assertions to service-level tests, including durable reminder Outbox and mark-sent ordering.

- [ ] **Step 2: Write failing supervisor tests**

Use a fake connection factory to assert reconnect backoff reset, connection-scoped task cancellation, JSON filtering and event dispatch delegation.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_background_services tests.test_runtime_supervisor -v`

Expected: import failures.

- [ ] **Step 4: Implement ReminderPushService and BackupService**

Move the existing logic exactly, preserving dedupe keys, quiet-hours behavior, interval values and log messages.

- [ ] **Step 5: Implement WebSocketSupervisor**

Inject connection factory, event callback, outbox loop factory, reminder loop factory and logger. Keep exponential reconnect delay from 1 to 30 seconds.

- [ ] **Step 6: Reduce Bridge.run_forever to runtime delegation**

Leave compatibility loop methods where tests require them, but move ownership of connection lifecycle to the supervisor.

- [ ] **Step 7: Run background/runtime regression tests**

Run: `.venv/bin/python -m unittest tests.test_background_services tests.test_runtime_supervisor tests.test_bridge tests.test_reminders tests.test_backups -v`

Expected: PASS.

- [ ] **Step 8: Record checkpoint**

Run: `git diff --check`

Expected: no whitespace errors.

---

### Task 5: 拆分 SQLite 数据库基础与文档/审批仓储

**Files:**
- Create: `tests/test_persistence_documents.py`
- Create: `tests/test_persistence_approvals.py`
- Create: `src/qq_openclaw_bridge/persistence/__init__.py`
- Create: `src/qq_openclaw_bridge/persistence/database.py`
- Create: `src/qq_openclaw_bridge/persistence/documents.py`
- Create: `src/qq_openclaw_bridge/persistence/approvals.py`
- Modify: `src/qq_openclaw_bridge/storage.py`

**Interfaces:**
- Produces: `SqliteDatabase.locked()` and `SqliteDatabase.connect()`.
- Produces: `DocumentRepository.ensure/read/write`.
- Produces: `ApprovalRepository.save/get_pending/delete`.

- [ ] **Step 1: Add repository contract tests**

Run each repository against a temporary SQLite file. Assert WAL mode, schema version 2, Markdown atomic export, pending approval replacement and database/file mode `0600`.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_persistence_documents tests.test_persistence_approvals -v`

Expected: import failures.

- [ ] **Step 3: Extract SqliteDatabase**

Move process lock, thread lock, connection pragmas, schema initialization, timestamp helpers and hardening. Preserve `BEGIN IMMEDIATE` semantics and stale Outbox reset logic.

- [ ] **Step 4: Extract DocumentRepository and ApprovalRepository**

Move SQL and Markdown export behavior without changing table names, column names, timestamps or return dictionaries.

- [ ] **Step 5: Convert WorkspaceStateStore methods to delegation**

Construct repositories from one shared `SqliteDatabase`. Keep every old method signature unchanged.

- [ ] **Step 6: Run persistence and existing storage/security tests**

Run: `.venv/bin/python -m unittest tests.test_persistence_documents tests.test_persistence_approvals tests.test_storage tests.test_security tests.test_backups -v`

Expected: PASS and schema version remains `2`.

- [ ] **Step 7: Record checkpoint**

Run: `git diff --check`

Expected: no whitespace errors.

---

### Task 6: 拆分消息、Outbox 和指标仓储

**Files:**
- Create: `tests/test_persistence_messaging.py`
- Create: `src/qq_openclaw_bridge/persistence/messaging.py`
- Modify: `src/qq_openclaw_bridge/storage.py`

**Interfaces:**
- Produces: `InboundRepository.claim/complete/fail`.
- Produces: `OutboxRepository.enqueue/get/claim_due/mark_sent/mark_retry`.
- Produces: `RuntimeMetricRepository.set/transport_stats`.

- [ ] **Step 1: Add failing messaging repository tests**

Cover active/completed duplicate claims, stale reclaim, Outbox dedupe, exponential retry, failed terminal state, stale sending recovery and transport statistics.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_persistence_messaging -v`

Expected: import failure.

- [ ] **Step 3: Extract repositories using the shared SqliteDatabase**

Preserve transaction boundaries, status strings, retry cap at 3600 seconds and current dictionary fields.

- [ ] **Step 4: Delegate remaining WorkspaceStateStore methods**

`WorkspaceStateStore` must contain no raw SQL after this task except compatibility initialization if required by old construction tests.

- [ ] **Step 5: Run persistence, delivery and watchdog tests**

Run: `.venv/bin/python -m unittest tests.test_persistence_messaging tests.test_storage tests.test_delivery tests.test_bridge tests.test_dashboard tests.test_watchdog -v`

Expected: PASS.

- [ ] **Step 6: Record checkpoint**

Run: `/usr/bin/sqlite3 /tmp/e25-check.sqlite3 'PRAGMA quick_check;'` only if a test-created database is retained; otherwise rely on repository tests. Run `git diff --check`.

Expected: quick check `ok` when applicable; no whitespace errors.

---

### Task 7: 拆分 Housekeeper 的模型、记忆和待办编解码

**Files:**
- Create: `tests/test_domain_memory.py`
- Create: `tests/test_domain_todos.py`
- Create: `src/qq_openclaw_bridge/domain/__init__.py`
- Create: `src/qq_openclaw_bridge/domain/models.py`
- Create: `src/qq_openclaw_bridge/domain/memory.py`
- Create: `src/qq_openclaw_bridge/domain/todos.py`
- Modify: `src/qq_openclaw_bridge/housekeeper.py`
- Modify: `src/qq_openclaw_dashboard/controller.py`

**Interfaces:**
- Produces: immutable `MemoryItem`, `TodoDraft`, `TodoItem`.
- Produces: pure memory parse/replace helpers.
- Produces: pure todo parse/format/replace helpers.

- [ ] **Step 1: Add pure-domain tests from existing fixtures**

Copy representative Markdown compatibility cases from `tests/test_housekeeper.py` and `tests/test_dashboard.py`. Include legacy metadata, unknown fields, completed items and stable IDs.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_domain_memory tests.test_domain_todos -v`

Expected: import failures.

- [ ] **Step 3: Move dataclasses and pure codecs**

The new modules must not import `Path`, SQLite or state store classes. Keep old private helper names re-exported from `housekeeper.py` while Dashboard still imports them.

- [ ] **Step 4: Update Housekeeper and Dashboard imports**

Use new domain functions internally while preserving compatibility imports such as `_parse_todos`, `_extract_memory_items`, `_clean_todo_task_text` and `_parse_metadata_datetime`.

- [ ] **Step 5: Run domain, housekeeper and dashboard tests**

Run: `.venv/bin/python -m unittest tests.test_domain_memory tests.test_domain_todos tests.test_housekeeper tests.test_dashboard -v`

Expected: PASS with byte-compatible Markdown output for fixtures.

- [ ] **Step 6: Record checkpoint**

Run: `git diff --check`

Expected: no whitespace errors.

---

### Task 8: 拆分时间解析与 Housekeeper 命令服务

**Files:**
- Create: `tests/test_domain_scheduling.py`
- Create: `tests/test_domain_housekeeper.py`
- Create: `src/qq_openclaw_bridge/domain/scheduling.py`
- Create: `src/qq_openclaw_bridge/domain/housekeeper.py`
- Modify: `src/qq_openclaw_bridge/housekeeper.py`
- Modify: `src/qq_openclaw_bridge/reminders.py`

**Interfaces:**
- Produces: scheduling parse/recurrence functions with current signatures.
- Produces: `HousekeeperService.handle(text, user_id=None) -> Optional[str]`.

- [ ] **Step 1: Add scheduling characterization tests**

Cover absolute dates, tomorrow/weekday hints, daily/weekly/monthly recurrence, invalid dates, quiet-hour-independent due parsing and leap/month-end behavior already supported.

- [ ] **Step 2: Add HousekeeperService routing tests**

Assert command precedence and exact replies for memory, todos, reminders, pause/resume and unknown text.

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_domain_scheduling tests.test_domain_housekeeper -v`

Expected: import failures.

- [ ] **Step 4: Move scheduling helpers**

Re-export old helper names from `housekeeper.py` so reminders and dashboard remain compatible during migration.

- [ ] **Step 5: Implement HousekeeperService and thin facade**

Move command routing and operations into the domain service with injected document access. Keep `Housekeeper` constructor and `create_housekeeper` as adapters for paths/state store.

- [ ] **Step 6: Run all housekeeper/reminder/dashboard tests**

Run: `.venv/bin/python -m unittest tests.test_domain_scheduling tests.test_domain_housekeeper tests.test_housekeeper tests.test_reminders tests.test_dashboard tests.test_bridge -v`

Expected: PASS.

- [ ] **Step 7: Record checkpoint**

Run: `git diff --check`

Expected: no whitespace errors.

---

### Task 9: 拆分 Dashboard 查询/命令与 Watchdog probes

**Files:**
- Create: `tests/test_dashboard_services.py`
- Create: `tests/test_watchdog_probes.py`
- Create: `src/qq_openclaw_dashboard/queries.py`
- Create: `src/qq_openclaw_dashboard/commands.py`
- Create: `src/qq_openclaw_watchdog/probes.py`
- Modify: `src/qq_openclaw_dashboard/controller.py`
- Modify: `src/qq_openclaw_watchdog/watchdog.py`

**Interfaces:**
- Produces: `DashboardQueryService.get_state() -> Dict[str, object]`.
- Produces: `DashboardCommandService` mutation/service/backup methods.
- Produces: `Probe.run() -> bool` implementations for bridge, OneBot, proxy, Gateway and queue.

- [ ] **Step 1: Add failing service/probe tests**

Assert query service never invokes service scripts; command service enforces the existing action whitelist; each probe can be tested independently and returns a boolean without changing watchdog state.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_dashboard_services tests.test_watchdog_probes -v`

Expected: import failures.

- [ ] **Step 3: Extract Dashboard services**

Move `get_state` and helper reads to the query service. Move mutations, service commands and backups to the command service. Keep HTTP payload shape, token checks and controller methods unchanged.

- [ ] **Step 4: Extract watchdog probes**

Move only probe logic; keep failure counters, restart cooldown, notification and state-file writes in `Watchdog`.

- [ ] **Step 5: Run dashboard/watchdog regressions**

Run: `.venv/bin/python -m unittest tests.test_dashboard_services tests.test_watchdog_probes tests.test_dashboard tests.test_watchdog -v`

Expected: PASS.

- [ ] **Step 6: Record checkpoint**

Run: `git diff --check`

Expected: no whitespace errors.

---

### Task 10: 集中装配、架构约束、文档与实机部署

**Files:**
- Create: `tests/test_architecture.py`
- Create: `tests/test_composition.py`
- Create: `src/qq_openclaw_bridge/runtime/composition.py`
- Modify: `src/qq_openclaw_bridge/__main__.py`
- Modify: `src/qq_openclaw_bridge/bridge.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `build_application(config: Config) -> Bridge` or equivalent runtime facade used by `__main__.py`.

- [ ] **Step 1: Add failing architecture tests**

Parse imports with `ast` and reject infrastructure imports from `qq_openclaw_bridge.domain` and `qq_openclaw_bridge.orchestration.models/ports`. Add a composition test using fake factories to prove one shared state store is injected into security, housekeeper, reminders, delivery and backups.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_architecture tests.test_composition -v`

Expected: FAIL until composition root exists and forbidden imports are removed.

- [ ] **Step 3: Implement composition root**

Move object construction from `__main__.py` into factory functions. `__main__.py` should only load config, configure logging, build the application and run it.

- [ ] **Step 4: Reduce compatibility facades**

Ensure `Bridge`, `Housekeeper`, `WorkspaceStateStore` and `DashboardController` delegate extracted responsibilities. Do not delete methods required by compatibility tests.

- [ ] **Step 5: Update README**

Add an E2.5 section describing module boundaries, the active direct strategy, dormant A++ ports, extension rules and the fact that no extra API usage is introduced.

- [ ] **Step 6: Run static verification**

Run:

```bash
bash -n scripts/*.sh
PYTHONPYCACHEPREFIX=/tmp/chichu-e25-pycache .venv/bin/python -m compileall -q src tests
${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --check src/qq_openclaw_dashboard/static/app.js
git diff --check
```

Expected: exit code 0 with no output except intentional command output.

- [ ] **Step 7: Run the complete test suite**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: all old and new tests PASS; zero failures and errors.

- [ ] **Step 8: Review the full diff against the design**

Confirm every new module has one responsibility, no E3 behavior is accidentally active, model call count is unchanged, schema remains v2 and no existing public entry point was removed.

- [ ] **Step 9: Create a runtime backup and deploy**

Create a manual backup through the existing `DashboardController`, sync only reviewed source/docs/scripts to `${HOME}/qq-chat-bot`, preserve runtime `.env`, and verify permissions remain `0600`.

- [ ] **Step 10: Restart via desktop launchers and perform live acceptance**

Use the existing macOS launchers so processes survive the Codex command session. Verify Bridge, Dashboard and Watchdog remain alive; SQLite `PRAGMA quick_check` is `ok`; schema version is `2`; dashboard API is reachable; queue has no failed rows; Gateway smoke does not use embedded fallback; recent logs contain no `ERROR` or traceback.

- [ ] **Step 11: Record final checkpoint**

Run: `git status --short` and list only E2.5 files changed in this implementation. Do not stage or commit without explicit user instruction.
