# KanagawaWave Dataset V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a verified 10,000-row train and 1,000-row test dataset that preserves authorized KanagawaWave community language, plus a matching QLoRA training bundle and an inactive future runtime prompt path.

**Architecture:** Add a v2 dataset module beside the existing neutralized v1 builder. Reuse the proven archive extraction and parent-recovery boundary, but replace model-based rewriting with reject-only safety filtering, deterministic thread-level sampling, private token checkpoints, and atomic publication. Share one prompt-contract constant between the dataset builder and a profile-gated QQ prompt path so training and future inference cannot drift.

**Tech Stack:** Python 3.9 standard library, SQLite, `unittest`, LLaMA-Factory configuration files, local `tar --zstd`, ZIP packaging.

**Spec:** `docs/superpowers/specs/2026-10-01-kanagawawave-dataset-v2-design.md`

## Global Constraints

- Read only `data/db/KanagawaWave.sqlite3` from the authorized archive; never execute archive content.
- Preserve accepted parent and reply strings exactly; do not paraphrase, correct, translate, or normalize emitted text.
- Reject a complete pair when either side triggers a privacy, URL, advertising, bot, garbage, control-character, dangerous-instruction, or targeted-harassment rule.
- Publish exactly 10,000 train rows, 1,000 test rows, and 100 deterministic review rows for the real build.
- Split by thread before sampling; train/test thread overlap must be zero.
- Limit each author to 50 train rows and 10 test rows; cap very short replies at 15 percent per split.
- Published examples contain exactly `instruction`, `input`, and `output`; no source or author metadata.
- Raw/generated dataset artifacts remain local and uncommitted.
- Do not train or import a model, deploy code, switch the active Ollama model or profile, change `@`/reply triggering, or send QQ messages.
- Keep the current v1 builder and current v1 runtime behavior working.

## Review Focus

- NFC-equivalent duplicates with different original spellings must be selected once without rewriting the selected original string; Task 2 pins this.
- A PII/URL token touching Chinese text without whitespace must still reject the pair while ordinary code-switching remains eligible; Task 1 pins this.
- A checkpoint from a different policy, archive fingerprint, seed, or target count must fail closed without exposing text; Task 3 pins this.
- A build interrupted after staging starts must preserve the previous complete output and remove the stage directory; Task 3 pins this.
- The future v2 prompt path must not activate for the current v1 profile and must keep user text in the user role; Task 5 pins this.

---

### Task 1: Shared prompt contract and reject-only candidate policy

**Files:**
- Create: `src/qq_openclaw_bridge/community_prompts.py`
- Create: `src/qq_openclaw_bridge/dataset/kanagawawave_v2.py`
- Create: `tests/test_kanagawawave_v2_dataset.py`

**Interfaces:**
- Consumes: `RawCandidate` from `qq_openclaw_bridge.dataset.kanagawawave`.
- Produces: `KANAGAWAWAVE_V2_PROFILE_KEY: str`, `KANAGAWAWAVE_V2_INSTRUCTION: str`, `V2_POLICY_VERSION: str`, `rejection_reason(candidate: RawCandidate) -> Optional[str]`, and `example_from_candidate(candidate: RawCandidate) -> Mapping[str, str]`.

- [ ] **Step 1: Write the failing prompt-contract and preservation tests**

Add tests named:

- `test_v2_prompt_contract_is_single_exact_instruction`
- `test_example_preserves_eligible_parent_and_reply_exactly`
- `test_example_contains_only_alpaca_fields`

Assert the instruction is exactly:

```text
[COMMUNITY=KANAGAWAVE] 以该社区的常见语气自然回复下面的内容。只输出回复正文。
```

Use an eligible fixture containing mixed case, emoji, `.jpg`, repeated punctuation, a deliberate typo, and line breaks; assert the emitted input/output strings are unchanged.

- [ ] **Step 2: Run the new tests and verify RED**

Run: `python -m unittest tests.test_kanagawawave_v2_dataset -v`

Expected: import failure because the v2 module and prompt constants do not exist.

- [ ] **Step 3: Implement the shared constants and minimum example conversion**

Create `community_prompts.py` with the exact profile key
`kanagawawave-v2-model-style` and instruction above. In
`kanagawawave_v2.py`, import the instruction and return only the three Alpaca
fields without modifying eligible strings.

- [ ] **Step 4: Write failing reject-only policy tests**

Add table-driven tests named:

- `test_rejects_each_privacy_and_contact_class`
- `test_rejects_urls_ads_bots_removed_media_and_garbage`
- `test_rejects_dangerous_instructions_and_targeted_harassment`
- `test_rejects_contact_and_url_tokens_touching_chinese`
- `test_accepts_forum_style_signals_without_rewriting`

The acceptance case must include ordinary code-switching such as `PS6要是`
and `.jpg` so the privacy detector cannot collapse into a blanket ban on
ASCII text touching Chinese.

Each rejection asserts a stable reason from this closed set:
`privacy`, `url`, `advertisement`, `bot`, `removed`, `media_only`, `garbage`,
`invalid_text`, `overlong`, `dangerous_content`, `targeted_harassment`, or
`input_equals_output`. Assertions must never include the rejected text.

- [ ] **Step 5: Run the policy tests and verify RED**

Run: `python -m unittest tests.test_kanagawawave_v2_dataset -v`

Expected: failures because `rejection_reason` does not yet implement the v2
policy.

- [ ] **Step 6: Implement `rejection_reason` with deterministic detectors**

Reuse safe, source-independent detector patterns from the v1 module where
their behavior matches the v2 spec. Add explicit bounded marker sets for
dangerous instructions and targeted harassment. Return only the stable
reason token, never text or match details. Keep comparison-only NFC values
local and return `None` for eligible candidates.

- [ ] **Step 7: Run focused and v1 regression tests**

Run:

```bash
python -m unittest tests.test_kanagawawave_v2_dataset -v
python -m unittest tests.test_kanagawawave_dataset -v
```

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/qq_openclaw_bridge/community_prompts.py src/qq_openclaw_bridge/dataset/kanagawawave_v2.py tests/test_kanagawawave_v2_dataset.py
git commit -m "feat: add KanagawaWave v2 candidate policy"
```

### Task 2: Deterministic thread split, sampling, and aggregate audit

**Files:**
- Modify: `src/qq_openclaw_bridge/dataset/kanagawawave_v2.py`
- Modify: `tests/test_kanagawawave_v2_dataset.py`

**Interfaces:**
- Consumes: accepted `RawCandidate` values and the Task 1 policy.
- Produces: `V2BuildConfig`, `V2Selection`, `v2_candidate_split(thread_id: str, seed: str) -> str`, and `select_v2_candidates(candidates: Sequence[RawCandidate], config: V2BuildConfig) -> V2Selection`.

- [ ] **Step 1: Write failing selection tests**

Add tests named:

- `test_v2_split_is_deterministic_and_keeps_threads_whole`
- `test_v2_selection_preserves_natural_mix_without_style_labels`
- `test_v2_selection_enforces_per_split_author_caps`
- `test_v2_selection_caps_short_replies_by_unicode_alphanumeric_length`
- `test_v2_selection_deduplicates_exact_and_nfc_pairs_globally`
- `test_v2_selection_rejects_input_equal_to_output`
- `test_v2_selection_requires_exact_target_counts`

Use small literal candidates whose thread and author assignments make each
assertion observable. Assert that the selected original string wins an
NFC-equivalent collision unchanged.

- [ ] **Step 2: Run the selection tests and verify RED**

Run: `python -m unittest tests.test_kanagawawave_v2_dataset -v`

Expected: failures because the selection interfaces are missing.

- [ ] **Step 3: Implement immutable v2 configuration and selection results**

`V2BuildConfig` contains `database`, `archive`, `output_dir`, `train_size`
(default 10000), `test_size` (default 1000), `review_size` (default 100),
`split_seed` (default `kanagawave-v2`), `max_per_author_train` (default 50),
`max_per_author_test` (default 10), and `max_short_ratio` (default 0.15).

`V2Selection` contains selected candidate tuples, salted selected-thread
tokens, per-split author counts used only in memory, stable aggregate
rejection counts, and safe audit counters.

- [ ] **Step 4: Implement deterministic selection**

Hash the thread ID with the seed before sampling, rank candidates with a
separate seeded digest, apply the reject-only policy, global NFC pair
deduplication, author caps, and the six-alphanumeric-character short rule.
Raise `DatasetBuildError("insufficient safe candidates for requested dataset sizes")`
unless both exact targets are reached.

- [ ] **Step 5: Write failing aggregate style-signal tests**

Add `test_v2_audit_reports_only_aggregate_nonidentifying_style_signals`.
Assert length buckets, emoji rate, repeated-punctuation rate, code-switching
rate, question rate, and top one-to-three-character n-grams with counts.
Reject digits and ASCII identifier runs from reported n-grams and assert no
complete fixture sentence appears in the audit.

- [ ] **Step 6: Implement aggregate audit calculation**

Add `style_signal_audit(examples: Sequence[Mapping[str, str]]) -> Mapping[str, object>`.
Keep n-grams to one through three Unicode characters, exclude whitespace,
digits, controls, and ASCII identifier sequences, and report counts only.

- [ ] **Step 7: Run focused tests**

Run: `python -m unittest tests.test_kanagawave_v2_dataset -v`

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/qq_openclaw_bridge/dataset/kanagawawave_v2.py tests/test_kanagawawave_v2_dataset.py
git commit -m "feat: select deterministic KanagawaWave v2 splits"
```

### Task 3: Private checkpoint, atomic publication, and CLI

**Files:**
- Modify: `src/qq_openclaw_bridge/dataset/kanagawawave_v2.py`
- Create: `scripts/build_kanagawawave_v2_dataset.py`
- Modify: `tests/test_kanagawawave_v2_dataset.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `V2BuildConfig`, v1 `extract_database_from_archive`, v1 `load_candidates`, and Task 2 selection.
- Produces: `build_v2_dataset(config: V2BuildConfig) -> Mapping[str, object]` and CLI `main(argv=None) -> int`.

- [ ] **Step 1: Write failing publication tests**

Add tests named:

- `test_v2_build_publishes_exact_atomic_artifact_set`
- `test_v2_manifest_hashes_train_test_review_and_prompt_files`
- `test_v2_review_rows_are_deterministic_subset_of_test`
- `test_v2_checkpoint_contains_only_private_tokens_and_safe_status`
- `test_v2_checkpoint_mismatch_fails_closed_without_text`
- `test_v2_interruption_preserves_previous_output_and_removes_stage`
- `test_v2_shortage_publishes_nothing`
- `test_v2_same_input_seed_and_policy_are_byte_identical`

Use temporary SQLite databases with the existing exact `records` schema.
For small fixtures, set explicit small train/test/review sizes.

- [ ] **Step 2: Run publication tests and verify RED**

Run: `python -m unittest tests.test_kanagawave_v2_dataset -v`

Expected: failures because publication and checkpoint functions are absent.

- [ ] **Step 3: Implement checkpoint validation and source context**

Derive private candidate and thread tokens with SHA-256 over the seed,
purpose, and source value. Bind a checkpoint to the v2 policy version,
private source fingerprint token, seed, counts, and limits. Store only
selected candidate tokens, split, and safe status. Reject any extra key or
raw string field and write checkpoints mode 0600.

- [ ] **Step 4: Implement atomic artifact publication**

Create a mode-0700 sibling stage directory, write mode-0600 canonical JSON
files and `inference_prompt.txt`, fsync them, validate all audits, then replace
the absent destination directory atomically. If a complete destination
already exists, validate and return it only when its configuration token
matches; otherwise fail without overwriting it.

The published set is exactly `forum_train.json`, `forum_test.json`,
`manifest.json`, `human_review.json`, `inference_prompt.txt`, and
`checkpoint.json`.

- [ ] **Step 5: Write failing CLI tests**

Add tests for exactly one of `--archive`/`--database`, v2 defaults, no Ollama
arguments, stable safe failure text, and success against a small fixture.

- [ ] **Step 6: Implement `scripts/build_kanagawawave_v2_dataset.py`**

Parse directly into `V2BuildConfig`, call `build_v2_dataset`, print only a
safe JSON summary on success, and map `DatasetBuildError`, `ValueError`, and
expected source errors to exit 1 without echoing source content.

- [ ] **Step 7: Ignore local v2 artifacts**

Add `datasets/kanagawawave_v2/` to `.gitignore`. Do not broaden ignore rules
for unrelated training or distribution directories.

- [ ] **Step 8: Run focused, v1, and CLI tests**

Run:

```bash
python -m unittest tests.test_kanagawawave_v2_dataset -v
python -m unittest tests.test_kanagawawave_dataset -v
python -m compileall -q src scripts/build_kanagawawave_v2_dataset.py tests/test_kanagawawave_v2_dataset.py
```

Expected: PASS with no output from `compileall`.

- [ ] **Step 9: Commit Task 3**

```bash
git add .gitignore src/qq_openclaw_bridge/dataset/kanagawawave_v2.py scripts/build_kanagawawave_v2_dataset.py tests/test_kanagawawave_v2_dataset.py
git commit -m "feat: publish KanagawaWave v2 datasets"
```

### Task 4: Self-contained QLoRA training bundle template and validator

**Files:**
- Create: `training/kanagawawave_v2/configs/train_4060_8gb.yaml`
- Create: `training/kanagawawave_v2/configs/train_4060_low_memory.yaml`
- Create: `training/kanagawawave_v2/configs/chat.yaml`
- Create: `training/kanagawawave_v2/configs/merge.yaml`
- Create: `training/kanagawawave_v2/data/dataset_info.json`
- Create: `training/kanagawawave_v2/INFERENCE_PROMPT.txt`
- Create: `training/kanagawawave_v2/README_zh.md`
- Create: `scripts/package_kanagawawave_v2_training.py`
- Create: `scripts/validate_kanagawawave_v2_bundle.py`
- Create: `tests/test_kanagawawave_v2_bundle.py`

**Interfaces:**
- Consumes: a complete Task 3 dataset directory and the committed training template.
- Produces: `build_training_bundle(dataset_dir: Path, output_zip: Path) -> Mapping[str, object]`, `validate_training_bundle(root: Path) -> Mapping[str, object]`, and a portable ZIP for the other computer.

- [ ] **Step 1: Write failing training-template tests**

Assert both train configurations use Qwen3-4B-Instruct-2507, 4-bit NF4
QLoRA, rank 8, alpha 16, dropout 0.05, `lora_target: all`, learning rate
1e-4, two epochs, `qwen3_nothink`, train dataset `forum_train_v2`, and eval
dataset `forum_test_v2`. Assert the low-memory variant differs only in the
documented cutoff, accumulation, worker, and output settings.

- [ ] **Step 2: Run bundle tests and verify RED**

Run: `python -m unittest tests.test_kanagawawave_v2_bundle -v`

Expected: missing template and packaging interfaces.

- [ ] **Step 3: Add the minimal training template and Chinese guide**

Register Alpaca columns in `dataset_info.json`. Make `INFERENCE_PROMPT.txt`
byte-identical to the shared constant. Document setup, preflight validation,
train, resume, chat, merge, and return-package commands without embedding
source-derived examples.

- [ ] **Step 4: Write failing package-integrity tests**

Add tests named:

- `test_bundle_requires_exact_v2_counts_and_prompt`
- `test_bundle_recomputes_and_verifies_dataset_hashes`
- `test_bundle_rejects_extra_alpaca_keys_or_v1_style_labels`
- `test_bundle_contains_no_checkpoint_or_source_metadata`
- `test_bundle_zip_is_deterministic_and_validates_after_extraction`

- [ ] **Step 5: Implement packager and validator**

Copy only train, test, manifest, review, and prompt artifacts into a private
staging bundle; exclude the checkpoint. Write `DATASET_LOCK.json` containing
counts and SHA-256 hashes, validate every row and instruction, copy the
committed template, and create a deterministic ZIP. The validator must work
without network access or LLaMA-Factory installed.

- [ ] **Step 6: Run bundle tests**

Run: `python -m unittest tests.test_kanagawawave_v2_bundle -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add training/kanagawawave_v2 scripts/package_kanagawawave_v2_training.py scripts/validate_kanagawawave_v2_bundle.py tests/test_kanagawawave_v2_bundle.py
git commit -m "feat: package KanagawaWave v2 QLoRA training bundle"
```

### Task 5: Prepare but do not activate the matched QQ inference prompt

**Files:**
- Modify: `src/qq_openclaw_bridge/application/group_chat.py`
- Modify: `tests/test_group_chat.py`

**Interfaces:**
- Consumes: `KANAGAWAWAVE_V2_PROFILE_KEY` and `KANAGAWAWAVE_V2_INSTRUCTION` from Task 1.
- Produces: a profile-gated v2 user prompt inside `GroupPromptBuilder.build(...)`; all other profiles retain the existing JSON prompt path.

- [ ] **Step 1: Write failing profile-gating tests**

Add tests named:

- `test_v2_profile_uses_exact_training_instruction_before_message`
- `test_v2_quote_is_bounded_user_context_not_system_content`
- `test_v1_and_neutral_profiles_keep_existing_prompt_shape`
- `test_user_prompt_injection_cannot_create_a_system_message`

Construct a valid active profile with key `kanagawawave-v2-model-style` and
assert the final user message begins with the exact instruction, followed by
the current message and optional labelled quoted context. Assert all user and
quote content remains in one `role=user` message.

- [ ] **Step 2: Run group-chat tests and verify RED**

Run: `python -m unittest tests.test_group_chat -v`

Expected: v2 prompt assertion fails because every profile currently uses the
JSON request body.

- [ ] **Step 3: Implement the exact profile-gated prompt path**

Add one bounded helper for v2 user content. Do not change coordinator model
selection, style cards, safety system rules, delivery, group binding, or
trigger filters. The current profile key `kanagawawave-model-style` must not
match the v2 gate.

- [ ] **Step 4: Run group and acceptance regressions**

Run:

```bash
python -m unittest tests.test_group_chat -v
python -m unittest tests.test_community_style_acceptance -v
python -m unittest tests.test_group_filters tests.test_group_delivery -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/qq_openclaw_bridge/application/group_chat.py tests/test_group_chat.py
git commit -m "feat: align future KanagawaWave v2 inference prompt"
```

### Task 6: Build, audit, review, and package the real local dataset

**Files:**
- Read: `${HOME}/Desktop/KanagawaWave_2026-09-23.tar.zst`
- Generate locally: `datasets/kanagawawave_v2/`
- Generate locally: `datasets/kanagawawave_v2/kanagawawave-v2-training-bundle.zip`

**Interfaces:**
- Consumes: the authorized archive and Tasks 1–4.
- Produces: verified local v2 dataset artifacts, a user-reviewed attestation outside the dataset files, and the portable training ZIP.

- [ ] **Step 1: Run the real v2 build**

Run:

```bash
python scripts/build_kanagawawave_v2_dataset.py \
  --archive ${HOME}/Desktop/KanagawaWave_2026-09-23.tar.zst \
  --output-dir datasets/kanagawawave_v2
```

Expected: safe JSON summary with train 10000, test 1000, review 100, zero
thread overlap, and zero duplicate examples.

- [ ] **Step 2: Independently verify published artifacts**

Load the JSON files read-only and assert the exact schemas, counts, prompt,
hashes, caps, no duplicate pairs, and review-subset relation. Re-run all
implemented detectors against every published input/output and require zero
findings. Do not print raw samples to logs.

- [ ] **Step 3: Present `human_review.json` to the user and pause for review**

Give the user a clickable local file link and ask them to inspect context
coherence, privacy, community style, and excessive toxicity. Do not create a
pass result on their behalf.

- [ ] **Step 4: Record the user's explicit review result outside dataset files**

After a pass, create a local `review_attestation.json` containing only result,
review timestamp, dataset manifest hash, and the phrase `user-reviewed`; do
not include sample text or user identity. If review fails, return to the Task
1 policy through a new failing test before rebuilding.

- [ ] **Step 5: Build and validate the portable ZIP**

Run:

```bash
python scripts/package_kanagawawave_v2_training.py \
  --dataset-dir datasets/kanagawawave_v2 \
  --output-zip datasets/kanagawawave_v2/kanagawawave-v2-training-bundle.zip
python scripts/validate_kanagawawave_v2_bundle.py \
  datasets/kanagawawave_v2/kanagawawave-v2-training-bundle.zip
```

Expected: validation reports exact counts, matching hashes and prompt, and no
checkpoint/source metadata.

- [ ] **Step 6: Confirm no generated artifact is staged by Git**

Run: `git status --short --ignored datasets/kanagawawave_v2`

Expected: the directory is ignored and no contained file is staged.

### Task 7: Repository-wide verification and handoff

**Files:**
- Review: all files changed by Tasks 1–5.

**Interfaces:**
- Consumes: the completed implementation and local dataset package.
- Produces: final verification evidence and training handoff instructions.

- [ ] **Step 1: Run focused test suites**

```bash
python -m unittest tests.test_kanagawawave_v2_dataset tests.test_kanagawawave_v2_bundle -v
python -m unittest tests.test_kanagawawave_dataset tests.test_group_chat tests.test_community_style_acceptance -v
```

Expected: PASS.

- [ ] **Step 2: Run the complete repository suite**

Run: `python -m unittest discover -s tests -v`

Expected: PASS. Record every unrelated failure by exact test name instead of
omitting it.

- [ ] **Step 3: Run static and diff checks**

```bash
python -m compileall -q src scripts tests
git diff --check
git status --short
```

Expected: compile and diff checks are silent; only the preserved untracked
`.venv` and `node_modules` may remain.

- [ ] **Step 4: Inspect the complete branch diff**

Run `git diff HEAD~5..HEAD --stat` and inspect every changed file. Confirm no
raw dataset, archive content, secrets, model weights, checkpoint, or review
attestation is committed.

- [ ] **Step 5: Deliver the handoff**

Provide links to the local training ZIP, dataset manifest, human review file,
specification, and this plan. State that the current QQ deployment remains on
v1 and that importing or activating a returned v2 model will require a later
explicit action-time confirmation.
