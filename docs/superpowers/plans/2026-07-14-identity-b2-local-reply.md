# Identity B2 Local Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make identity questions about chichu return a stable local persona reply without calling OpenClaw.

**Architecture:** Add a small local identity responder module and call it from `Bridge.handle_event` before housekeeper, style memory, and model adapter handling. Keep the reply deterministic and short.

**Tech Stack:** Python 3.9 standard library, existing unittest suite, current OneBot bridge flow.

## Global Constraints

- B2 only handles local identity Q&A; do not expand feedback learning or style inspection commands.
- Identity replies must not call the model adapter.
- Identity replies must include `chichu`, `本地 QQ 私聊管家`, and a concise toxic-housekeeper tone.
- Non-identity messages must continue through the existing bridge path.

---

### Task 1: Identity Responder Module

**Files:**
- Create: `src/qq_openclaw_bridge/identity.py`
- Create: `tests/test_identity.py`

**Interfaces:**
- Produces `identity_reply(text: str) -> Optional[str]`.

- [ ] **Step 1: Write failing tests**

Add tests covering:
- `你是谁`
- `介绍一下你自己`
- `chichu 是谁`
- normal messages return `None`.

- [ ] **Step 2: Run identity tests and verify RED**

Run: `python3 -m unittest tests.test_identity -q`

Expected: FAIL because `qq_openclaw_bridge.identity` does not exist.

- [ ] **Step 3: Implement module**

Add normalized exact/near-exact matching and deterministic reply text.

- [ ] **Step 4: Run identity tests and verify GREEN**

Run: `python3 -m unittest tests.test_identity -q`

Expected: OK.

---

### Task 2: Bridge Integration

**Files:**
- Modify: `src/qq_openclaw_bridge/bridge.py`
- Modify: `tests/test_bridge.py`
- Modify: `README.md`

**Interfaces:**
- `Bridge.handle_event` calls `identity_reply(request_data.text)` before housekeeper/style/adapter.

- [ ] **Step 1: Write failing bridge test**

Add a test asserting an identity question sends the local reply and does not call the agent adapter.

- [ ] **Step 2: Run bridge tests and verify RED**

Run: `python3 -m unittest tests.test_bridge -q`

Expected: FAIL because bridge currently calls the adapter.

- [ ] **Step 3: Integrate local identity reply**

Import `identity_reply` in `bridge.py` and call it before housekeeper handling.

- [ ] **Step 4: Update README**

Document local identity questions under the B section.

- [ ] **Step 5: Run bridge tests and verify GREEN**

Run: `python3 -m unittest tests.test_bridge -q`

Expected: OK.

---

### Task 3: Runtime Sync And Verification

**Files:**
- Copy changed files to `${HOME}/qq-chat-bot`.

- [ ] **Step 1: Run full tests**

Run: `python3 -m unittest discover -s tests -q`

Expected: OK.

- [ ] **Step 2: Sync runtime files**

Run:

```bash
ditto src/qq_openclaw_bridge/identity.py ${HOME}/qq-chat-bot/src/qq_openclaw_bridge/identity.py
ditto src/qq_openclaw_bridge/bridge.py ${HOME}/qq-chat-bot/src/qq_openclaw_bridge/bridge.py
ditto README.md ${HOME}/qq-chat-bot/README.md
```

- [ ] **Step 3: Restart runtime bridge**

Run: `/bin/bash ${HOME}/qq-chat-bot/scripts/bridge_service.sh restart`

Expected: service starts and connects to OneBot.

---

## Self-Review

- Spec coverage: B2 identity local reply is covered by Tasks 1-3.
- Placeholder scan: no vague or deferred implementation remains.
- Type consistency: `identity_reply(text: str) -> Optional[str]` is the only new public interface.
