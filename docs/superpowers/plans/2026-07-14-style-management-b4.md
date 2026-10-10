# Style Management B4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local QQ commands for viewing, checking, deleting, and clearing chichu style feedback entries in `STYLE.md`.

**Architecture:** Extend `StyleMemory` with `handle_command(text: str) -> Optional[str>` and keep `STYLE.md` parsing/writing inside that module. Update `Bridge.handle_event` to route style-management commands before passive feedback recording and before OpenClaw.

**Tech Stack:** Python 3.9 standard library, unittest, Markdown files.

## Global Constraints

- Do not call OpenClaw for B4 style-management commands.
- Preserve all non-feedback content in `STYLE.md`, including B1 persona and style sections.
- Only modify list entries under `## 用户反馈沉淀`.
- Keep B3 passive feedback behavior unchanged.

---

### Task 1: Add StyleMemory Command Tests And API

**Files:**
- Modify: `tests/test_style_memory.py`
- Modify: `src/qq_openclaw_bridge/style_memory.py`

**Interfaces:**
- Produces: `StyleMemory.handle_command(text: str) -> Optional[str>`
- Consumes: existing `STYLE_MEMORY_SECTION` and feedback line format.

- [ ] **Step 1: Write failing tests**

Add tests that create a temporary `STYLE.md` with two feedback entries and assert:
- `handle_command("查看风格")` returns numbered feedback lines;
- `handle_command("风格自检")` returns the total count and latest feedback;
- `handle_command("删除风格 1")` removes only the first feedback entry;
- `handle_command("清空风格反馈")` removes all feedback entries but keeps other file content;
- unknown text returns `None`.

- [ ] **Step 2: Run style tests and verify RED**

Run: `python3 -m unittest tests.test_style_memory -q`

Expected: FAIL because `StyleMemory.handle_command` does not exist.

- [ ] **Step 3: Implement command handling**

Add:
- `_extract_feedback_entries(content: str) -> list[str]`
- `_replace_feedback_entries(content: str, entries: Sequence[str]) -> str`
- `StyleMemory.handle_command(text: str) -> Optional[str>`

- [ ] **Step 4: Run style tests and verify GREEN**

Run: `python3 -m unittest tests.test_style_memory -q`

Expected: OK.

---

### Task 2: Route B4 Commands Through Bridge

**Files:**
- Modify: `tests/test_bridge.py`
- Modify: `src/qq_openclaw_bridge/bridge.py`

**Interfaces:**
- Consumes: `StyleMemory.handle_command(text: str) -> Optional[str>`

- [ ] **Step 1: Write failing bridge test**

Add a fake style memory with `handle_command` and assert `查看风格` sends a local reply without calling the agent.

- [ ] **Step 2: Run bridge tests and verify RED**

Run: `python3 -m unittest tests.test_bridge -q`

Expected: FAIL because bridge does not call `handle_command`.

- [ ] **Step 3: Implement bridge routing**

Add a `_handle_style_command(text: str) -> Optional[str]` helper and call it after housekeeper, before `_record_style_feedback`.

- [ ] **Step 4: Run bridge tests and verify GREEN**

Run: `python3 -m unittest tests.test_bridge -q`

Expected: OK.

---

### Task 3: Docs, Runtime Sync, And Verification

**Files:**
- Modify: `README.md`
- Copy runtime files to `${HOME}/qq-chat-bot`.

- [ ] **Step 1: Update README**

Document:
- `查看风格`
- `风格沉淀`
- `风格自检`
- `删除风格 1`
- `清空风格反馈`

- [ ] **Step 2: Run full tests**

Run: `python3 -m unittest discover -s tests -q`

Expected: OK.

- [ ] **Step 3: Sync runtime files**

Copy `style_memory.py`, `bridge.py`, and `README.md` to `${HOME}/qq-chat-bot`.

- [ ] **Step 4: Restart and verify**

Restart through the desktop app path and confirm `bridge_service.sh status` reports a running PID.

## Self-Review

- Spec coverage: all approved B4 commands are included.
- Placeholder scan: no deferred behavior remains.
- Type consistency: `StyleMemory.handle_command` is used consistently by bridge and tests.
