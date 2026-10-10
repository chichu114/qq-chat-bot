# E2 Reliable Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move QQ agent turns to the persistent OpenClaw Gateway and make inbound processing and outbound delivery non-blocking, ordered, idempotent, and durable.

**Architecture:** Keep the synchronous adapter interface at the process boundary, invoke it from bounded worker threads, and serialize each QQ user's turns with an async lock. Extend the E1 SQLite store with inbound claims, a durable outbox, and runtime metrics used by the bridge, dashboard, and watchdog.

**Tech Stack:** Python 3.9, asyncio, sqlite3 WAL, websockets, OpenClaw 2026.6.11 CLI/Gateway, unittest.

## Global Constraints

- Preserve all A-E1 commands, persona files, reminder behavior, and E1 sandbox permissions.
- Keep Gateway, dashboard, and OneBot connections bound to localhost.
- Write tests before each production behavior and observe the expected failure.
- Do not add third-party Python dependencies.

---

### Task 1: Gateway-backed adapter and validated settings

**Files:** `tests/test_agent.py`, `tests/test_config.py`, `src/qq_openclaw_bridge/agent.py`, `src/qq_openclaw_bridge/config.py`, `.env.example`

- [ ] Add failing tests proving Gateway mode omits `--local` and local fallback mode retains it.
- [ ] Add failing tests for every E2 integer setting and default.
- [ ] Implement adapter/config changes and run the focused tests.

### Task 2: Durable transport state

**Files:** `tests/test_storage.py`, `src/qq_openclaw_bridge/storage.py`

- [ ] Add failing tests for inbound claim dedupe, stale reclaim, durable outbox dedupe, retry scheduling, sent state, and metrics.
- [ ] Migrate the schema transactionally and implement the minimal store API.
- [ ] Run storage tests and verify WAL/file permissions remain intact.

### Task 3: Async ordered bridge

**Files:** `tests/test_bridge.py`, `src/qq_openclaw_bridge/bridge.py`, `src/qq_openclaw_bridge/__main__.py`

- [ ] Add failing tests proving the reader can dispatch while an agent call is slow.
- [ ] Add failing tests proving same-user serialization and cross-user concurrency.
- [ ] Add failing tests for duplicate inbound events, retry, circuit breaker, and outbox recovery.
- [ ] Implement bounded tasks, worker-thread agent calls, user locks, reliable delivery, and clean cancellation.

### Task 4: Reminder reliability and observability

**Files:** `tests/test_bridge.py`, `tests/test_dashboard.py`, `tests/test_watchdog.py`, `src/qq_openclaw_dashboard/controller.py`, `src/qq_openclaw_dashboard/static/app.js`, `src/qq_openclaw_watchdog/watchdog.py`

- [ ] Add failing tests for reminder outbox dedupe and post-crash state completion.
- [ ] Add failing dashboard and watchdog tests for queue metrics and Gateway reachability.
- [ ] Implement the focused behavior and run all related tests.

### Task 5: Readiness, docs, deployment, and live verification

**Files:** `tests/test_scripts.py`, `scripts/check_ready.sh`, `README.md`, runtime deployment files

- [ ] Test and update readiness checks so Gateway mode verifies Gateway health and uses the configured agent for smoke tests.
- [ ] Run the complete test suite, syntax checks, and diff checks.
- [ ] Back up, sync to `${HOME}/qq-chat-bot`, preserve runtime `.env`, and restart services.
- [ ] Verify Gateway, bridge, OneBot, SQLite queues, dashboard, and one real QQ agent turn.
