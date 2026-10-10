#!/usr/bin/env python3
"""Build the reject-only KanagawaWave v2 LLaMA-Factory dataset."""

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from qq_openclaw_bridge.dataset.kanagawawave import DatasetBuildError
from qq_openclaw_bridge.dataset.kanagawawave_v2 import (
    V2BuildConfig,
    build_v2_dataset,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--database", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--train-size", type=int, default=10_000)
    parser.add_argument("--test-size", type=int, default=1_000)
    parser.add_argument("--review-size", type=int, default=100)
    parser.add_argument("--split-seed", default="kanagawave-v2")
    parser.add_argument("--max-per-author-train", type=int, default=50)
    parser.add_argument("--max-per-author-test", type=int, default=10)
    parser.add_argument("--max-short-ratio", type=float, default=0.15)
    return parser


def parse_args(argv=None):
    return build_parser().parse_args(argv)


def main(argv=None) -> int:
    arguments = parse_args(argv)
    config = V2BuildConfig(
        database=arguments.database,
        archive=arguments.archive,
        output_dir=arguments.output_dir,
        train_size=arguments.train_size,
        test_size=arguments.test_size,
        review_size=arguments.review_size,
        split_seed=arguments.split_seed,
        max_per_author_train=arguments.max_per_author_train,
        max_per_author_test=arguments.max_per_author_test,
        max_short_ratio=arguments.max_short_ratio,
    )
    try:
        manifest = build_v2_dataset(config)
    except (DatasetBuildError, ValueError):
        print(
            "dataset build failed: input database is unavailable or invalid"
            if not (config.database and config.database.is_file()) and config.archive is None
            else "dataset build failed: dataset could not be completed safely",
            file=sys.stderr,
        )
        return 1
    summary = {
        "status": "ready",
        "counts": manifest["counts"],
        "audit": {
            "thread_overlap_count": manifest["audit"]["thread_overlap_count"],
            "duplicate_example_count": manifest["audit"]["duplicate_example_count"],
        },
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
