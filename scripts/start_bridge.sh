#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
CHECK_SCRIPT="$SCRIPT_DIR/check_ready.sh"

chmod 600 "$ENV_FILE" 2>/dev/null || true

usage() {
  cat <<'EOF'
Usage: scripts/start_bridge.sh [--skip-check] [--check-only]

Start the QQ OpenClaw private-message bridge.

Options:
  --skip-check  Start without running scripts/check_ready.sh first.
  --check-only  Run readiness checks and exit.
  -h, --help    Show this help.
EOF
}

SKIP_CHECK=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --skip-check)
      SKIP_CHECK=1
      ;;
    --check-only)
      CHECK_ONLY=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '[ERROR] Unknown option: %s\n' "$arg" >&2
      exit 1
      ;;
  esac
done

cd "$PROJECT_DIR"

if [[ "$SKIP_CHECK" != "1" ]]; then
  /bin/bash "$CHECK_SCRIPT"
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  exit 0
fi

set -a
source "$ENV_FILE"
set +a

printf '[INFO] Starting QQ OpenClaw bridge\n'
printf '[INFO] OneBot: %s\n' "${ONEBOT_WS_URL:-}"
printf '[INFO] Adapter: %s\n' "${AGENT_ADAPTER:-echo}"
printf '[INFO] Stop with Ctrl+C\n'

exec "$PYTHON_BIN" -m qq_openclaw_bridge
