#!/usr/bin/env python3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from qq_openclaw_bridge.persona import install_persona_core


def main() -> None:
    workspace = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path("~/.openclaw/workspace").expanduser()
    install_persona_core(workspace)
    print("Updated:", workspace / "SOUL.md")
    print("Updated:", workspace / "STYLE.md")


if __name__ == "__main__":
    main()
