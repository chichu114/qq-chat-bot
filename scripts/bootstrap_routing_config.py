#!/usr/bin/env python3
"""Initialize the local shadow-routing config without calling a model."""

import argparse
import os
import re
import stat
from pathlib import Path
from typing import Dict, Optional, Sequence

from qq_openclaw_bridge.config import Config
from qq_openclaw_bridge.runtime.routing import bootstrap_shadow_routing_config
from qq_openclaw_bridge.storage import WorkspaceStateStore


_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_ENV_BYTES = 64 * 1024
_MAX_ENV_LINE = 8 * 1024


def _parse_env(content: str) -> Dict[str, str]:
    values = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        if len(raw_line) > _MAX_ENV_LINE:
            raise ValueError(".env line %d is too long" % line_number)
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(".env line %d is invalid" % line_number)
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if _ENV_KEY.fullmatch(key) is None:
            raise ValueError(".env line %d has an invalid key" % line_number)
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(".env line %d has unmatched quotes" % line_number)
            value = value[1:-1]
        values[key] = value
    return values


def load_project_env(project_dir: Path) -> Dict[str, str]:
    path = Path(project_dir) / ".env"
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise RuntimeError("secure .env open flags are unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(str(path), flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(".env must be a regular file")
        if metadata.st_uid != os.getuid():
            raise ValueError(".env must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError(".env permissions must be 0600")
        if metadata.st_size > _MAX_ENV_BYTES:
            raise ValueError(".env is too large")
        with os.fdopen(descriptor, "rb", closefd=False) as env_file:
            payload = env_file.read(_MAX_ENV_BYTES + 1)
        if len(payload) > _MAX_ENV_BYTES:
            raise ValueError(".env is too large")
        return _parse_env(payload.decode("utf-8"))
    finally:
        os.close(descriptor)


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="bootstrap shadow-routing config")
    parser.add_argument(
        "--project-dir",
        default=str(Path(__file__).resolve().parents[1]),
    )
    args = parser.parse_args(argv)
    env = load_project_env(Path(args.project_dir))
    config = Config.from_env(env)
    if not config.enable_shadow_routing:
        return
    state_store = WorkspaceStateStore(Path(config.state_database_path))
    active = bootstrap_shadow_routing_config(state_store)
    print("[OK] routing config bootstrap version:", active["version"])


if __name__ == "__main__":
    main()
