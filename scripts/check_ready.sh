#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

usage() {
  cat <<'EOF'
Usage: scripts/check_ready.sh [--smoke-openclaw] [--smoke-orchestration] [--smoke-media] [--probe-routing]

Check whether the QQ OpenClaw bridge is ready to start.

Options:
  --smoke-openclaw  Run a real OpenClaw one-shot test. This may call the API.
  --smoke-orchestration
                     Run one paid Direct probe; requires ORCHESTRATION_MODE=direct.
  --smoke-media     Send one tiny image to the configured media API.
  --probe-routing   Run one routing-only classification probe. This may call the API.
  -h, --help        Show this help.
EOF
}

ok() {
  printf '[OK] %s\n' "$1"
}

warn() {
  printf '[WARN] %s\n' "$1" >&2
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

normalize_shadow_routing() {
  local value
  value="$(printf '%s' "${ENABLE_SHADOW_ROUTING:-false}" | /usr/bin/tr '[:upper:]' '[:lower:]')"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  case "$value" in
    1|true|yes|on)
      printf 'true\n'
      ;;
    0|false|no|off|'')
      printf 'false\n'
      ;;
    *)
      fail "ENABLE_SHADOW_ROUTING must be true or false"
      ;;
  esac
}

normalize_group_agent() {
  local value
  value="$(printf '%s' "${ENABLE_GROUP_AGENT:-false}" | /usr/bin/tr '[:upper:]' '[:lower:]')"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  case "$value" in
    1|true|yes|on)
      printf 'true\n'
      ;;
    0|false|no|off|'')
      printf 'false\n'
      ;;
    *)
      fail "ENABLE_GROUP_AGENT must be true or false"
      ;;
  esac
}

normalize_routing_classifier_transport() {
  local value
  value="${ROUTING_CLASSIFIER_TRANSPORT-openclaw}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  case "$value" in
    openclaw|responses_api)
      printf '%s\n' "$value"
      ;;
    *)
      fail "ROUTING_CLASSIFIER_TRANSPORT must be openclaw or responses_api"
      ;;
  esac
}

SMOKE_OPENCLAW=0
SMOKE_ORCHESTRATION=0
SMOKE_MEDIA=0
PROBE_ROUTING=0
for arg in "$@"; do
  case "$arg" in
    --smoke-openclaw)
      SMOKE_OPENCLAW=1
      ;;
    --smoke-media)
      SMOKE_MEDIA=1
      ;;
    --smoke-orchestration)
      SMOKE_ORCHESTRATION=1
      ;;
    --probe-routing)
      PROBE_ROUTING=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $arg"
      ;;
  esac
done
if [[ "$SMOKE_OPENCLAW" == "1" && "$SMOKE_ORCHESTRATION" == "1" ]]; then
  fail "--smoke-openclaw and --smoke-orchestration are mutually exclusive"
fi
if [[ "$PROBE_ROUTING" == "1" && ( "$SMOKE_OPENCLAW" == "1" || "$SMOKE_ORCHESTRATION" == "1" ) ]]; then
  fail "--probe-routing is mutually exclusive with OpenClaw smoke probes"
fi
export SMOKE_MEDIA

cd "$PROJECT_DIR"

[[ -f "$ENV_FILE" ]] || fail ".env was not found. Copy .env.example to .env first."
chmod 600 "$ENV_FILE" 2>/dev/null || true
ok ".env found"

[[ -x "$PYTHON_BIN" ]] || fail ".venv was not found. Run: python3 -m venv .venv && source .venv/bin/activate && python3 -m pip install -r requirements.txt && python3 -m pip install -e ."
ok "Python virtualenv found"

set -a
source "$ENV_FILE"
set +a
SHADOW_ROUTING_ENABLED="$(normalize_shadow_routing)"
GROUP_AGENT_ENABLED="$(normalize_group_agent)"
ROUTING_CLASSIFIER_TRANSPORT="$(normalize_routing_classifier_transport)"
export ROUTING_CLASSIFIER_TRANSPORT

"$PYTHON_BIN" - <<'PY'
import base64
import json
import os
import socket
import sqlite3
import stat
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from qq_openclaw_bridge.config import Config
from qq_openclaw_bridge.persistence.routing import validate_routing_config


config = Config.from_env()
print("[OK] bridge config loaded")
print("[INFO] orchestration mode:", config.orchestration_mode)
print("[INFO] OneBot:", config.onebot_ws_url)
print("[INFO] adapter:", config.agent_adapter)
if config.agent_adapter == "openclaw-cli":
    print("[INFO] OpenClaw command:", config.openclaw_command)
if config.enable_style_memory:
    print("[INFO] style memory:", config.style_memory_path)
else:
    print("[INFO] style memory: disabled")
if config.enable_housekeeper:
    print("[INFO] housekeeper memory:", config.housekeeper_memory_path)
    print("[INFO] housekeeper todo:", config.housekeeper_todo_path)
else:
    print("[INFO] housekeeper: disabled")
if config.enable_reminder_push:
    print("[INFO] reminder push: every %ss" % config.reminder_check_interval_seconds)
    print("[INFO] reminder state:", config.reminder_state_path)
    if config.enable_daily_briefing:
        print("[INFO] daily briefing:", config.daily_briefing_time)
    else:
        print("[INFO] daily briefing: disabled")
    if config.enable_evening_review:
        print("[INFO] evening review:", config.evening_review_time)
    else:
        print("[INFO] evening review: disabled")
    print(
        "[INFO] quiet hours: %s-%s"
        % (config.quiet_hours_start, config.quiet_hours_end)
    )
else:
    print("[INFO] reminder push: disabled")

print(
    "[INFO] media pipeline:",
    "enabled" if config.enable_media_pipeline else "disabled",
)
print(
    "[INFO] long tasks:",
    "enabled" if config.enable_long_tasks else "disabled",
)

media_secret = None
if config.enable_media_pipeline:
    from qq_openclaw_bridge.runtime.secrets import SecretResolver

    media_secret = SecretResolver().resolve(
        config.media_api_secret_ref,
        os.environ.get("MEDIA_API_KEY"),
    )
    print("[OK] media API credential resolves")
    print("[INFO] media API base URL: configured")
    print("[INFO] media image model:", config.media_image_model)
    print("[INFO] media audio model:", config.media_audio_model)
    print("[INFO] media daily budget:", config.media_api_daily_budget)

    cache = Path(config.media_cache_dir).expanduser()
    if cache.exists():
        if not cache.is_dir() or cache.is_symlink():
            raise SystemExit("[ERROR] media cache must be a real directory")
        mode = stat.S_IMODE(cache.stat().st_mode)
        if mode & 0o077:
            raise SystemExit("[ERROR] media cache permissions must be 0700")
        print("[OK] media cache permissions: 0700")
    else:
        print("[WARN] media cache does not exist yet; runtime will create it as 0700")

database = Path(config.state_database_path).expanduser()
orchestration_version = 0
orchestration_active_mode = None
orchestration_backlog = 0
if config.enable_sqlite_store and database.is_file():
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        outbox = 0
        if "outbox" in tables:
            outbox = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE status IN ('pending','sending','failed')"
            ).fetchone()[0]
        task_queue = 0
        if "long_tasks" in tables:
            task_queue = connection.execute(
                "SELECT COUNT(*) FROM long_tasks WHERE status IN ('queued','running','pending')"
            ).fetchone()[0]
        if "orchestration_config_versions" in tables:
            active = connection.execute(
                "SELECT version, config_json FROM orchestration_config_versions "
                "WHERE status='active' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if active is not None:
                orchestration_version = int(active[0])
                active_payload = json.loads(str(active[1]))
                orchestration_active_mode = active_payload.get("mode")
        if "orchestration_runs" in tables:
            orchestration_backlog = connection.execute(
                "SELECT COUNT(*) FROM orchestration_runs "
                "WHERE status IN ('received','planned','executing','verifying','accepted')"
            ).fetchone()[0]
    print("[INFO] outbox backlog:", outbox)
    print("[INFO] task queue backlog:", task_queue)
else:
    print("[INFO] outbox backlog: unavailable before SQLite initialization")
    print("[INFO] task queue backlog: unavailable before SQLite initialization")

print("[INFO] orchestration config version:", orchestration_version)
print(
    "[INFO] orchestration active mode:",
    orchestration_active_mode or "unconfigured",
)
if (
    orchestration_active_mode is not None
    and orchestration_active_mode != config.orchestration_mode
):
    raise SystemExit(
        "[ERROR] active orchestration mode does not match ORCHESTRATION_MODE"
    )
print("[INFO] orchestration active backlog:", orchestration_backlog)

if not config.enable_shadow_routing:
    print("[SKIP] shadow routing: disabled")
else:
    if not config.enable_sqlite_store or not database.is_file():
        raise SystemExit("[ERROR] routing active config is unavailable")
    if "routing_config_versions" not in tables:
        raise SystemExit("[ERROR] routing active config is unavailable")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        active_routing = connection.execute(
            "SELECT version, config_json, config_sha256 FROM routing_config_versions "
            "WHERE status='active' ORDER BY version DESC"
        ).fetchall()
    if len(active_routing) != 1:
        raise SystemExit("[ERROR] routing active config must exist exactly once")
    try:
        routing_config = json.loads(str(active_routing[0][1]))
        validate_routing_config(routing_config, active_routing[0][2])
    except (TypeError, ValueError, json.JSONDecodeError):
        raise SystemExit("[ERROR] routing active config is invalid")
    if routing_config["enabled"] is not True:
        raise SystemExit("[ERROR] routing active config is invalid")
    print("[OK] routing active config version:", int(active_routing[0][0]))
    if routing_config["classifier_enabled"] is not True:
        print("[SKIP] routing Agent: pure-rule mode")
    elif config.routing_classifier_transport == "responses_api":
        from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter

        curl_path = Path("/usr/bin/curl")
        if not curl_path.is_file() or not os.access(curl_path, os.X_OK):
            raise SystemExit("[ERROR] direct routing transport requires /usr/bin/curl")
        try:
            OpenAIResponsesAgentAdapter(
                model=routing_config["classifier_model"],
                timeout_seconds=routing_config["classifier_timeout_seconds"],
                routing_agent_id=config.routing_agent_id,
            )._read_api_key()
        except (KeyError, RuntimeError, ValueError):
            raise SystemExit("[ERROR] direct routing auth is unavailable or unsafe")
        print("[OK] direct routing curl is executable")
        print("[OK] direct routing auth is readable and safe")
    else:
        import sys

        sys.path.insert(0, str(Path.cwd() / "scripts"))
        from install_routing_agent import ROUTING_AGENT_DENY

        config_path = Path(
            os.environ.get("OPENCLAW_CONFIG_PATH", "~/.openclaw/openclaw.json")
        ).expanduser()
        try:
            openclaw_payload = json.loads(config_path.read_text(encoding="utf-8"))
            openclaw_agents = openclaw_payload["agents"]
            openclaw_agent_list = openclaw_agents["list"]
            model_allowlist = openclaw_agents["defaults"]["models"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise SystemExit("[ERROR] routing readiness could not read OpenClaw agent metadata")
        if not isinstance(openclaw_agent_list, list) or not isinstance(model_allowlist, dict):
            raise SystemExit("[ERROR] routing readiness found invalid OpenClaw agent metadata")
        matches = [
            item for item in openclaw_agent_list
            if isinstance(item, dict) and item.get("id") == config.routing_agent_id
        ]
        if len(matches) != 1:
            raise SystemExit("[ERROR] routing Agent must exist exactly once")
        routing_agent = matches[0]
        routing_tools = routing_agent.get("tools")
        expected_denies = set(ROUTING_AGENT_DENY)
        if (
            routing_agent.get("skills") != []
            or not isinstance(routing_tools, dict)
            or routing_tools.get("profile") != "minimal"
            or routing_tools.get("allow") != []
            or routing_tools.get("alsoAllow") != []
            or routing_tools.get("elevated") != {"enabled": False}
            or not expected_denies.issubset(set(routing_tools.get("deny", [])))
        ):
            raise SystemExit("[ERROR] routing Agent restricted tool profile is invalid")
        routing_model = routing_config.get("classifier_model")
        if (
            not isinstance(routing_model, str)
            or routing_model not in model_allowlist
            or routing_agent.get("model") != routing_model
        ):
            raise SystemExit("[ERROR] routing model is not allowlisted for the active config")
        print("[OK] routing Agent exists:", config.routing_agent_id)
        print("[OK] routing model is allowlisted")

parsed = urlparse(config.onebot_ws_url)
host = parsed.hostname
port = parsed.port
if host and port:
    sock = socket.socket()
    sock.settimeout(1.0)
    try:
        sock.connect((host, port))
    except OSError:
        print(
            "[WARN] OneBot/NapCat is not reachable right now. "
            "Start QQ/NapCat first if you want immediate replies."
        )
    else:
        print("[OK] OneBot/NapCat port is reachable")
    finally:
        sock.close()
else:
    print("[WARN] Could not parse OneBot host/port from ONEBOT_WS_URL")

if os.environ.get("SMOKE_MEDIA") == "1":
    if not config.enable_media_pipeline:
        raise SystemExit("[ERROR] --smoke-media requires ENABLE_MEDIA_PIPELINE=true")
    if config.media_api_daily_budget <= 0:
        raise SystemExit("[ERROR] --smoke-media requires a positive media daily budget")

    from qq_openclaw_bridge.application.media_budget import MediaBudgetPolicy
    from qq_openclaw_bridge.runtime.media_api import MediaApiClient

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "/w8AAusB9Y9Z4m8AAAAASUVORK5CYII="
    )
    with tempfile.TemporaryDirectory(prefix="chichu-media-smoke-") as temp_dir:
        image_path = Path(temp_dir) / "probe.png"
        image_path.write_bytes(png)
        client = MediaApiClient(
            base_url=config.media_api_base_url,
            api_key=media_secret,
            image_model=config.media_image_model,
            audio_model=config.media_audio_model,
            timeout_seconds=config.media_api_timeout_seconds,
            requests_per_minute=config.media_api_requests_per_minute,
            max_concurrent_calls=1,
            budget_policy=MediaBudgetPolicy(config.media_api_daily_budget),
            estimated_cost=Decimal("0.000001"),
        )
        result = client.describe_image(
            SimpleNamespace(path=image_path, mime="image/png"),
            "这是连通性测试图片。只需简短描述看到的内容。",
        )
        if not result.text.strip():
            raise SystemExit("[ERROR] media API semantic probe returned empty text")
    print("[OK] media API semantic probe passed")
PY

if [[ "$GROUP_AGENT_ENABLED" == "true" ]]; then
  "$PYTHON_BIN" "$SCRIPT_DIR/check_style_agent.py" --project-dir "$PROJECT_DIR"
else
  printf '[SKIP] community style agent: disabled\n'
fi

launchd_status="healthy"
for label in com.chichu.qq-bridge com.chichu.qq-dashboard com.chichu.qq-watchdog; do
  launchd_detail="$(/bin/launchctl print "gui/$(id -u)/$label" 2>/dev/null || true)"
  if ! printf '%s\n' "$launchd_detail" \
    | /usr/bin/grep -Eq '^[[:space:]]*state = running[[:space:]]*$'; then
    launchd_status="unhealthy"
  fi
done
printf '[INFO] launchd bridge/dashboard/watchdog: %s\n' "$launchd_status"

if /usr/bin/security find-generic-password \
  -s qq-openclaw-backup -a backup-master-v1 >/dev/null 2>&1; then
  printf '[INFO] backup encryption: configured\n'
else
  printf '[INFO] backup encryption: unconfigured\n'
fi

if [[ "$SMOKE_ORCHESTRATION" == "1" ]]; then
  [[ "${ORCHESTRATION_MODE:-legacy_direct}" == "direct" ]] \
    || fail "--smoke-orchestration requires ORCHESTRATION_MODE=direct"
fi

if [[ "$SHADOW_ROUTING_ENABLED" == "true" && "${AGENT_ADAPTER:-echo}" != "openclaw-cli" ]]; then
  fail "shadow routing requires AGENT_ADAPTER=openclaw-cli"
fi

if [[ "$PROBE_ROUTING" == "1" && "$SHADOW_ROUTING_ENABLED" != "true" ]]; then
  fail "--probe-routing requires ENABLE_SHADOW_ROUTING=true"
fi

if [[ "$SMOKE_OPENCLAW" == "1" || "$SMOKE_ORCHESTRATION" == "1" ]]; then
  [[ "${AGENT_ADAPTER:-echo}" == "openclaw-cli" ]] \
    || fail "OpenClaw smoke probes require AGENT_ADAPTER=openclaw-cli"
fi

if [[ "${AGENT_ADAPTER:-echo}" == "openclaw-cli" ]]; then
  openclaw_command="${OPENCLAW_COMMAND:-scripts/openclaw.sh}"
  if [[ "$openclaw_command" != /* ]]; then
    openclaw_command="$PROJECT_DIR/$openclaw_command"
  fi

  [[ -x "$openclaw_command" ]] || fail "OpenClaw command is not executable: $openclaw_command"
  "$openclaw_command" --version >/dev/null
  ok "OpenClaw command works"

  if [[ -n "${OPENCLAW_AGENT_ID:-}" ]]; then
    if ! "$openclaw_command" agents list --json | "$PYTHON_BIN" -c '
import json
import os
import sys

agent_id = os.environ["OPENCLAW_AGENT_ID"]
agents = json.load(sys.stdin)
if not any(item.get("id") == agent_id for item in agents):
    raise SystemExit(1)
'; then
      fail "OpenClaw agent does not exist: ${OPENCLAW_AGENT_ID}"
    fi
    ok "OpenClaw agent exists: ${OPENCLAW_AGENT_ID}"
  fi

  if [[ "${OPENCLAW_USE_GATEWAY:-true}" == "true" ]]; then
    if ! "$openclaw_command" gateway health >/dev/null; then
      fail "OpenClaw Gateway is not healthy. Run: $openclaw_command gateway start"
    fi
    ok "OpenClaw Gateway is healthy"
  fi

  if [[ -z "${HTTPS_PROXY:-}${HTTP_PROXY:-}${ALL_PROXY:-}${https_proxy:-}${http_proxy:-}${all_proxy:-}" ]]; then
    warn "No proxy variable found. OpenAI API calls may time out in your network."
  else
    ok "proxy variables found"
  fi

  if [[ "$SMOKE_OPENCLAW" == "1" ]]; then
    smoke_command=(
      "$openclaw_command" agent --json
      --message "只回复两个字：你好"
      --session-key "${OPENCLAW_SESSION_PREFIX:-agent:main:qq}-ready-check-$$"
      --timeout 90
    )
    if [[ "${OPENCLAW_USE_GATEWAY:-true}" != "true" ]]; then
      smoke_command+=("--local")
    fi
    if [[ -n "${OPENCLAW_AGENT_ID:-}" ]]; then
      smoke_command+=("--agent" "$OPENCLAW_AGENT_ID")
    fi
    smoke_stderr="$(/usr/bin/mktemp -t chichu-openclaw-smoke)"
    if ! "${smoke_command[@]}" >/dev/null 2>"$smoke_stderr"; then
      cat "$smoke_stderr" >&2
      rm -f "$smoke_stderr"
      fail "OpenClaw smoke test failed"
    fi
    if grep -q "EMBEDDED FALLBACK" "$smoke_stderr"; then
      cat "$smoke_stderr" >&2
      rm -f "$smoke_stderr"
      fail "OpenClaw smoke test fell back to embedded mode"
    fi
    rm -f "$smoke_stderr"
    ok "OpenClaw smoke test passed"
  fi

  if [[ "$PROBE_ROUTING" == "1" ]]; then
    if [[ "${ROUTING_CLASSIFIER_TRANSPORT:-openclaw}" == "responses_api" ]]; then
      if ! "$PYTHON_BIN" "$SCRIPT_DIR/probe_routing_classifier.py"; then
        fail "routing probe failed"
      fi
      ok "routing probe passed"
    else
      routing_probe_directory="$(/usr/bin/mktemp -d -t chichu-routing-probe)"
      routing_probe_stdout="$routing_probe_directory/stdout"
      routing_probe_stderr="$routing_probe_directory/stderr"
      routing_probe_cleanup() {
        local cleanup_status=0
        /bin/rm -f "$routing_probe_stdout" "$routing_probe_stderr" || cleanup_status=1
        /bin/rmdir "$routing_probe_directory" || cleanup_status=1
        return "$cleanup_status"
      }
      trap 'routing_probe_cleanup' EXIT
      trap 'exit 130' INT
      trap 'exit 143' TERM
      routing_probe_prompt="$("$PYTHON_BIN" - <<'PY'
from qq_openclaw_bridge.application.shadow_routing import build_routing_classifier_prompt

print(build_routing_classifier_prompt("你好，这是一次无害的问候。"))
PY
)"
      routing_probe_command=(
        "$openclaw_command" agent --json
        --agent "${ROUTING_AGENT_ID:-routing}"
        --message "$routing_probe_prompt"
        --session-key "agent:${ROUTING_AGENT_ID:-routing}:routing-probe-$(date +%s)-$$"
        --timeout 30
      )
      if [[ "${OPENCLAW_USE_GATEWAY:-true}" != "true" ]]; then
        routing_probe_command+=("--local")
      fi
      if ! "${routing_probe_command[@]}" >"$routing_probe_stdout" 2>"$routing_probe_stderr"; then
        fail "routing probe failed"
      fi
      if grep -q "EMBEDDED FALLBACK" "$routing_probe_stderr"; then
        fail "routing probe fell back to embedded mode"
      fi
      if ! "$PYTHON_BIN" - "$routing_probe_stdout" <<'PY'
import sys

from qq_openclaw_bridge.agent import extract_openclaw_response_from_stdout
from qq_openclaw_bridge.domain.routing import parse_classifier_response

try:
    stdout = open(sys.argv[1], encoding="utf-8").read()
    response = extract_openclaw_response_from_stdout(stdout, require_json=True)
    parse_classifier_response(response.text)
except (OSError, RuntimeError, ValueError):
    raise SystemExit(1)
PY
      then
        fail "routing probe did not return strict JSON"
      fi
      if ! routing_probe_cleanup; then
        fail "routing probe cleanup failed"
      fi
      trap - EXIT INT TERM
      ok "routing probe passed"
    fi
  fi
fi

if [[ "$SMOKE_ORCHESTRATION" == "1" ]]; then
  "$PYTHON_BIN" -m qq_openclaw_bridge.runtime.readiness
fi

ok "ready"
