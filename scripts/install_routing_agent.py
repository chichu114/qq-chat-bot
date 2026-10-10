#!/usr/bin/env python3
"""Provision the isolated, tool-free OpenClaw routing agent."""

import argparse
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path


MAX_MODEL_IDENTIFIER_LENGTH = 128
MIN_BOOTSTRAP_CHARS = 1
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")

# This is the single installer/readiness source for the routing agent's deny policy.
ROUTING_AGENT_DENY = [
    "group:runtime",
    "group:fs",
    "group:web",
    "group:memory",
    "group:sessions",
    "group:ui",
    "group:media",
    "exec",
    "process",
    "bash",
    "code",
    "code_execution",
    "write",
    "edit",
    "apply_patch",
    "read",
    "find",
    "ls",
    "glob",
    "grep",
    "file_fetch",
    "directory_list",
    "browser",
    "web_search",
    "web_fetch",
    "x_search",
    "memory",
    "memory_get",
    "memory_search",
    "sessions",
    "sessions_list",
    "sessions_history",
    "sessions_send",
    "sessions_spawn",
    "sessions_yield",
    "session_status",
    "subagents",
    "message",
    "gateway",
    "elevated",
    "canvas",
    "image",
    "image_generate",
    "pdf",
    "tts",
    "music_generate",
    "video_generate",
    "nodes",
    "cron",
    "agents_list",
    "search",
    "tool_search",
    "tool_describe",
    "tool_call",
    "create_goal",
    "get_goal",
    "update_goal",
    "update_plan",
    "heartbeat_respond",
    "skill_workshop",
]


def _validate_identifier(value: str, label: str, pattern: re.Pattern) -> str:
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > MAX_MODEL_IDENTIFIER_LENGTH
        or pattern.fullmatch(candidate) is None
    ):
        raise ValueError("%s must be a bounded identifier" % label)
    return candidate


def _routing_agent(agent_id: str, model: str, workspace: Path) -> dict:
    return {
        "id": agent_id,
        "model": model,
        "workspace": str(workspace),
        "contextInjection": "never",
        "thinkingDefault": "off",
        "reasoningDefault": "off",
        "fastModeDefault": True,
        # OpenClaw's schema requires positive bootstrap limits even when injection is off.
        "bootstrapMaxChars": MIN_BOOTSTRAP_CHARS,
        "bootstrapTotalMaxChars": MIN_BOOTSTRAP_CHARS,
        "skills": [],
        "tools": {
            "profile": "minimal",
            "allow": [],
            "alsoAllow": [],
            "deny": list(ROUTING_AGENT_DENY),
            "elevated": {"enabled": False},
        },
    }


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(handle) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _chmod_private(descriptor: int) -> None:
    os.fchmod(descriptor, 0o600)


def _write_bytes(handle, contents: bytes) -> None:
    handle.write(contents)


def _stage_private_file(directory: Path, contents: bytes, prefix: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(dir=str(directory), prefix=prefix)
    temporary = Path(temporary_name)
    handle = None
    try:
        handle = os.fdopen(descriptor, "wb")
        descriptor = None
        _chmod_private(handle.fileno())
        _write_bytes(handle, contents)
        _fsync_file(handle)
        handle.close()
        handle = None
        return temporary
    except BaseException:
        if handle is not None:
            handle.close()
        elif descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _prepare_backup_directory(backup_root: Path) -> None:
    try:
        details = backup_root.lstat()
    except FileNotFoundError:
        backup_root.mkdir(mode=0o700)
        _fsync_directory(backup_root.parent)
        _fsync_directory(backup_root)
        return

    if stat.S_ISLNK(details.st_mode):
        raise ValueError("backup directory must not be a symlink")
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("backup path must be a directory")
    if details.st_uid != os.getuid():
        raise ValueError("backup directory must be owned by the current user")
    os.chmod(str(backup_root), 0o700)
    _fsync_directory(backup_root)
    _fsync_directory(backup_root.parent)


def _restore_original_config(path: Path, original_stage: Path) -> None:
    if original_stage is not None and original_stage.exists():
        os.replace(str(original_stage), str(path))
        try:
            _fsync_directory(path.parent)
        except OSError:
            pass


def install_routing_agent(
    config_path: Path,
    model: str,
    routing_agent_id: str = "routing",
    backup_dir: Path = None,
) -> Path:
    """Create or replace one routing agent without exposing configuration data."""
    path = Path(config_path).expanduser()
    agent_id = _validate_identifier(routing_agent_id, "routing agent ID", _AGENT_ID)
    selected_model = _validate_identifier(model, "model", _MODEL)
    original = path.read_bytes()
    payload = json.loads(original.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("OpenClaw configuration must be an object")

    agents = payload.get("agents")
    if not isinstance(agents, dict):
        raise ValueError("OpenClaw configuration is missing agents")
    agent_list = agents.get("list")
    if not isinstance(agent_list, list):
        raise ValueError("OpenClaw configuration is missing agents.list")
    matches = [
        item for item in agent_list
        if isinstance(item, dict) and item.get("id") == agent_id
    ]
    if len(matches) > 1:
        raise ValueError("duplicate routing agent ID: %s" % agent_id)

    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("agents.defaults must be an object")
    models = defaults.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError("agents.defaults.models must be an object")
    models.setdefault(selected_model, {})
    routing_workspace = path.parent / "workspace" / agent_id
    restricted = _routing_agent(agent_id, selected_model, routing_workspace)
    if matches:
        agent_list[agent_list.index(matches[0])] = restricted
    else:
        agent_list.append(restricted)
    rendered = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    backup_root = (
        Path(backup_dir).expanduser()
        if backup_dir is not None
        else path.parent / "routing-agent-config-backups"
    )
    _prepare_backup_directory(backup_root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_root / ("openclaw-before-routing-agent-%s.json" % stamp)

    original_stage = None
    config_stage = None
    backup_stage = None
    config_replaced = False
    backup_replaced = False
    try:
        original_stage = _stage_private_file(path.parent, original, ".routing-agent-original-")
        config_stage = _stage_private_file(path.parent, rendered, ".routing-agent-config-")
        backup_stage = _stage_private_file(backup_root, original, ".routing-agent-backup-")

        os.replace(str(config_stage), str(path))
        config_stage = None
        config_replaced = True
        _fsync_directory(path.parent)

        os.replace(str(backup_stage), str(backup))
        backup_stage = None
        backup_replaced = True
        _fsync_directory(backup_root)

        _fsync_directory(path.parent)
        _remove_if_present(original_stage)
        original_stage = None
        return backup
    except BaseException:
        if config_replaced:
            try:
                _restore_original_config(path, original_stage)
                original_stage = None
            except OSError:
                pass
        if backup_replaced:
            try:
                _remove_if_present(backup)
                _fsync_directory(backup_root)
            except OSError:
                pass
        raise
    finally:
        for temporary in (config_stage, backup_stage, original_stage):
            if temporary is not None:
                try:
                    _remove_if_present(temporary)
                except OSError:
                    pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision a dedicated OpenClaw routing agent with no tools"
    )
    parser.add_argument(
        "--config", default="~/.openclaw/openclaw.json", help="OpenClaw config path"
    )
    parser.add_argument(
        "--model", default="openai/gpt-5.4-nano", help="routing model identifier"
    )
    parser.add_argument(
        "--agent",
        default=os.environ.get("ROUTING_AGENT_ID", "routing"),
        help="routing agent ID",
    )
    parser.add_argument("--backup-dir", default="", help="backup directory")
    args = parser.parse_args(argv)
    backup = install_routing_agent(
        Path(args.config),
        args.model,
        routing_agent_id=args.agent,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
    )
    print("Routing agent provisioned: %s" % args.agent)
    print("Routing model allowlisted: %s" % args.model)
    print("Configuration backup: %s" % backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
