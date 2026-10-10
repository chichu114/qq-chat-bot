#!/usr/bin/env python3
"""Build privacy-neutral KanagawaWave LLaMA-Factory datasets."""

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from qq_openclaw_bridge.dataset.kanagawawave import (
    BuildConfig,
    DatasetBuildError,
    OllamaNeutralizer,
    build_dataset,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a privacy-neutral KanagawaWave forum-style dataset."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--database", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--train-size", type=int, default=2000)
    parser.add_argument("--test-size", type=int, default=200)
    parser.add_argument("--split-seed", default="kanagawave-v1")
    parser.add_argument("--max-per-author", type=int, default=20)
    parser.add_argument("--max-short-ratio", type=float, default=0.15)
    parser.add_argument("--neutralization-attempts", type=int, default=3)
    parser.add_argument("--ollama-base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ollama-model", default="qwen3:8b")
    parser.add_argument("--ollama-timeout", type=float, default=120.0)
    return parser


def parse_args(argv=None):
    return build_parser().parse_args(argv)


def main(argv=None) -> int:
    arguments = parse_args(argv)
    try:
        config = BuildConfig(
            database=arguments.database,
            archive=arguments.archive,
            output_dir=arguments.output_dir,
            train_size=arguments.train_size,
            test_size=arguments.test_size,
            split_seed=arguments.split_seed,
            max_per_author=arguments.max_per_author,
            max_short_ratio=arguments.max_short_ratio,
            neutralization_attempts=arguments.neutralization_attempts,
        )
        neutralizer = OllamaNeutralizer(
            base_url=arguments.ollama_base_url,
            model=arguments.ollama_model,
            timeout=arguments.ollama_timeout,
        )
        build_dataset(config, neutralizer)
    except (DatasetBuildError, ValueError) as error:
        print("dataset build failed: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
