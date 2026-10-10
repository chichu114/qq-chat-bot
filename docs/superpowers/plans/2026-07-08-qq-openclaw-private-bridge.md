# QQ OpenClaw 私聊桥接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个本地可运行的 QQ 私聊桥接服务，让 NapCatQQ 的 OneBot v11 私聊消息可以转发给 OpenClaw 智能体并把文本结果回发到 QQ。

**Architecture:** Python 项目分为纯逻辑核心、OpenClaw 适配器和 OneBot WebSocket 运行时三层。纯逻辑核心负责消息解析、白名单和限流；适配器负责调用 echo 或 HTTP 形式的 OpenClaw；运行时只负责连接 WebSocket、调度请求和发送回复。

**Tech Stack:** Python 3.9+、标准库 `unittest`、运行时依赖 `websockets`、HTTP OpenClaw 适配使用标准库 `urllib.request`。

## Global Constraints

- 第一版只支持私聊文本消息。
- 只响应 `ALLOWED_QQ_USERS` 白名单中的 QQ 用户。
- 默认不启用本地 shell 执行、文件删除、支付、群发或广播能力。
- `.env` 和 `.env.*` 不进入版本控制。
- OpenClaw 入口通过配置选择：`AGENT_ADAPTER=echo` 或 `AGENT_ADAPTER=http`。
- OneBot 入口使用 `ONEBOT_WS_URL`。
- 长回复默认截断到 `MAX_REPLY_CHARS`，默认 1800 字符。

---

## File Structure

- `pyproject.toml`：项目元数据和测试命令入口。
- `requirements.txt`：运行桥接服务需要的第三方依赖。
- `.env.example`：本地配置示例，不包含真实账号或密钥。
- `README.md`：本地启动和验证说明。
- `src/qq_openclaw_bridge/__init__.py`：包版本。
- `src/qq_openclaw_bridge/config.py`：从环境变量加载配置。
- `src/qq_openclaw_bridge/models.py`：内部请求、响应和判定结果模型。
- `src/qq_openclaw_bridge/onebot.py`：OneBot v11 事件解析和发送动作构造。
- `src/qq_openclaw_bridge/rate_limit.py`：每用户滑动窗口限流。
- `src/qq_openclaw_bridge/filters.py`：私聊、文本、白名单和限流判定。
- `src/qq_openclaw_bridge/agent.py`：echo 与 HTTP OpenClaw 适配器。
- `src/qq_openclaw_bridge/bridge.py`：WebSocket 事件循环、重连和消息处理。
- `src/qq_openclaw_bridge/__main__.py`：命令行启动入口。
- `tests/test_config.py`：配置加载测试。
- `tests/test_onebot_filters.py`：OneBot 解析、白名单和限流测试。
- `tests/test_agent.py`：echo 和 HTTP 适配器测试。
- `tests/test_bridge.py`：桥接处理流程测试。

## Task 1: 配置、OneBot 解析和过滤核心

**Files:**
- Create: `pyproject.toml`
- Create: `src/qq_openclaw_bridge/__init__.py`
- Create: `src/qq_openclaw_bridge/config.py`
- Create: `src/qq_openclaw_bridge/models.py`
- Create: `src/qq_openclaw_bridge/onebot.py`
- Create: `src/qq_openclaw_bridge/rate_limit.py`
- Create: `src/qq_openclaw_bridge/filters.py`
- Create: `tests/test_config.py`
- Create: `tests/test_onebot_filters.py`

**Interfaces:**
- Produces: `Config.from_env(env: Mapping[str, str] | None = None) -> Config`
- Produces: `extract_text(message: object) -> str | None`
- Produces: `build_send_private_msg(user_id: int, message: str, echo: str | None = None) -> dict[str, object]`
- Produces: `RateLimiter.allow(key: str, now: float | None = None) -> bool`
- Produces: `evaluate_event(event: Mapping[str, object], allowed_users: set[int], rate_limiter: RateLimiter) -> MessageDecision`

- [x] **Step 1: Write failing tests**

Create tests that cover: default config values, comma-separated QQ whitelist parsing, text extraction from OneBot string messages, text extraction from OneBot segment arrays, group messages ignored, non-whitelisted private messages ignored, whitelisted private text accepted, and rate limit rejection.

- [x] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_config tests.test_onebot_filters -v`

Expected: FAIL because `qq_openclaw_bridge` modules do not exist yet.

- [x] **Step 3: Implement minimal core**

Implement the files listed for this task with dataclasses for config and decision models, pure functions for OneBot parsing, and a simple per-user sliding-window rate limiter.

- [x] **Step 4: Run tests and verify GREEN**

Run: `python3 -m unittest tests.test_config tests.test_onebot_filters -v`

Expected: all tests pass with `OK`.

- [x] **Step 5: Commit**

Run:

```bash
git add pyproject.toml src/qq_openclaw_bridge tests/test_config.py tests/test_onebot_filters.py
git commit -m "feat: add QQ message filtering core"
```

## Task 2: OpenClaw 适配器

**Files:**
- Create: `src/qq_openclaw_bridge/agent.py`
- Create: `tests/test_agent.py`

**Interfaces:**
- Consumes: `AgentRequest` and `AgentResponse` from `src/qq_openclaw_bridge/models.py`
- Produces: `EchoAgentAdapter.reply(request: AgentRequest) -> AgentResponse`
- Produces: `HttpAgentAdapter.reply(request: AgentRequest) -> AgentResponse`
- Produces: `create_agent_adapter(config: Config) -> AgentAdapter`

- [x] **Step 1: Write failing tests**

Create tests that cover: echo adapter includes the incoming text, HTTP adapter sends JSON containing `user_id`, `message_id`, `text`, and `timestamp`, HTTP adapter accepts response fields named `reply`, `text`, or `message`, and unsupported adapter names raise `ValueError`.

- [x] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_agent -v`

Expected: FAIL because `src/qq_openclaw_bridge/agent.py` does not exist yet.

- [x] **Step 3: Implement minimal adapter layer**

Implement an `AgentAdapter` protocol, `EchoAgentAdapter`, `HttpAgentAdapter`, response parsing, timeout handling, and adapter factory selection from `Config.agent_adapter`.

- [x] **Step 4: Run tests and verify GREEN**

Run: `python3 -m unittest tests.test_agent -v`

Expected: all tests pass with `OK`.

- [x] **Step 5: Commit**

Run:

```bash
git add src/qq_openclaw_bridge/agent.py tests/test_agent.py
git commit -m "feat: add OpenClaw agent adapters"
```

## Task 3: 桥接运行时

**Files:**
- Create: `src/qq_openclaw_bridge/bridge.py`
- Create: `src/qq_openclaw_bridge/__main__.py`
- Create: `tests/test_bridge.py`

**Interfaces:**
- Consumes: `evaluate_event(...)`, `build_send_private_msg(...)`, and `AgentAdapter.reply(...)`
- Produces: `Bridge.handle_event(event: Mapping[str, object]) -> bool`
- Produces: `Bridge.run_forever() -> None`
- Produces: module command `python3 -m qq_openclaw_bridge`

- [x] **Step 1: Write failing tests**

Create tests with fake WebSocket and fake adapter that cover: accepted private messages send one reply action, ignored messages do not send actions, OpenClaw errors send the configured fallback reply, and long replies are truncated with a suffix.

- [x] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_bridge -v`

Expected: FAIL because `src/qq_openclaw_bridge/bridge.py` does not exist yet.

- [x] **Step 3: Implement bridge runtime**

Implement `Bridge.handle_event` as an async method for one event, plus `run_forever` with WebSocket connection, JSON parsing, send action serialization, logging, reconnect backoff, and import-time error guidance if `websockets` is not installed.

- [x] **Step 4: Run tests and verify GREEN**

Run: `python3 -m unittest tests.test_bridge -v`

Expected: all tests pass with `OK`.

- [x] **Step 5: Commit**

Run:

```bash
git add src/qq_openclaw_bridge/bridge.py src/qq_openclaw_bridge/__main__.py tests/test_bridge.py
git commit -m "feat: add OneBot bridge runtime"
```

## Task 4: 本地运行文档和配置示例

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `README.md`
- Modify: `docs/superpowers/specs/2026-07-08-qq-openclaw-private-agent-design.md`

**Interfaces:**
- Consumes: all runtime environment variables from `Config`
- Produces: copyable local startup steps for NapCatQQ, OpenClaw, and the bridge

- [x] **Step 1: Write documentation artifacts**

Create `.env.example` with safe dummy values, `requirements.txt` with `websockets`, and `README.md` with local startup, smoke test, and risk notes.

- [x] **Step 2: Verify documentation references real commands**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass with `OK`.

- [x] **Step 3: Commit**

Run:

```bash
git add requirements.txt .env.example README.md docs/superpowers/specs/2026-07-08-qq-openclaw-private-agent-design.md
git commit -m "docs: add QQ OpenClaw bridge startup guide"
```

## Self-Review

- Spec coverage: private chat, whitelist, text-only behavior, rate limit, logging path, fallback reply, response truncation, echo adapter, HTTP OpenClaw adapter, and local startup docs are covered.
- Placeholder scan: no unresolved placeholder markers are present in this plan.
- Type consistency: `Config`, `AgentRequest`, `AgentResponse`, `MessageDecision`, `RateLimiter`, `AgentAdapter`, and `Bridge` names are used consistently across tasks.
