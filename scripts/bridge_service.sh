#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.chichu.qq-bridge"
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LAUNCHCTL_BIN="${CHICHU_LAUNCHCTL_BIN:-/bin/launchctl}"
PGREP_BIN="${CHICHU_PGREP_BIN:-/usr/bin/pgrep}"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
INSTALLER="$SCRIPT_DIR/install_launch_agents.py"
ROUTING_BOOTSTRAP="$SCRIPT_DIR/bootstrap_routing_config.py"

usage() {
  cat <<'EOF'
Usage: scripts/bridge_service.sh <start|stop|status|restart>

Manage the QQ OpenClaw bridge through the current user's LaunchAgent.
EOF
}

ensure_plist() {
  if [[ -f "$PLIST" ]]; then
    return 0
  fi
  if [[ ! -x "$PYTHON_BIN" ]]; then
    printf '[ERROR] Project Python is missing: %s\n' "$PYTHON_BIN" >&2
    return 1
  fi
  "$PYTHON_BIN" "$INSTALLER" --project-dir "$PROJECT_DIR" --home-dir "$HOME" >/dev/null
}

is_loaded() {
  "$LAUNCHCTL_BIN" print "$TARGET" >/dev/null 2>&1
}

managed_pid() {
  "$LAUNCHCTL_BIN" print "$TARGET" 2>/dev/null \
    | /usr/bin/awk '$1 == "pid" && $2 == "=" { print $3; exit }'
}

legacy_process_running() {
  local owner_pid="$1"
  local pids=""
  local status=0
  pids="$("$PGREP_BIN" -f "^$PYTHON_BIN -m qq_openclaw_bridge( |$)" 2>/dev/null)" || status=$?
  if [[ "$status" == "1" ]]; then
    return 1
  fi
  if [[ "$status" != "0" ]]; then
    return 2
  fi
  local pid
  for pid in $pids; do
    if [[ -z "$owner_pid" || "$pid" != "$owner_pid" ]]; then
      return 0
    fi
  done
  return 1
}

bootstrap_routing_config() {
  if [[ ! -f "$PROJECT_DIR/.env" || ! -x "$PYTHON_BIN" ]]; then
    return 0
  fi
  "$PYTHON_BIN" "$ROUTING_BOOTSTRAP" --project-dir "$PROJECT_DIR"
}

start_bridge() {
  local loaded=0
  local owner_pid=""
  if is_loaded; then
    loaded=1
    owner_pid="$(managed_pid || true)"
  fi
  local legacy_status=0
  legacy_process_running "$owner_pid" || legacy_status=$?
  if [[ "$legacy_status" == "0" ]]; then
    printf '[ERROR] A legacy unmanaged Bridge process is running; stop it before migration.\n' >&2
    return 1
  elif [[ "$legacy_status" != "1" ]]; then
    printf '[ERROR] Unable to inspect existing Bridge processes; refusing to start.\n' >&2
    return 1
  fi
  bootstrap_routing_config
  printf '[INFO] Running readiness checks\n'
  /bin/bash "$SCRIPT_DIR/check_ready.sh"
  ensure_plist
  if [[ "$loaded" == "0" ]]; then
    "$LAUNCHCTL_BIN" bootstrap "$DOMAIN" "$PLIST"
  fi
  "$LAUNCHCTL_BIN" kickstart -k "$TARGET"
  printf '[INFO] Bridge LaunchAgent started: %s\n' "$LABEL"
}

stop_bridge() {
  if ! is_loaded; then
    printf '[INFO] Bridge LaunchAgent is not loaded.\n'
    return 0
  fi
  "$LAUNCHCTL_BIN" bootout "$TARGET"
  printf '[INFO] Bridge LaunchAgent stopped.\n'
}

status_bridge() {
  if ! "$LAUNCHCTL_BIN" print "$TARGET"; then
    printf '[INFO] Bridge LaunchAgent is not loaded.\n'
    return 1
  fi
}

case "${1:-}" in
  start) start_bridge ;;
  stop) stop_bridge ;;
  status) status_bridge ;;
  restart) stop_bridge; start_bridge ;;
  -h|--help|"") usage ;;
  *) printf '[ERROR] Unknown command: %s\n' "$1" >&2; usage; exit 1 ;;
esac
