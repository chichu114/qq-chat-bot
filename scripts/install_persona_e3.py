#!/usr/bin/env python3
import argparse
import sys
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from qq_openclaw_bridge.persona import install_e3_persona_core


def main() -> int:
    parser = argparse.ArgumentParser(description="压缩并安装 chichu E3 人格核心")
    parser.add_argument(
        "--workspace",
        default="~/.openclaw/workspace",
        help="OpenClaw 工作区路径",
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="人格文件备份目录；留空时写入项目 backups/persona-e3-*",
    )
    args = parser.parse_args()
    workspace = Path(args.workspace).expanduser()
    backup = (
        Path(args.backup_dir).expanduser()
        if args.backup_dir
        else PROJECT_DIR
        / "backups"
        / ("persona-e3-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    )
    install_e3_persona_core(workspace, backup_dir=backup)
    print("E3 人格核心已安装：%s" % workspace)
    print("原文件备份：%s" % backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

