#!/usr/bin/env python3
"""Run one direct Responses API routing classifier readiness probe."""

import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Callable, Mapping, Optional

from qq_openclaw_bridge.agent import OpenAIResponsesAgentAdapter
from qq_openclaw_bridge.application.shadow_routing import (
    build_routing_classifier_prompt,
)
from qq_openclaw_bridge.config import Config
from qq_openclaw_bridge.domain.routing import parse_classifier_response
from qq_openclaw_bridge.models import AgentRequest
from qq_openclaw_bridge.persistence.routing import validate_routing_config


_PROBE_MESSAGE = "你好，这是一次无害的问候。"


def _load_active_routing_config(database_path: str) -> dict:
    database = Path(database_path).expanduser()
    try:
        with sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True
        ) as connection:
            rows = connection.execute(
                "SELECT config_json, config_sha256 FROM routing_config_versions "
                "WHERE status='active' ORDER BY version DESC"
            ).fetchall()
        if len(rows) != 1:
            raise RuntimeError("routing active config is unavailable")
        routing_config = json.loads(str(rows[0][0]))
        validate_routing_config(routing_config, rows[0][1])
    except RuntimeError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
        raise RuntimeError("routing active config is invalid") from None

    if (
        routing_config.get("enabled") is not True
        or routing_config.get("classifier_enabled") is not True
    ):
        raise RuntimeError("routing classifier is not enabled")
    return routing_config


def probe_routing_classifier(
    env: Optional[Mapping[str, str]] = None,
    adapter_factory: Callable = OpenAIResponsesAgentAdapter,
):
    config = Config.from_env(env)
    if config.routing_classifier_transport != "responses_api":
        raise RuntimeError("direct routing classifier transport is not enabled")
    if not config.enable_shadow_routing or not config.enable_sqlite_store:
        raise RuntimeError("shadow routing is not enabled")

    routing_config = _load_active_routing_config(config.state_database_path)
    adapter = adapter_factory(
        model=routing_config["classifier_model"],
        timeout_seconds=routing_config["classifier_timeout_seconds"],
        routing_agent_id=config.routing_agent_id,
    )
    request = AgentRequest(
        user_id=0,
        message_id="routing-readiness-probe",
        text=build_routing_classifier_prompt(_PROBE_MESSAGE),
        timestamp=time.time(),
        session_key="agent:%s:routing-probe" % config.routing_agent_id,
    )
    response = adapter.reply(request)
    return parse_classifier_response(response.text)


def main(
    env: Optional[Mapping[str, str]] = None,
    adapter_factory: Callable = OpenAIResponsesAgentAdapter,
) -> int:
    try:
        probe_routing_classifier(env=env, adapter_factory=adapter_factory)
    except Exception:
        print("[ERROR] routing classifier probe failed", file=sys.stderr)
        return 1
    print("[OK] routing classifier probe passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
