"""Reject-only KanagawaWave dataset builder primitives."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Mapping, Optional, Sequence, Tuple
import unicodedata

from qq_openclaw_bridge.community_prompts import KANAGAWAWAVE_V2_INSTRUCTION
from qq_openclaw_bridge.dataset import kanagawawave as _v1
from qq_openclaw_bridge.dataset.kanagawawave import RawCandidate


V2_POLICY_VERSION = "kanagawawave-v2-reject-only-1"

_DANGEROUS_MARKERS = (
    "制作炸弹",
    "自制爆炸物",
    "投毒步骤",
    "入侵账号步骤",
)
_TARGETED_HARASSMENT_MARKERS = (
    "人肉这个",
    "曝光住址",
    "开盒这个",
    "网暴这个",
)
_V2_AD_MARKERS = _v1._AD_MARKERS + (
    "加群领取",
    "限时推广",
)


@dataclass(frozen=True)
class V2BuildConfig:
    database: Optional[Path]
    archive: Optional[Path]
    output_dir: Path
    train_size: int = 10_000
    test_size: int = 1_000
    review_size: int = 100
    split_seed: str = "kanagawave-v2"
    max_per_author_train: int = 50
    max_per_author_test: int = 10
    max_short_ratio: float = 0.15


@dataclass(frozen=True)
class V2Selection:
    train: Tuple[RawCandidate, ...]
    test: Tuple[RawCandidate, ...]
    thread_tokens: Mapping[str, frozenset]
    author_counts: Mapping[str, Mapping[str, int]]
    rejection_counts: Mapping[str, int]
    audit: Mapping[str, object]


def _text_reason(text: object, *, maximum: int) -> Optional[str]:
    if not isinstance(text, str):
        return "invalid_text"
    if not text.strip():
        return "garbage"
    if len(text) > maximum:
        return "overlong"
    if _v1._contains_control(text) or "�" in text:
        return "invalid_text"
    lowered = text.casefold()
    if lowered.strip() in _v1._REMOVED_VALUES:
        return "removed"
    if _v1._MEDIA_ONLY_RE.fullmatch(text):
        return "media_only"
    if (
        _v1._URL_RE.search(text)
        or _v1._NON_HIERARCHICAL_URI_RE.search(text)
        or _v1._DOMAIN_URL_RE.search(text)
    ):
        return "url"
    if any(marker.casefold() in lowered for marker in _V2_AD_MARKERS):
        return "advertisement"
    if any(marker in lowered for marker in _v1._BOT_MARKERS):
        return "bot"
    if (
        _v1._EMAIL_RE.search(text)
        or _v1._PHONE_RE.search(text)
        or _v1._REDDIT_USER_RE.search(text)
        or _v1._HANDLE_RE.search(text)
        or _v1._IPV4_RE.search(text)
        or _v1._ACCOUNT_RE.search(text)
        or _v1._ADDRESS_CUE_RE.search(text)
    ):
        return "privacy"
    if any(marker in text for marker in _DANGEROUS_MARKERS):
        return "dangerous_content"
    if any(marker in text for marker in _TARGETED_HARASSMENT_MARKERS):
        return "targeted_harassment"
    if _v1._obvious_garbage(text):
        return "garbage"
    return None


def rejection_reason(candidate: RawCandidate) -> Optional[str]:
    """Return a stable rejection class, or ``None`` for an eligible pair."""

    if not isinstance(candidate, RawCandidate):
        return "invalid_text"
    for text, maximum in (
        (candidate.parent_text, 4000),
        (candidate.reply_text, 2000),
    ):
        reason = _text_reason(text, maximum=maximum)
        if reason is not None:
            return reason
    if unicodedata.normalize("NFC", candidate.parent_text) == unicodedata.normalize(
        "NFC", candidate.reply_text
    ):
        return "input_equals_output"
    return None


def v2_candidate_split(thread_id: str, seed: str) -> str:
    """Assign a complete thread to the deterministic v2 90/10 split."""

    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("invalid thread id")
    if not isinstance(seed, str) or not seed:
        raise ValueError("invalid split seed")
    digest = hashlib.sha256((seed + "\0" + thread_id).encode("utf-8")).digest()
    return "test" if int.from_bytes(digest[:8], "big") % 10 == 0 else "train"


def _private_token(seed: str, purpose: str, value: str) -> str:
    material = "\0".join((seed, purpose, value)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _sample_rank(candidate: RawCandidate, seed: str) -> Tuple[bytes, str]:
    material = "\0".join(
        (seed, "sample", candidate.thread_id, candidate.comment_id)
    ).encode("utf-8")
    return hashlib.sha256(material).digest(), candidate.comment_id


def _pair_key(candidate: RawCandidate) -> Tuple[str, str]:
    return (
        unicodedata.normalize("NFC", candidate.parent_text),
        unicodedata.normalize("NFC", candidate.reply_text),
    )


def _is_short(text: str) -> bool:
    return sum(character.isalnum() for character in text) < 6


def _validate_selection_config(config: V2BuildConfig) -> None:
    if not isinstance(config, V2BuildConfig):
        raise _v1.DatasetBuildError("invalid v2 dataset configuration")
    if config.train_size < 0 or config.test_size < 0:
        raise _v1.DatasetBuildError("dataset sizes must be non-negative")
    if config.train_size + config.test_size < 1:
        raise _v1.DatasetBuildError("dataset must contain at least one example")
    if config.review_size < 0 or config.review_size > config.test_size:
        raise _v1.DatasetBuildError("review size exceeds test size")
    if config.max_per_author_train < 1 or config.max_per_author_test < 1:
        raise _v1.DatasetBuildError("author caps must be positive")
    if not 0.0 <= config.max_short_ratio <= 1.0:
        raise _v1.DatasetBuildError("max_short_ratio must be between zero and one")
    if not isinstance(config.split_seed, str) or not config.split_seed:
        raise _v1.DatasetBuildError("split_seed must be non-empty")


def select_v2_candidates(
    candidates: Sequence[RawCandidate],
    config: V2BuildConfig,
) -> V2Selection:
    """Select exact deterministic v2 splits while retaining source strings."""

    _validate_selection_config(config)
    selected = {"train": [], "test": []}
    author_counts = {"train": Counter(), "test": Counter()}
    thread_tokens = {"train": set(), "test": set()}
    short_counts = {"train": 0, "test": 0}
    targets = {"train": config.train_size, "test": config.test_size}
    author_caps = {
        "train": config.max_per_author_train,
        "test": config.max_per_author_test,
    }
    short_limits = {
        split: math.floor(targets[split] * config.max_short_ratio)
        for split in ("train", "test")
    }
    rejections = Counter()
    seen_pairs = set()

    for candidate in sorted(candidates, key=lambda item: _sample_rank(item, config.split_seed)):
        reason = rejection_reason(candidate)
        if reason is not None:
            rejections[reason] += 1
            continue
        pair = _pair_key(candidate)
        if pair in seen_pairs:
            rejections["duplicate"] += 1
            continue
        seen_pairs.add(pair)
        split = v2_candidate_split(candidate.thread_id, config.split_seed)
        if len(selected[split]) >= targets[split]:
            rejections["split_surplus"] += 1
            continue
        if author_counts[split][candidate.author_hash] >= author_caps[split]:
            rejections["author_cap"] += 1
            continue
        short = _is_short(candidate.reply_text)
        if short and short_counts[split] >= short_limits[split]:
            rejections["short_cap"] += 1
            continue
        selected[split].append(candidate)
        author_counts[split][candidate.author_hash] += 1
        thread_tokens[split].add(
            _private_token(config.split_seed, "thread", candidate.thread_id)
        )
        if short:
            short_counts[split] += 1

    if any(len(selected[split]) != targets[split] for split in ("train", "test")):
        raise _v1.DatasetBuildError(
            "insufficient safe candidates for requested dataset sizes"
        )
    overlap = thread_tokens["train"] & thread_tokens["test"]
    if overlap:
        raise _v1.DatasetBuildError("internal dataset audit failed")
    audit = {
        "source_candidate_count": len(candidates),
        "selected_train": len(selected["train"]),
        "selected_test": len(selected["test"]),
        "unique_threads_train": len(thread_tokens["train"]),
        "unique_threads_test": len(thread_tokens["test"]),
        "thread_overlap_count": 0,
        "short_replies_train": short_counts["train"],
        "short_replies_test": short_counts["test"],
        "max_author_examples_train": max(author_counts["train"].values(), default=0),
        "max_author_examples_test": max(author_counts["test"].values(), default=0),
    }
    return V2Selection(
        train=tuple(selected["train"]),
        test=tuple(selected["test"]),
        thread_tokens={
            "train": frozenset(thread_tokens["train"]),
            "test": frozenset(thread_tokens["test"]),
        },
        author_counts={
            "train": dict(author_counts["train"]),
            "test": dict(author_counts["test"]),
        },
        rejection_counts=dict(sorted(rejections.items())),
        audit=audit,
    )


def style_signal_audit(
    examples: Sequence[Mapping[str, str]],
) -> Mapping[str, object]:
    """Return aggregate style diagnostics without complete source phrases."""

    total = len(examples)
    buckets = Counter()
    emoji_count = 0
    repeated_count = 0
    mixed_count = 0
    question_count = 0
    ngrams = Counter()
    repeated_punctuation = re.compile(r"[!?？！。.，,~～]{2,}")
    for example in examples:
        if not isinstance(example, Mapping) or not isinstance(example.get("output"), str):
            raise ValueError("invalid example")
        output = example["output"]
        length = len(output)
        if length < 6:
            buckets["under_6"] += 1
        elif length <= 40:
            buckets["6_to_40"] += 1
        elif length <= 160:
            buckets["41_to_160"] += 1
        else:
            buckets["over_160"] += 1
        emoji_count += bool(_v1._EMOJI_RE.search(output))
        repeated_count += bool(repeated_punctuation.search(output))
        has_ascii_letters = bool(re.search(r"[A-Za-z]", output))
        has_cjk = bool(re.search(r"[\u3400-\u9fff]", output))
        mixed_count += has_ascii_letters and has_cjk
        question_count += "?" in output or "？" in output

        filtered = "".join(
            character
            for character in unicodedata.normalize("NFC", output)
            if not character.isspace()
            and not character.isdigit()
            and not (character.isascii() and character.isalnum())
            and not unicodedata.category(character).startswith("C")
        )
        for size in (1, 2, 3):
            for offset in range(max(0, len(filtered) - size + 1)):
                ngrams[filtered[offset : offset + size]] += 1

    def rate(count: int) -> float:
        return count / total if total else 0.0

    return {
        "reply_length_buckets": {
            key: buckets.get(key, 0)
            for key in ("under_6", "6_to_40", "41_to_160", "over_160")
        },
        "emoji_rate": rate(emoji_count),
        "repeated_punctuation_rate": rate(repeated_count),
        "code_switching_rate": rate(mixed_count),
        "question_rate": rate(question_count),
        "common_character_ngrams": [
            {"ngram": ngram, "count": count}
            for ngram, count in sorted(
                ngrams.items(), key=lambda item: (-item[1], item[0])
            )[:20]
        ],
    }


_ARTIFACT_NAMES = frozenset(
    {
        "forum_train.json",
        "forum_test.json",
        "manifest.json",
        "human_review.json",
        "inference_prompt.txt",
        "checkpoint.json",
    }
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _candidate_token(candidate: RawCandidate, seed: str) -> str:
    value = "\0".join(
        (
            candidate.thread_id,
            candidate.comment_id,
            candidate.author_hash,
            candidate.parent_text,
            candidate.reply_text,
        )
    )
    return _private_token(seed, "candidate", value)


def _source_token(candidates: Sequence[RawCandidate], seed: str) -> str:
    tokens = sorted(_candidate_token(candidate, seed) for candidate in candidates)
    return _private_token(seed, "source", "".join(tokens))


def _configuration_token(config: V2BuildConfig, source_token: str) -> str:
    value = json.dumps(
        {
            "policy": V2_POLICY_VERSION,
            "source_token": source_token,
            "train_size": config.train_size,
            "test_size": config.test_size,
            "review_size": config.review_size,
            "split_seed": config.split_seed,
            "max_per_author_train": config.max_per_author_train,
            "max_per_author_test": config.max_per_author_test,
            "max_short_ratio": config.max_short_ratio,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _private_token(config.split_seed, "configuration", value)


@contextmanager
def _source_path(config: V2BuildConfig):
    if (config.database is None) == (config.archive is None):
        raise _v1.DatasetBuildError("exactly one input source is required")
    if config.database is not None:
        yield Path(config.database)
        return
    with _v1.extract_database_from_archive(Path(config.archive)) as extracted:
        yield extracted


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _safe_checkpoint(
    config: V2BuildConfig,
    source_token: str,
    configuration_token: str,
    selection: V2Selection,
) -> Mapping[str, object]:
    entries = []
    for split, candidates in (("train", selection.train), ("test", selection.test)):
        for candidate in candidates:
            entries.append(
                {
                    "candidate_token": _candidate_token(candidate, config.split_seed),
                    "split": split,
                    "status": "selected",
                }
            )
    return {
        "version": 1,
        "configuration_token": configuration_token,
        "source_token": source_token,
        "entries": entries,
    }


def _validated_checkpoint(
    value: object,
    *,
    configuration_token: str,
    source_token: str,
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "configuration_token",
        "source_token",
        "entries",
    }:
        raise ValueError
    if (
        value["version"] != 1
        or value["configuration_token"] != configuration_token
        or value["source_token"] != source_token
        or not isinstance(value["entries"], list)
    ):
        raise ValueError
    for entry in value["entries"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"candidate_token", "split", "status"}
            or not isinstance(entry["candidate_token"], str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["candidate_token"]) is None
            or entry["split"] not in {"train", "test"}
            or entry["status"] != "selected"
        ):
            raise ValueError


def _existing_manifest(
    output_dir: Path,
    *,
    configuration_token: str,
    source_token: str,
) -> Mapping[str, object]:
    try:
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise ValueError
        files = {path.name for path in output_dir.iterdir() if path.is_file()}
        if files != _ARTIFACT_NAMES:
            raise ValueError
        manifest = json.loads((output_dir / "manifest.json").read_text("utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 2
            or manifest.get("configuration_token") != configuration_token
        ):
            raise ValueError
        expected_hashes = manifest.get("file_sha256")
        if not isinstance(expected_hashes, dict) or set(expected_hashes) != {
            "forum_train.json",
            "forum_test.json",
            "human_review.json",
            "inference_prompt.txt",
        }:
            raise ValueError
        for filename, expected in expected_hashes.items():
            if not isinstance(expected, str) or _sha256(
                (output_dir / filename).read_bytes()
            ) != expected:
                raise ValueError
        if (output_dir / "inference_prompt.txt").read_text("utf-8") != KANAGAWAWAVE_V2_INSTRUCTION:
            raise ValueError
        checkpoint = json.loads((output_dir / "checkpoint.json").read_text("utf-8"))
        _validated_checkpoint(
            checkpoint,
            configuration_token=configuration_token,
            source_token=source_token,
        )
        return manifest
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise _v1.DatasetBuildError("existing dataset is invalid") from None


def build_v2_dataset(config: V2BuildConfig) -> Mapping[str, object]:
    """Build and atomically publish an exact-sized reject-only v2 dataset."""

    _validate_selection_config(config)
    output_dir = Path(config.output_dir)
    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with _source_path(config) as database_path:
            candidates = _v1.load_candidates(database_path)
    except _v1.DatasetBuildError:
        raise
    except (
        _v1.ArchiveExtractionError,
        _v1.DatabaseSchemaError,
        OSError,
        sqlite3.Error,
    ):
        raise _v1.DatasetBuildError("input database is unavailable or invalid") from None

    source_token = _source_token(candidates, config.split_seed)
    configuration_token = _configuration_token(config, source_token)
    if output_dir.exists():
        return _existing_manifest(
            output_dir,
            configuration_token=configuration_token,
            source_token=source_token,
        )

    selection = select_v2_candidates(candidates, config)
    train = [example_from_candidate(candidate) for candidate in selection.train]
    test = [example_from_candidate(candidate) for candidate in selection.test]
    review_candidates = sorted(
        selection.test,
        key=lambda item: _sample_rank(item, config.split_seed + "\0review"),
    )[: config.review_size]
    review = [example_from_candidate(candidate) for candidate in review_candidates]

    payloads = {
        "forum_train.json": _json_bytes(train),
        "forum_test.json": _json_bytes(test),
        "human_review.json": _json_bytes(review),
        "inference_prompt.txt": KANAGAWAWAVE_V2_INSTRUCTION.encode("utf-8"),
    }
    pair_keys = {
        (
            unicodedata.normalize("NFC", example["input"]),
            unicodedata.normalize("NFC", example["output"]),
        )
        for example in train + test
    }
    duplicate_count = len(train) + len(test) - len(pair_keys)
    if duplicate_count or selection.audit["thread_overlap_count"]:
        raise _v1.DatasetBuildError("internal dataset audit failed")
    manifest = {
        "version": 2,
        "configuration_token": configuration_token,
        "human_review_required": True,
        "counts": {
            "train": len(train),
            "test": len(test),
            "review": len(review),
        },
        "limits": {
            "max_per_author_train": config.max_per_author_train,
            "max_per_author_test": config.max_per_author_test,
            "max_short_ratio": config.max_short_ratio,
        },
        "audit": dict(selection.audit, duplicate_example_count=duplicate_count),
        "rejection_counts": dict(selection.rejection_counts),
        "style_signals": {
            "train": style_signal_audit(train),
            "test": style_signal_audit(test),
        },
        "file_sha256": {
            filename: _sha256(payload) for filename, payload in sorted(payloads.items())
        },
    }
    checkpoint = _safe_checkpoint(
        config,
        source_token,
        configuration_token,
        selection,
    )
    stage = Path(
        tempfile.mkdtemp(
            prefix="." + output_dir.name + ".stage-",
            dir=str(output_dir.parent),
        )
    )
    os.chmod(stage, 0o700)
    try:
        for filename, payload in payloads.items():
            _write_private_file(stage / filename, payload)
        _write_private_file(stage / "checkpoint.json", _json_bytes(checkpoint))
        _write_private_file(stage / "manifest.json", _json_bytes(manifest))
        os.replace(stage, output_dir)
    except OSError:
        raise _v1.DatasetBuildError("dataset publication failed") from None
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return manifest


def example_from_candidate(candidate: RawCandidate) -> Mapping[str, str]:
    """Return an Alpaca row without rewriting eligible source strings."""

    if not isinstance(candidate, RawCandidate):
        raise ValueError("invalid candidate")
    if rejection_reason(candidate) is not None:
        raise ValueError("candidate rejected")
    return {
        "instruction": KANAGAWAWAVE_V2_INSTRUCTION,
        "input": candidate.parent_text,
        "output": candidate.reply_text,
    }


__all__ = (
    "V2BuildConfig",
    "V2_POLICY_VERSION",
    "V2Selection",
    "build_v2_dataset",
    "example_from_candidate",
    "rejection_reason",
    "select_v2_candidates",
    "style_signal_audit",
    "v2_candidate_split",
)
