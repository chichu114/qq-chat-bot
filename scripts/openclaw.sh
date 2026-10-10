#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NODE_BIN_DIR="${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin"
PROJECT_BIN_DIR="$PROJECT_DIR/node_modules/.bin"
NODE_BIN="$NODE_BIN_DIR/node"
OPENCLAW_ENTRY="$PROJECT_DIR/node_modules/openclaw/openclaw.mjs"

if [[ ! -x "$NODE_BIN" || ! -f "$OPENCLAW_ENTRY" ]]; then
  echo "ERROR: OpenClaw is not installed in this project."
  echo "Run: pnpm add openclaw@latest"
  exit 1
fi

export PATH="$PROJECT_BIN_DIR:$NODE_BIN_DIR:$PATH"
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-$PROJECT_DIR/.npm-cache}"
export npm_config_cache="${npm_config_cache:-$NPM_CONFIG_CACHE}"
exec "$NODE_BIN" "$OPENCLAW_ENTRY" "$@"
