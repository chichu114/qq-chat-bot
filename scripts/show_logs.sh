#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/bridge.log"

usage() {
  cat <<'EOF'
Usage: scripts/show_logs.sh [--summary] [--open]

Show or open QQ OpenClaw bridge logs.

Options:
  --summary  Print the latest useful log lines.
  --open     Open logs/bridge.log with the default macOS app.
  -h, --help Show this help.
EOF
}

ensure_log_file() {
  mkdir -p "$LOG_DIR"
  if [[ ! -f "$LOG_FILE" ]]; then
    printf '日志还没生成。先启动一次 QQ 智能体，别急，锅还没开始热。\n' > "$LOG_FILE"
  fi
}

show_summary() {
  ensure_log_file
  printf '== QQ 智能体最近日志 ==\n'
  printf '文件：%s\n\n' "$LOG_FILE"
  tail -n 80 "$LOG_FILE"
}

open_log() {
  ensure_log_file
  open "$LOG_FILE"
}

mode="summary"
for arg in "$@"; do
  case "$arg" in
    --summary)
      mode="summary"
      ;;
    --open)
      mode="open"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '[ERROR] Unknown option: %s\n' "$arg" >&2
      usage
      exit 1
      ;;
  esac
done

case "$mode" in
  summary)
    show_summary
    ;;
  open)
    open_log
    ;;
esac
