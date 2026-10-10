# KanagawaWave Dataset Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build deterministic, privacy-preserving LLaMA-Factory Alpaca train/test datasets from the one permitted KanagawaWave SQLite member.

**Architecture:** A reusable dataset package owns safe source opening, schema validation, candidate recovery/filtering, deterministic thread partitioning and bounded sampling, neutralization, final leakage validation, checkpointing, and atomic publication. A thin CLI validates arguments, constructs either a database source or exact-member archive source, selects the local Ollama neutralizer, and maps expected failures to a non-zero exit.

**Tech Stack:** Python 3.9 standard library, SQLite, `unittest`, local Ollama HTTP API, system `tar --zstd` invoked without a shell.

**Spec:** `${HOME}/.codex/attachments/5057d62b-be73-4cee-9f66-83d6bc92c35f/pasted-text.txt` plus the approved task requirements supplied by the parent agent.

## Global Constraints

- Never execute code or read any archive member other than `data/db/KanagawaWave.sqlite3`.
- Archive extraction uses an argv list, a mode-0600 temporary DB, a 4 GiB streaming limit, and guaranteed cleanup.
- Final examples contain exactly `instruction`, `input`, and `output`; raw source text must first pass through the injected neutralizer.
- Partition by stable hash of `thread_id` before sampling; one thread cannot cross train/test.
- Apply per-split author and short-reply caps and deterministic exact/NFC de-duplication.
- Reject PII, URLs, usernames, ads, bot notices, media-only text, obvious garbage, removed/deleted text, and overlong text without placeholders.
- Publish train, test, manifest, and checkpoint atomically only when exact requested counts pass final validation.
- Do not commit Git changes and do not run the full real 2,200-row generation.

## Review Focus

- Malformed JSON fields and schema drift must fail closed without leaking row content.
- An archive process that exceeds 4 GiB, exits non-zero, or writes an invalid DB must leave no temporary file.
- A neutralizer that returns malformed/unsafe/echoed content must be retried only to the configured bound, then skipped.
- Resume state must not contain source text or public source identifiers and must not change deterministic output.
- A shortage caused by author or short-reply caps must leave all previously absent destination files absent.

---

### Task 1: Safe source access and row recovery

**Files:**
- Create: `src/qq_openclaw_bridge/dataset/__init__.py`
- Create: `src/qq_openclaw_bridge/dataset/kanagawawave.py`
- Test: `tests/test_kanagawawave_dataset.py`

**Interfaces:**
- Produces: `open_readonly_database(path)`, `extract_database_from_archive(path, runner=subprocess.Popen)`, `load_candidates(path)`.

- [ ] Write real temporary-SQLite tests for strict schema validation, submission/comment parent recovery, missing/cross-subreddit parents, and exact safe archive extraction arguments/limits/cleanup.
- [ ] Run focused tests and verify missing imports/functions fail for the intended reason.
- [ ] Implement read-only/query-only access, strict column validation, JSON decoding, parent lookup, and bounded extraction.
- [ ] Re-run focused tests to green.

### Task 2: Deterministic filtering, partitioning, and sampling

**Files:**
- Modify: `src/qq_openclaw_bridge/dataset/kanagawawave.py`
- Modify: `tests/test_kanagawawave_dataset.py`

**Interfaces:**
- Consumes: recovered `RawCandidate` records.
- Produces: `candidate_split(thread_id, seed)`, `filter_candidate(candidate)`, `sample_candidates(...)`, `classify_style(output)`.

- [ ] Add failing literal-fixture tests for thread isolation, deterministic results, author cap, 15% short cap, NFC/exact de-duplication, PII/removal/URL/ad/media/garbage/length filtering, and preservation of emoji/repeated punctuation/case/typos.
- [ ] Verify each group is red because behavior is absent.
- [ ] Implement minimal deterministic hashing, filters, caps, de-duplication, and style heuristics without normalizing source wording.
- [ ] Re-run focused tests to green and refactor only while green.

### Task 3: Neutralization and final safety validation

**Files:**
- Modify: `src/qq_openclaw_bridge/dataset/kanagawawave.py`
- Modify: `tests/test_kanagawawave_dataset.py`

**Interfaces:**
- Produces: `Neutralizer` protocol, `OllamaNeutralizer`, `validate_neutralized_pair(...)`, and bounded retry handling.

- [ ] Add failing tests using an injected fake neutralizer for JSON-only Alpaca output, style label derivation, retry/skip, PII/control-character rejection, and rejection of source long-fragment leakage while allowing common short phrases.
- [ ] Run the focused tests and record the expected red failures.
- [ ] Implement the protocol and local-only Ollama HTTP call using structured JSON, `think: false`, low temperature, bounded response reads, and fixed safe error classes.
- [ ] Implement final validation and verify focused tests green.

### Task 4: Atomic builder, checkpoint, manifest, and CLI

**Files:**
- Modify: `src/qq_openclaw_bridge/dataset/kanagawawave.py`
- Create: `scripts/build_kanagawawave_dataset.py`
- Modify: `tests/test_kanagawawave_dataset.py`

**Interfaces:**
- Produces: `BuildConfig`, `build_dataset(config, neutralizer)`, and `main(argv)`.

- [ ] Add failing end-to-end tests over a temporary SQLite DB for exact output shape/count, safe manifest/checkpoint, resumability, no duplicate/thread leakage audit counts, and no partial output on shortage.
- [ ] Add failing CLI tests for required exclusive source arguments and defaults.
- [ ] Run and confirm red failures.
- [ ] Implement staging-directory writes, fsync-and-replace publication, safe checkpoint identifiers only, and CLI parsing/error mapping.
- [ ] Run focused and neighboring tests to green.

### Task 5: Verification and audit

**Files:**
- Review all files above.

- [ ] Run `python -m unittest tests.test_kanagawawave_dataset -v`.
- [ ] Run neighboring dataset/community-style tests.
- [ ] Run the repository test suite and report any unrelated failures by name.
- [ ] Run `python -m compileall -q src scripts tests/test_kanagawawave_dataset.py`.
- [ ] Run `git diff --check` and inspect `git diff --stat` plus the full targeted diff.
- [ ] Confirm no archive member other than the exact SQLite path is referenced and no raw content can enter manifest/checkpoint.
