# Persona B1 Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make chichu's core persona reproducible and harder to drift by installing canonical B1 sections into OpenClaw workspace files.

**Architecture:** Add a small Python persona installer module in the bridge package. The module owns canonical B1 sections for `SOUL.md` and `STYLE.md`, replaces those managed sections idempotently, and leaves unrelated user content intact.

**Tech Stack:** Python 3.9 standard library, existing unittest suite, Markdown workspace files.

## Global Constraints

- B1 only stabilizes persona core files; do not implement local identity Q&A, new feedback commands, or style inspection commands.
- Managed sections must be idempotent and safe to re-run.
- Keep existing user feedback sections in `STYLE.md`.
- Keep current OpenClaw workspace path `${HOME}/.openclaw/workspace`.

---

### Task 1: Persona Installer Module

**Files:**
- Create: `src/qq_openclaw_bridge/persona.py`
- Create: `tests/test_persona.py`

**Interfaces:**
- Produces `install_persona_core(workspace_dir: Path) -> None`.
- Produces `apply_managed_section(content: str, heading: str, body: str) -> str`.

- [ ] **Step 1: Write failing tests**

Add tests that assert:
- installing creates `SOUL.md` and `STYLE.md`;
- sections include `B1 人设内核`;
- rerunning does not duplicate managed sections;
- existing `## 用户反馈沉淀` content remains.

- [ ] **Step 2: Run persona tests and verify RED**

Run: `python3 -m unittest tests.test_persona -q`

Expected: FAIL because `qq_openclaw_bridge.persona` does not exist.

- [ ] **Step 3: Implement module**

Implement managed section replacement with ASCII-safe Python code and Chinese Markdown content constants.

- [ ] **Step 4: Run persona tests and verify GREEN**

Run: `python3 -m unittest tests.test_persona -q`

Expected: OK.

---

### Task 2: Installer Script And Runtime Application

**Files:**
- Create: `scripts/install_persona_b1.py`
- Modify: `README.md`

**Interfaces:**
- Script usage: `python3 scripts/install_persona_b1.py [workspace_dir]`
- Default workspace dir: `~/.openclaw/workspace`.

- [ ] **Step 1: Write script**

The script imports `install_persona_core` and applies it to the default or provided workspace dir.

- [ ] **Step 2: Run full tests**

Run: `python3 -m unittest discover -s tests -q`

Expected: OK.

- [ ] **Step 3: Apply B1 to OpenClaw workspace**

Run: `python3 scripts/install_persona_b1.py ${HOME}/.openclaw/workspace`

Expected: prints updated file paths.

- [ ] **Step 4: Verify installed files**

Run:

```bash
rg -n "B1 人设内核|chichu|毒舌御姐|不要回复成普通 AI 助手介绍|先办事，再发挥风格" ${HOME}/.openclaw/workspace/SOUL.md ${HOME}/.openclaw/workspace/STYLE.md
```

Expected: matches in both files.

---

## Self-Review

- Spec coverage: B1 is limited to reproducible persona core installation.
- Placeholder scan: no unspecified implementation remains.
- Type consistency: `install_persona_core(Path) -> None` is the only public implementation interface.
