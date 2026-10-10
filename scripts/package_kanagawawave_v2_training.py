#!/usr/bin/env python3
"""Package a complete KanagawaWave v2 dataset for offline QLoRA training."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Mapping
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.validate_kanagawawave_v2_bundle import (
    EXPECTED_COUNTS,
    EXPECTED_HASH_PATHS,
    KANAGAWAWAVE_V2_INSTRUCTION,
    KANAGAWAWAVE_V2_PROFILE_KEY,
    validate_training_bundle,
)


TEMPLATE_ROOT = PROJECT_ROOT / "training" / "kanagawawave_v2"
SOURCE_ARTIFACTS = frozenset(
    {
        "forum_train.json",
        "forum_test.json",
        "manifest.json",
        "human_review.json",
        "inference_prompt.txt",
    }
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid dataset artifact: %s" % path.name) from error


def _validate_source_dataset(dataset_dir: Path) -> None:
    if dataset_dir.is_symlink() or not dataset_dir.is_dir():
        raise ValueError("dataset directory is unavailable")
    for filename in SOURCE_ARTIFACTS:
        path = dataset_dir / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing dataset artifact: %s" % filename)
    manifest = _read_json(dataset_dir / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("version") != 2:
        raise ValueError("dataset is not a v2 dataset")
    if manifest.get("counts") != EXPECTED_COUNTS:
        raise ValueError("dataset must contain exactly 10000 train, 1000 test, 100 review")
    hashes = manifest.get("file_sha256")
    expected_names = SOURCE_ARTIFACTS - {"manifest.json"}
    if not isinstance(hashes, dict) or set(hashes) != expected_names:
        raise ValueError("dataset manifest hash inventory is invalid")
    for filename in sorted(expected_names):
        if hashes[filename] != _sha256(dataset_dir / filename):
            raise ValueError("dataset hash mismatch: %s" % filename)
    if (
        (dataset_dir / "inference_prompt.txt").read_bytes()
        != KANAGAWAWAVE_V2_INSTRUCTION.encode("utf-8")
    ):
        raise ValueError("dataset prompt is not the exact v2 prompt")


def _copy_private(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o600)


def _stage_bundle(dataset_dir: Path, stage: Path) -> None:
    mappings = {
        dataset_dir / "forum_train.json": stage / "data" / "forum_train.json",
        dataset_dir / "forum_test.json": stage / "data" / "forum_test.json",
        dataset_dir / "manifest.json": stage / "manifest.json",
        dataset_dir / "human_review.json": stage / "human_review.json",
        TEMPLATE_ROOT / "data" / "dataset_info.json": stage / "data" / "dataset_info.json",
        dataset_dir / "inference_prompt.txt": stage / "INFERENCE_PROMPT.txt",
        TEMPLATE_ROOT / "README_zh.md": stage / "README_zh.md",
        PROJECT_ROOT / "scripts" / "validate_kanagawawave_v2_bundle.py": (
            stage / "scripts" / "validate_kanagawawave_v2_bundle.py"
        ),
    }
    for name in (
        "train_4060_8gb.yaml",
        "train_4060_low_memory.yaml",
        "chat.yaml",
        "merge.yaml",
    ):
        mappings[TEMPLATE_ROOT / "configs" / name] = stage / "configs" / name
    for source, destination in mappings.items():
        if source.is_symlink() or not source.is_file():
            raise ValueError("training template is incomplete")
        _copy_private(source, destination)
    hashes = {
        relative: _sha256(stage / relative) for relative in sorted(EXPECTED_HASH_PATHS)
    }
    lock = {
        "version": 1,
        "profile_key": KANAGAWAWAVE_V2_PROFILE_KEY,
        "counts": dict(EXPECTED_COUNTS),
        "sha256": hashes,
    }
    (stage / "DATASET_LOCK.json").write_bytes(_json_bytes(lock))
    (stage / "DATASET_LOCK.json").chmod(0o600)


def _write_deterministic_zip(root: Path, output_zip: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + output_zip.name + ".",
        suffix=".tmp",
        dir=str(output_zip.parent),
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary_path, "w") as archive:
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100600 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(
                    info,
                    path.read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        os.replace(temporary_path, output_zip)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def build_training_bundle(
    dataset_dir: Path,
    output_zip: Path,
) -> Mapping[str, object]:
    """Build and validate a deterministic portable KanagawaWave v2 ZIP."""

    dataset_dir = Path(dataset_dir)
    output_zip = Path(output_zip)
    if output_zip.exists():
        raise ValueError("output ZIP already exists")
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    _validate_source_dataset(dataset_dir)
    with tempfile.TemporaryDirectory(
        prefix="kanagawawave-v2-package-", dir=str(output_zip.parent)
    ) as temporary:
        stage = Path(temporary)
        _stage_bundle(dataset_dir, stage)
        summary = validate_training_bundle(stage)
        _write_deterministic_zip(stage, output_zip)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--output-zip", type=Path, required=True)
    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        summary = build_training_bundle(arguments.dataset_dir, arguments.output_zip)
    except ValueError as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
