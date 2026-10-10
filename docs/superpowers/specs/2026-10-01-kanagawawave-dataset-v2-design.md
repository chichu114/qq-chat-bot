# KanagawaWave Dataset V2 Design

## Purpose

Build a training and test dataset that teaches Qwen3-4B the aggregate
KanagawaWave community voice: slang, sentence shape, code-switching,
punctuation, emoji use, jokes, and reply timing. The target is a community
style, not imitation of a named or identifiable person.

The user will train the model on another computer. This repository must
produce the dataset, audit artifacts, matching LLaMA-Factory configuration,
and the exact inference prompt contract. It must not train a model or change
the currently deployed QQ group model during this work.

## Background and Failure Being Corrected

Dataset v1 neutralized every source pair by replacing its scene, facts,
nouns, and actions with one of a small number of household-task templates.
That preserved privacy but erased most community-specific language. Of the
2,000 training rows, 1,954 were labelled `STYLE=NORMAL`, while only six were
`STYLE=FORUM` and six were `STYLE=MEME`; the inputs contained only 80 unique
templates. The deployed model therefore learned generic Chinese assistant
replies rather than KanagawaWave style.

Dataset v2 removes semantic neutralization. Because the source archive and
its relevant posts are authorized for this local use, v2 preserves eligible
parent and reply text exactly after Unicode validity checks. Samples that
contain privacy or safety risks are rejected as a whole instead of rewritten.

## Scope

### Included

- Read the single permitted SQLite member
  `data/db/KanagawaWave.sqlite3` from the authorized local archive.
- Recover direct parent-to-reply pairs from submissions and comments.
- Reject unsafe or unsuitable pairs without paraphrasing accepted text.
- Split by thread before sampling.
- Enforce duplicate, author, short-reply, and quality limits.
- Emit 10,000 training rows and 1,000 test rows in LLaMA-Factory Alpaca
  format.
- Emit an aggregate audit report, a deterministic human-review sample, and
  a private checkpoint that supports restart without storing raw source text.
- Update the portable LLaMA-Factory bundle configuration and documentation
  to use the v2 files and the exact inference prompt contract.
- Add automated tests for source safety, selection, publication, and prompt
  alignment.

### Excluded

- Crawling Reddit or any other live service.
- Training, merging, quantizing, or importing a model.
- Changing the active Ollama model, QQ group binding, trigger behavior, or
  sending a QQ message.
- Adding per-user personas or retaining author identifiers in published
  datasets.
- Multi-turn reconstruction in v2. Direct parent-to-reply pairs are the
  deliberately smaller first target.

## Selected Approach

Use real authorized parent-to-reply text with reject-only safety filtering.
Accepted text is not corrected, translated, summarized, paraphrased, or
normalized before publication. NFC-normalized copies may be used only for
comparison and hashes; the emitted strings remain the validated source
strings.

This approach is preferred over style-preserving paraphrase because the v1
failure showed that rewriting is the largest source of style loss. It is
preferred over synthetic augmentation because the archive already contains
enough candidate pairs and synthetic rows would make both authorship balance
and style fidelity harder to measure.

## Dataset Contract

Every published row contains exactly these string fields:

```json
{
  "instruction": "[COMMUNITY=KANAGAWAVE] 以该社区的常见语气自然回复下面的内容。只输出回复正文。",
  "input": "<validated parent text>",
  "output": "<validated direct reply text>"
}
```

The instruction is identical for train, test, local evaluation, and future
QQ inference. There are no `NORMAL`, `SERIOUS`, `FORUM`, or `MEME` labels.
The natural distribution of the source replies teaches when a plain,
technical, joking, short, or emphatic answer is appropriate.

Published rows contain no source IDs, thread IDs, author hashes, timestamps,
or provenance fields. Those values are used only while selecting and
auditing rows.

## Candidate Recovery

The existing safe archive boundary remains in force:

- invoke `tar --zstd -xOf` with an argument vector, never a shell;
- request only `data/db/KanagawaWave.sqlite3`;
- stream into a mode-0600 temporary file with a 4 GiB limit;
- open SQLite read-only with `query_only` enabled;
- validate the exact `records` schema before reading content;
- delete the temporary database on success, failure, or interruption.

A candidate consists of one comment and its direct parent. The parent can be
a submission or another comment, but both records must belong to
KanagawaWave and to the same thread. Orphans, ambiguous duplicate IDs, and
cross-community parents are rejected.

## Reject-Only Safety and Quality Policy

Reject the whole pair if either side contains any of the following:

- a phone number, email address, IP address, detailed street address, QQ or
  WeChat contact solicitation, or an explicit username/mention;
- a URL, referral link, advertisement, sales solicitation, or bot notice;
- deleted/removed markers, media-only placeholders, or replacement/control
  characters;
- content outside the existing bounded parent/reply length limits;
- empty text, pure punctuation, pure URL-like material, or obvious garbage;
- a large quoted block that primarily repeats another message;
- disallowed dangerous instructions or targeted hateful/harassing content
  that should not be reproduced by the group agent.

Do not replace rejected fragments with placeholders. Placeholder insertion
would alter punctuation and rhythm and could teach the model artificial
tokens. Borderline cases fail closed and are counted by aggregate rejection
reason without recording their text.

Accepted strings preserve spelling, casing, whitespace meaningful to the
reply, emoji, repeated punctuation, abbreviations, `.jpg`-style jokes,
parenthetical expressions, and Chinese/English mixing.

## Splitting and Sampling

- Assign entire threads to train or test using a stable salted hash before
  any sampling. The target ratio is 90/10 and thread overlap must be zero.
- Publish exactly 10,000 train rows and 1,000 test rows. If the policy cannot
  produce both exact counts, publish neither dataset and return a safe
  shortage error.
- Use deterministic seeded ordering so the same archive, seed, and policy
  version produce byte-identical outputs.
- Permit at most 50 rows from one author in train and at most 10 in test.
- Cap very short replies at 15 percent per split. Very short means fewer
  than six Unicode alphanumeric characters after comparison-only
  normalization.
- Remove exact and NFC-equivalent duplicate pairs across the entire build.
  A pair may appear in only one split.
- Reject a candidate whose output is an exact or NFC-equivalent copy of its
  input.
- Do not balance by heuristic style labels. Sampling must preserve the
  naturally occurring mix after author and short-reply caps.

The dataset builder must record selection counts and rejection counts by
safe reason so skew can be diagnosed without exposing source content.

## Publication and Restart Safety

Generate into a private staging directory and atomically replace all final
artifacts only after every validation passes. Existing v1 files remain
untouched until the complete v2 artifact set is ready.

The private checkpoint may contain salted candidate tokens, split,
selection state, and safe status values. It must not contain raw text,
public post IDs, thread IDs, author hashes, or unsalted content hashes.

The final v2 directory contains:

- `forum_train.json` — 10,000 Alpaca rows;
- `forum_test.json` — 1,000 held-out Alpaca rows;
- `manifest.json` — policy version, counts, limits, aggregate audits, and
  hashes of the published files;
- `human_review.json` — 100 deterministic held-out examples copied from the
  test set for manual review;
- `inference_prompt.txt` — the exact instruction line used by all rows;
- `checkpoint.json` — the private restart state, mode 0600.

The dataset files and review file are authorized local training artifacts.
They should remain local and should not be committed to Git. Code,
configuration, tests, manifests without content, and documentation may be
committed.

## Audit and Acceptance Criteria

Automated publication fails unless all of these are true:

- train count is 10,000 and test count is 1,000;
- every row has exactly `instruction`, `input`, and `output`;
- every instruction equals the prompt contract byte-for-byte;
- thread overlap, duplicate-pair count, and input-equals-output count are
  zero;
- published PII, URL, ad, bot-notice, removed-content, and invalid-control
  findings are zero under the implemented detectors;
- per-author and short-reply limits are respected in each split;
- author diversity and unique-input/output counts are reported;
- no single normalized output accounts for more than one row;
- the 100-row review set is drawn only from test and is deterministic;
- a second build with the same archive and seed is byte-identical.

The manifest also reports aggregate style signals for train and test:
reply-length buckets, emoji rate, repeated-punctuation rate, code-switching
rate, question rate, and common non-identifying character n-grams. These are
diagnostics, not sampling targets. They must not include complete source
sentences or identifiers.

Human review is required before training. Reviewers check the 100 held-out
examples for context-response coherence, accidental personal information,
community-style fidelity, and excessive toxicity. The build is accepted
only after the reviewer records a pass outside the dataset files; the
builder does not fabricate a human-review result.

## Training and Inference Alignment

The portable LLaMA-Factory package registers the v2 train and test files and
keeps Qwen3-4B-Instruct-2507, QLoRA, `qwen3_nothink`, rank 8, alpha 16,
dropout 0.05, learning rate 1e-4, and two epochs as the initial experiment.
The package documentation must identify the expected v2 file hashes and
must refuse training when those hashes or row counts differ.

Future QQ inference must send the same community instruction immediately
before the current group message. Safety and AI-identity system rules remain
separate and higher priority. The runtime prompt change is prepared and
tested as part of the package, but it is not activated against the current
v1 model. Activation occurs only after the user supplies a successfully
trained and merged v2 model.

## Error Handling

All expected failures use stable, non-content-bearing messages. No exception
may include source text. Schema mismatch, archive extraction failure,
unsafe-content rejection, candidate shortage, checkpoint mismatch, output
validation failure, and interrupted publication are distinct safe error
classes.

An interrupted or failed build removes staging files and leaves the last
complete published dataset unchanged. A checkpoint from another policy
version, archive fingerprint, seed, or target count is rejected rather than
silently reused.

## Test Strategy

Unit tests use small synthetic SQLite fixtures and literal safe/unsafe text
cases. They cover parent recovery, exact preservation of eligible style
signals, every rejection class, deduplication, deterministic thread split,
author and short-reply caps, exact-count shortage, checkpoint privacy, and
atomic publication.

Integration tests build a small deterministic v2 dataset twice and assert
byte identity, zero thread overlap, exact schema, exact prompt alignment,
review-set provenance, safe manifest contents, and no partial output after a
forced failure.

Portable-package tests verify dataset registration, count/hash validation,
the QLoRA configuration, and the inference prompt contract. Existing v1
tests remain as historical regression coverage unless they conflict with an
explicit v2 path; v2 is introduced under a new output directory and policy
version.

## Rollout

1. Implement and verify the v2 builder and portable-package checks.
2. Build the v2 artifacts from the authorized local archive.
3. Inspect the manifest and complete the 100-row human review.
4. Deliver the v2 training bundle for the user's other computer.
5. The user trains, evaluates, merges, and returns the model package.
6. Verify the returned package and run local, non-QQ A/B prompts.
7. With separate action-time confirmation, switch the active Ollama model
   and compatible runtime prompt; do not change `@`/reply triggering or the
   group binding.

