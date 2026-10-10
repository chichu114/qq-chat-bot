# Housekeeper Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade chichu from basic memory/todo/reminder into a practical private QQ housekeeper with daily briefings, evening review prompts, recurring reminders, todo categories, and quiet hours.

**Architecture:** Keep the current Markdown-backed storage model. Extend `TODO.md` item metadata with `category`, `repeat`, and recurrence fields; extend `REMINDER_STATE.md` with schedule send-state keys for daily briefing and evening review. `ReminderStore` remains the single proactive push source used by the existing bridge loop.

**Tech Stack:** Python 3.9 standard library, existing unittest suite, existing OneBot bridge loop, existing Markdown storage files.

## Global Constraints

- Keep the first A-stage scoped to QQ private text messages only.
- Do not introduce a database or external scheduler.
- Preserve compatibility with existing `TODO.md` lines that only have `user`, `due`, and `reminded`.
- Default daily briefing time is `09:00`.
- Default evening review time is `23:00`.
- Default quiet hours are `00:00-08:30`.
- Default categories are `工作`, `生活`, `学习`, `项目`, `其他`.
- Do not implement route B personality upgrades or route C tool permissions in this plan.

---

## File Structure

- Modify `src/qq_openclaw_bridge/config.py`
  - Add config fields for daily briefing, evening review, quiet hours, and category defaults.
- Modify `src/qq_openclaw_bridge/housekeeper.py`
  - Parse category syntax and recurring reminder syntax.
  - Store category and recurrence metadata on `TodoItem`.
  - Render category in list replies.
- Modify `src/qq_openclaw_bridge/reminders.py`
  - Respect quiet hours.
  - Advance recurring reminders after sending.
  - Generate daily briefing and evening review notifications.
  - Persist proactive send state in `REMINDER_STATE.md`.
- Modify `src/qq_openclaw_bridge/bridge.py`
  - Log proactive notification IDs without assuming every notification is a todo item.
- Modify `.env.example`, `README.md`, and `scripts/check_ready.sh`
  - Document and print the new A-stage options.
- Modify tests:
  - `tests/test_config.py`
  - `tests/test_housekeeper.py`
  - `tests/test_reminders.py`
  - `tests/test_bridge.py`

---

### Task 1: Configuration For Phase A Scheduling

**Files:**
- Modify: `src/qq_openclaw_bridge/config.py`
- Modify: `.env.example`
- Modify: `scripts/check_ready.sh`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces config fields:
  - `enable_daily_briefing: bool`
  - `daily_briefing_time: str`
  - `enable_evening_review: bool`
  - `evening_review_time: str`
  - `quiet_hours_start: str`
  - `quiet_hours_end: str`

- [ ] **Step 1: Write failing config tests**

Add tests asserting defaults and env overrides:

```python
def test_loads_phase_a_housekeeper_defaults(self):
    from qq_openclaw_bridge.config import Config

    config = Config.from_env({"ONEBOT_WS_URL": "ws://127.0.0.1:3001"})

    self.assertTrue(config.enable_daily_briefing)
    self.assertEqual(config.daily_briefing_time, "09:00")
    self.assertTrue(config.enable_evening_review)
    self.assertEqual(config.evening_review_time, "23:00")
    self.assertEqual(config.quiet_hours_start, "00:00")
    self.assertEqual(config.quiet_hours_end, "08:30")

def test_parses_phase_a_housekeeper_options(self):
    from qq_openclaw_bridge.config import Config

    config = Config.from_env(
        {
            "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
            "ENABLE_DAILY_BRIEFING": "false",
            "DAILY_BRIEFING_TIME": "08:45",
            "ENABLE_EVENING_REVIEW": "false",
            "EVENING_REVIEW_TIME": "22:30",
            "QUIET_HOURS_START": "23:30",
            "QUIET_HOURS_END": "09:15",
        }
    )

    self.assertFalse(config.enable_daily_briefing)
    self.assertEqual(config.daily_briefing_time, "08:45")
    self.assertFalse(config.enable_evening_review)
    self.assertEqual(config.evening_review_time, "22:30")
    self.assertEqual(config.quiet_hours_start, "23:30")
    self.assertEqual(config.quiet_hours_end, "09:15")
```

- [ ] **Step 2: Run config tests and verify RED**

Run: `python3 -m unittest tests.test_config -q`

Expected: FAIL because `Config` lacks the new fields.

- [ ] **Step 3: Implement config fields**

Add dataclass fields and parse env vars with `_get_bool`.

- [ ] **Step 4: Update env and readiness output**

Add new keys to `.env.example` and have `scripts/check_ready.sh` print daily briefing, evening review, and quiet hours.

- [ ] **Step 5: Run config tests and verify GREEN**

Run: `python3 -m unittest tests.test_config -q`

Expected: OK.

---

### Task 2: Todo Categories

**Files:**
- Modify: `src/qq_openclaw_bridge/housekeeper.py`
- Test: `tests/test_housekeeper.py`

**Interfaces:**
- Produces metadata key `category=<工作|生活|学习|项目|其他>`.
- Supported inputs:
  - `工作待办：写周报`
  - `待办：工作：写周报`
  - `待办：写周报 #工作`
  - `工作待办`
  - `待办列表 工作`

- [ ] **Step 1: Write failing category tests**

Add tests for recording, listing, and category filtering.

- [ ] **Step 2: Run housekeeper tests and verify RED**

Run: `python3 -m unittest tests.test_housekeeper.HousekeeperTests -q`

Expected: FAIL because category syntax is not parsed.

- [ ] **Step 3: Implement category parsing and list rendering**

Extend `_extract_todo_to_record` to return category in metadata, add category list command parsing, and render list items as `#1 [工作] 写周报`.

- [ ] **Step 4: Run housekeeper tests and verify GREEN**

Run: `python3 -m unittest tests.test_housekeeper.HousekeeperTests -q`

Expected: OK.

---

### Task 3: Recurring Reminders

**Files:**
- Modify: `src/qq_openclaw_bridge/housekeeper.py`
- Modify: `src/qq_openclaw_bridge/reminders.py`
- Test: `tests/test_housekeeper.py`
- Test: `tests/test_reminders.py`

**Interfaces:**
- Produces metadata keys:
  - `repeat=daily|weekly|monthly`
  - `repeat_time=HH:MM`
  - `repeat_weekday=1-7` for weekly, where 1 is Monday and 7 is Sunday
  - `repeat_month_day=1-31` for monthly
- After a recurring reminder is sent, `ReminderStore.mark_sent` advances `due` and leaves `reminded=` empty.

- [ ] **Step 1: Write failing recurring reminder tests**

Cover:
- `每天9点提醒我喝水`
- `每周一 9:30提醒我开周会`
- `每月1号 9:00提醒我交房租`
- Daily reminder advances from `2026-07-11T09:00` to `2026-07-12T09:00` after being sent.

- [ ] **Step 2: Run reminder-related tests and verify RED**

Run: `python3 -m unittest tests.test_housekeeper tests.test_reminders -q`

Expected: FAIL because recurring metadata and advance behavior do not exist.

- [ ] **Step 3: Implement recurrence parsing and next-due calculation**

Add helpers in `housekeeper.py` for initial due calculation. Add helpers in `reminders.py` for advancing next due after send.

- [ ] **Step 4: Run reminder-related tests and verify GREEN**

Run: `python3 -m unittest tests.test_housekeeper tests.test_reminders -q`

Expected: OK.

---

### Task 4: Quiet Hours

**Files:**
- Modify: `src/qq_openclaw_bridge/reminders.py`
- Test: `tests/test_reminders.py`

**Interfaces:**
- `ReminderStore.collect_due(now)` returns no proactive notifications during quiet hours.
- Due reminders become eligible after quiet hours end.

- [ ] **Step 1: Write failing quiet-hours tests**

Add tests showing a due reminder at `07:00` is skipped and the same reminder is collected at `08:31`.

- [ ] **Step 2: Run reminder tests and verify RED**

Run: `python3 -m unittest tests.test_reminders -q`

Expected: FAIL because quiet hours are not enforced.

- [ ] **Step 3: Implement quiet-hours check**

Add `quiet_hours_start` and `quiet_hours_end` constructor parameters to `ReminderStore` and skip all proactive pushes while current time is inside the configured range.

- [ ] **Step 4: Run reminder tests and verify GREEN**

Run: `python3 -m unittest tests.test_reminders -q`

Expected: OK.

---

### Task 5: Daily Briefing And Evening Review

**Files:**
- Modify: `src/qq_openclaw_bridge/reminders.py`
- Modify: `src/qq_openclaw_bridge/bridge.py`
- Test: `tests/test_reminders.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- `ReminderStore.collect_due(now)` returns:
  - `kind="daily_briefing"` notification once per configured date after `DAILY_BRIEFING_TIME`.
  - `kind="evening_review"` notification once per configured date after `EVENING_REVIEW_TIME`.
- State keys:
  - `daily_briefing_sent=YYYY-MM-DD`
  - `evening_review_sent=YYYY-MM-DD`

- [ ] **Step 1: Write failing proactive schedule tests**

Add tests that collect daily briefing at `09:01`, skip duplicate briefing later the same day after `mark_sent`, and collect evening review at `23:01`.

- [ ] **Step 2: Run proactive tests and verify RED**

Run: `python3 -m unittest tests.test_reminders tests.test_bridge -q`

Expected: FAIL because scheduled proactive notifications do not exist.

- [ ] **Step 3: Implement proactive notifications**

Build daily briefing message from active todos and today’s due reminders. Build evening review prompt from active todo count. Mark sent in `REMINDER_STATE.md`.

- [ ] **Step 4: Update bridge logging**

Log `notification_id` and `user_id`, keeping todo reminders readable.

- [ ] **Step 5: Run proactive tests and verify GREEN**

Run: `python3 -m unittest tests.test_reminders tests.test_bridge -q`

Expected: OK.

---

### Task 6: Documentation, Runtime Sync, And Restart

**Files:**
- Modify: `README.md`
- Copy changed runtime files to `${HOME}/qq-chat-bot`

**Interfaces:**
- Desktop buttons continue to start/stop the runtime copy at `${HOME}/qq-chat-bot`.

- [ ] **Step 1: Update README**

Document:
- `工作待办：...`
- `每天9点提醒我...`
- `每周一 9:30提醒我...`
- `每月1号 9:00提醒我...`
- daily briefing, evening review, quiet hours config keys.

- [ ] **Step 2: Run the full test suite**

Run: `python3 -m unittest discover -s tests -q`

Expected: OK.

- [ ] **Step 3: Sync runtime source and config docs**

Run:

```bash
ditto src ${HOME}/qq-chat-bot/src
ditto scripts ${HOME}/qq-chat-bot/scripts
ditto .env.example ${HOME}/qq-chat-bot/.env.example
ditto README.md ${HOME}/qq-chat-bot/README.md
```

- [ ] **Step 4: Run runtime readiness check**

Run: `/bin/bash ${HOME}/qq-chat-bot/scripts/start_bridge.sh --check-only`

Expected: `[OK] ready`.

- [ ] **Step 5: Restart runtime bridge**

Run: `/bin/bash ${HOME}/qq-chat-bot/scripts/bridge_service.sh restart`

Expected: `Bridge started` and `connected to OneBot WebSocket` in `${HOME}/qq-chat-bot/logs/bridge.log`.

---

## Self-Review

- Spec coverage: A-stage requirements map to Tasks 1-6.
- Placeholder scan: no `TBD`, `TODO`, or unspecified implementation task remains.
- Type consistency: `ReminderNotification.kind`, `notification_id`, and `todo_id` will be defined in Task 5 and used by `Bridge`.
