#!/usr/bin/env python3
"""Validate a portable KanagawaWave v2 training bundle without network access."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Mapping
import unicodedata
import zipfile


KANAGAWAWAVE_V2_INSTRUCTION = (
    "[COMMUNITY=KANAGAWAVE] "
    "以该社区的常见语气自然回复下面的内容。只输出回复正文。"
)
KANAGAWAWAVE_V2_PROFILE_KEY = "kanagawave-v2-model-style"
EXPECTED_COUNTS = {"train": 10_000, "test": 1_000, "review": 100}
EXPECTED_HASH_PATHS = frozenset(
    {
        "INFERENCE_PROMPT.txt",
        "README_zh.md",
        "configs/chat.yaml",
        "configs/merge.yaml",
        "configs/train_4060_8gb.yaml",
        "configs/train_4060_low_memory.yaml",
        "data/dataset_info.json",
        "data/forum_test.json",
        "data/forum_train.json",
        "human_review.json",
        "manifest.json",
        "scripts/validate_kanagawawave_v2_bundle.py",
    }
)
EXPECTED_FILES = EXPECTED_HASH_PATHS | {"DATASET_LOCK.json"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON file: %s" % path.name) from error


def _regular_files(root: Path):
    files = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("bundle must not contain symbolic links")
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
    return files


def _validate_lock(root: Path) -> Mapping[str, object]:
    lock = _load_json(root / "DATASET_LOCK.json")
    if not isinstance(lock, dict) or set(lock) != {
        "version",
        "profile_key",
        "counts",
        "sha256",
    }:
        raise ValueError("invalid dataset lock schema")
    if lock["version"] != 1 or lock["profile_key"] != KANAGAWAWAVE_V2_PROFILE_KEY:
        raise ValueError("invalid dataset lock version or profile")
    if lock["counts"] != EXPECTED_COUNTS:
        raise ValueError("dataset lock must declare exactly 10000 train, 1000 test, 100 review")
    hashes = lock["sha256"]
    if not isinstance(hashes, dict) or set(hashes) != EXPECTED_HASH_PATHS:
        raise ValueError("dataset lock hash inventory is invalid")
    for relative, expected in sorted(hashes.items()):
        if not isinstance(expected, str) or _SHA256_RE.fullmatch(expected) is None:
            raise ValueError("dataset lock contains an invalid hash")
        path = root / relative
        if not path.is_file() or _sha256(path) != expected:
            raise ValueError("bundle hash mismatch: %s" % relative)
    return lock


def _validate_rows(path: Path, expected_count: int):
    rows = _load_json(path)
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise ValueError("%s must contain exactly %d rows" % (path.name, expected_count))
    pair_keys = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"instruction", "input", "output"}:
            raise ValueError("invalid Alpaca row at %s:%d" % (path.name, index))
        if row["instruction"] != KANAGAWAWAVE_V2_INSTRUCTION:
            raise ValueError("invalid v2 instruction or v1 style label")
        if not isinstance(row["input"], str) or not row["input"]:
            raise ValueError("invalid input text at %s:%d" % (path.name, index))
        if not isinstance(row["output"], str) or not row["output"]:
            raise ValueError("invalid output text at %s:%d" % (path.name, index))
        pair = (
            unicodedata.normalize("NFC", row["input"]),
            unicodedata.normalize("NFC", row["output"]),
        )
        if pair[0] == pair[1]:
            raise ValueError("input equals output at %s:%d" % (path.name, index))
        if pair in pair_keys:
            raise ValueError("duplicate Alpaca row in %s" % path.name)
        pair_keys.add(pair)
    return rows, pair_keys


def _validate_manifest(root: Path) -> None:
    manifest = _load_json(root / "manifest.json")
    required = {
        "version",
        "human_review_required",
        "counts",
        "audit",
        "file_sha256",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise ValueError("invalid v2 manifest")
    if manifest["version"] != 2 or manifest["human_review_required"] is not True:
        raise ValueError("invalid v2 manifest version")
    if manifest["counts"] != EXPECTED_COUNTS:
        raise ValueError("manifest must declare exactly 10000 train, 1000 test, 100 review")
    audit = manifest["audit"]
    if (
        not isinstance(audit, dict)
        or audit.get("thread_overlap_count") != 0
        or audit.get("duplicate_example_count") != 0
    ):
        raise ValueError("manifest audit is not clean")
    expected_hashes = manifest["file_sha256"]
    source_files = {
        "forum_train.json": root / "data" / "forum_train.json",
        "forum_test.json": root / "data" / "forum_test.json",
        "human_review.json": root / "human_review.json",
        "inference_prompt.txt": root / "INFERENCE_PROMPT.txt",
    }
    if not isinstance(expected_hashes, dict) or set(expected_hashes) != set(source_files):
        raise ValueError("manifest hash inventory is invalid")
    for filename, path in source_files.items():
        if expected_hashes[filename] != _sha256(path):
            raise ValueError("manifest hash mismatch: %s" % filename)


def _validate_template(root: Path) -> None:
    prompt_bytes = KANAGAWAWAVE_V2_INSTRUCTION.encode("utf-8")
    if (root / "INFERENCE_PROMPT.txt").read_bytes() != prompt_bytes:
        raise ValueError("training inference prompt is not the exact v2 prompt")
    registry = _load_json(root / "data" / "dataset_info.json")
    expected_columns = {
        "prompt": "instruction",
        "query": "input",
        "response": "output",
    }
    expected_registry = {
        "forum_train_v2": {
            "file_name": "forum_train.json",
            "columns": expected_columns,
        },
        "forum_test_v2": {
            "file_name": "forum_test.json",
            "columns": expected_columns,
        },
    }
    if registry != expected_registry:
        raise ValueError("invalid Alpaca dataset registry")


def validate_training_bundle(root: Path) -> Mapping[str, object]:
    """Validate an extracted training bundle and return a stable summary."""

    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("bundle root must be a directory")
    files = _regular_files(root)
    if files != EXPECTED_FILES:
        raise ValueError("bundle file inventory is invalid")
    _validate_lock(root)
    _validate_manifest(root)
    _validate_template(root)
    train, train_pairs = _validate_rows(root / "data" / "forum_train.json", 10_000)
    test, test_pairs = _validate_rows(root / "data" / "forum_test.json", 1_000)
    review, _ = _validate_rows(root / "human_review.json", 100)
    if train_pairs & test_pairs:
        raise ValueError("train and test contain overlapping examples")
    test_rows = {
        (row["instruction"], row["input"], row["output"]) for row in test
    }
    if any(
        (row["instruction"], row["input"], row["output"]) not in test_rows
        for row in review
    ):
        raise ValueError("human review rows must be drawn from the test split")
    return {"status": "valid", "counts": dict(EXPECTED_COUNTS)}


def _validate_zip(path: Path) -> Mapping[str, object]:
    with tempfile.TemporaryDirectory(prefix="kanagawawave-v2-validate-") as temporary:
        root = Path(temporary)
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    member = PurePosixPath(info.filename)
                    if member.is_absolute() or ".." in member.parts:
                        raise ValueError("unsafe ZIP member")
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("ZIP must not contain symbolic links")
                archive.extractall(root)
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError("invalid training bundle ZIP") from error
        return validate_training_bundle(root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        summary = (
            _validate_zip(arguments.bundle)
            if arguments.bundle.is_file()
            else validate_training_bundle(arguments.bundle)
        )
    except ValueError as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
