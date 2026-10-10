#!/usr/bin/env python3
import argparse
import copy
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


def allow_memory_model(
    config_path: Path,
    model: str,
    restricted_agent_id: str = "qq",
    backup_dir: Path = None,
) -> Path:
    path = Path(config_path).expanduser()
    payload = json.loads(path.read_text(encoding="utf-8"))
    agents = payload.setdefault("agents", {})
    agent_list = agents.get("list")
    if not isinstance(agent_list, list):
        raise ValueError("OpenClaw 配置缺少 agents.list")
    restricted = next(
        (item for item in agent_list if item.get("id") == restricted_agent_id), None
    )
    if restricted is None:
        raise ValueError("找不到受限 Agent：%s" % restricted_agent_id)
    tools_before = copy.deepcopy(restricted.get("tools"))
    sandbox_before = copy.deepcopy(restricted.get("sandbox"))

    defaults = agents.setdefault("defaults", {})
    models = defaults.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError("agents.defaults.models 必须是对象")
    models.setdefault(model, {})

    restricted_after = next(
        item for item in agent_list if item.get("id") == restricted_agent_id
    )
    if (
        restricted_after.get("tools") != tools_before
        or restricted_after.get("sandbox") != sandbox_before
    ):
        raise RuntimeError("拒绝写入：受限 Agent 的工具或沙箱配置发生变化")

    backup_root = (
        Path(backup_dir).expanduser()
        if backup_dir is not None
        else path.parent / "e3-config-backups"
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_root / ("openclaw-before-memory-model-%s.json" % stamp)
    shutil.copy2(path, backup)

    mode = path.stat().st_mode & 0o777
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=".openclaw-e3-",
        delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只为受限 QQ Agent 增加 E3 轻量模型白名单"
    )
    parser.add_argument(
        "--config", default="~/.openclaw/openclaw.json", help="OpenClaw 配置路径"
    )
    parser.add_argument(
        "--model", default="openai/gpt-5.4-nano", help="轻量模型 ID"
    )
    parser.add_argument("--agent", default="qq", help="需要保持受限的 Agent ID")
    parser.add_argument("--backup-dir", default="", help="额外配置备份目录")
    args = parser.parse_args()
    backup = allow_memory_model(
        Path(args.config),
        args.model,
        restricted_agent_id=args.agent,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
    )
    print("已允许 E3 轻量模型：%s" % args.model)
    print("受限 Agent 权限未变：%s" % args.agent)
    print("配置备份：%s" % backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

