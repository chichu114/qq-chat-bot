# Style Feedback B3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand chichu's style feedback learning so common tone corrections are persisted into `STYLE.md`.

**Architecture:** Extend the existing `StyleFeedbackRule` table in `style_memory.py`. Keep the same storage path and `## 用户反馈沉淀` section so B3 remains compatible with existing style memory.

**Tech Stack:** Python 3.9 standard library, existing unittest suite, Markdown style memory.

## Global Constraints

- B3 only expands style feedback recognition; do not implement B4 style inspection commands.
- Do not call OpenClaw for feedback messages.
- Keep deduplication by exact normalized feedback text.
- Preserve existing `STYLE.md` structure and `## 用户反馈沉淀` behavior.

---

### Task 1: Expand Style Feedback Rules

**Files:**
- Modify: `src/qq_openclaw_bridge/style_memory.py`
- Modify: `tests/test_style_memory.py`

**Interfaces:**
- Existing `StyleMemory.record_if_feedback(text: str) -> Optional[str]` recognizes new markers:
  - `太凶了`
  - `太油了`
  - `太冷了`
  - `更毒舌一点`
  - `少自我介绍`
  - `更像管家`

- [ ] **Step 1: Write failing tests**

Add tests for each new feedback family. Each test should assert:
- reply is not `None`;
- feedback text is appended to `STYLE.md`;
- guidance contains the expected stable phrase.

- [ ] **Step 2: Run style tests and verify RED**

Run: `python3 -m unittest tests.test_style_memory -q`

Expected: FAIL because several markers are not recognized.

- [ ] **Step 3: Implement rules**

Add six `StyleFeedbackRule` entries before broad positive-style matching:
- harshness reduction;
- oily/flirty reduction;
- warmth increase;
- sharper toxic-housekeeper style;
- reduced self-introduction;
- stronger housekeeper framing.

- [ ] **Step 4: Run style tests and verify GREEN**

Run: `python3 -m unittest tests.test_style_memory -q`

Expected: OK.

---

### Task 2: Docs, Runtime Sync, And Verification

**Files:**
- Modify: `README.md`
- Copy changed runtime files to `${HOME}/qq-chat-bot`.

- [ ] **Step 1: Update README**

Document B3 feedback examples:
- `太凶了`
- `太油了`
- `太冷了`
- `更毒舌一点`
- `少自我介绍`
- `更像管家`

- [ ] **Step 2: Run full tests**

Run: `python3 -m unittest discover -s tests -q`

Expected: OK.

- [ ] **Step 3: Sync runtime files**

Run:

```bash
ditto src/qq_openclaw_bridge/style_memory.py ${HOME}/qq-chat-bot/src/qq_openclaw_bridge/style_memory.py
ditto README.md ${HOME}/qq-chat-bot/README.md
```

- [ ] **Step 4: Restart runtime bridge using the desktop app path**

Use the desktop app start path after syncing, because the Codex shell can clean up child background processes.

---

## Self-Review

- Spec coverage: B3 is limited to feedback rule expansion and runtime sync.
- Placeholder scan: no deferred implementation remains.
- Type consistency: no new public interface is introduced.
