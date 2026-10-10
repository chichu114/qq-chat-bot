import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..agent import create_agent_adapter
from ..application.coordinator_factory import create_direct_runtime
from ..config import Config
from ..models import AgentRequest, AgentResponse
from ..orchestration.models import TaskContract
from ..storage import create_workspace_state_store
from .composition import resolve_orchestration_config_version


def routing_health_from_connection(connection, now: datetime) -> bool:
    """Evaluate persisted F1 health without contacting the routing model."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("routing health requires a timezone-aware timestamp")
    try:
        active = connection.execute(
            "SELECT version, config_json FROM routing_config_versions "
            "WHERE status='active'"
        ).fetchall()
        if len(active) != 1:
            return False
        version, raw_config = active[0]
        config = json.loads(str(raw_config))
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return False
        timeout = config.get("classifier_timeout_seconds")
        if type(timeout) is not int or timeout < 1:
            return False
        cutoff = now.astimezone(timezone.utc) - timedelta(seconds=timeout + 60)
        pending = connection.execute(
            "SELECT created_at FROM route_decisions WHERE status='pending' "
            "AND routing_config_version=?",
            (int(version),),
        ).fetchall()
        for (created_at,) in pending:
            if _routing_timestamp(str(created_at)) < cutoff:
                return False
        if config.get("classifier_enabled") is not True:
            return True
        cooldown = config.get("cooldown_seconds")
        if type(cooldown) is not int or cooldown < 1:
            return False
        circuit = connection.execute(
            "SELECT state, opened_at FROM routing_classifier_circuit "
            "WHERE config_version=?",
            (int(version),),
        ).fetchall()
        if len(circuit) != 1:
            return False
        state, opened_at = circuit[0]
        if state == "closed":
            return True
        if state != "cooling" or opened_at is None:
            return False
        opened = _routing_timestamp(str(opened_at))
        elapsed = (now.astimezone(timezone.utc) - opened).total_seconds()
        # A watchdog cannot write the transition. Once cooling has elapsed,
        # it reports the circuit's effective closed state until the observer
        # can persist its compare-and-swap transition on a later observation.
        return elapsed >= 0
    except (json.JSONDecodeError, sqlite3.Error, ValueError, TypeError):
        return False


def _routing_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("routing timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


async def run_direct_probe(
    config: Config,
    adapter_factory: Callable[[Config], object] = create_agent_adapter,
    state_store_factory: Callable = create_workspace_state_store,
    runtime_factory: Callable = create_direct_runtime,
) -> str:
    """Run exactly one model call through the F0 Direct Runtime."""

    if config.orchestration_mode != "direct":
        raise ValueError("Direct readiness probe requires orchestration mode direct")
    if config.agent_adapter != "openclaw-cli":
        raise ValueError("Direct readiness probe requires openclaw-cli adapter")

    state_store = state_store_factory(config)
    orchestration_repository = getattr(
        state_store, "orchestration_repository", None
    )
    config_version = resolve_orchestration_config_version(
        orchestration_repository, config.orchestration_mode
    )
    adapter = adapter_factory(config)
    call_count = 0

    async def reply_call(request: AgentRequest) -> AgentResponse:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise RuntimeError("Direct readiness probe exceeded one model call")
        return await asyncio.to_thread(adapter.reply, request)

    runtime = runtime_factory(reply_call, config_version=config_version)
    user_id = min(config.allowed_qq_users) if config.allowed_qq_users else 1
    nonce = time.time_ns()
    request = AgentRequest(
        user_id=user_id,
        message_id="ready-%s" % nonce,
        text="只回复两个字：你好",
        timestamp=time.time(),
        session_key="%s-ready-check-%s"
        % (config.openclaw_session_prefix, nonce),
    )
    result = await runtime.execute(TaskContract.from_request(request))
    if call_count != 1:
        raise RuntimeError("Direct readiness probe did not make exactly one model call")
    return result.text


def main() -> int:
    try:
        result = asyncio.run(run_direct_probe(Config.from_env()))
        if not result.strip():
            raise RuntimeError("Direct readiness probe returned an empty reply")
    except Exception as error:
        print(
            "[ERROR] Direct orchestration probe failed: %s"
            % error.__class__.__name__,
            file=sys.stderr,
        )
        return 1
    print("[OK] Direct orchestration probe passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
