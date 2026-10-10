import os
import ipaddress
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional, Set, Tuple


def _parse_allowed_users(raw: str) -> Set[int]:
    users: Set[int] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            users.add(int(value))
        except ValueError as exc:
            raise ValueError("ALLOWED_QQ_USERS must contain only numeric QQ IDs") from exc
    return users


def _parse_csv_values(raw: str) -> Tuple[str, ...]:
    return tuple(
        value for value in (item.strip() for item in raw.split(",")) if value
    )


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ROUTING_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _normalize_exact_host(value: str) -> str:
    try:
        host = value.encode("idna").decode("ascii").rstrip(".").casefold()
    except (UnicodeError, AttributeError) as exc:
        raise ValueError("invalid hostname") from exc
    labels = host.split(".")
    if (
        not host
        or len(host.encode("ascii")) > 253
        or any(
            not label
            or len(label.encode("ascii")) > 63
            or _DNS_LABEL.fullmatch(label) is None
            for label in labels
        )
    ):
        raise ValueError("invalid hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("IP literals are not supported")


def _parse_remote_hosts(raw: str) -> Tuple[str, ...]:
    normalized = []
    seen = set()
    for value in _parse_csv_values(raw):
        try:
            host = _normalize_exact_host(value)
        except ValueError as exc:
            raise ValueError(
                "MEDIA_REMOTE_ALLOWED_HOSTS must contain exact DNS hostnames"
            ) from exc
        if host not in seen:
            normalized.append(host)
            seen.add(host)
    return tuple(normalized)


def _get_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError("%s must be an integer" % name) from exc


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default

    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError("%s must be true or false" % name)


def _get_routing_agent_id(env: Mapping[str, str]) -> str:
    value = env.get("ROUTING_AGENT_ID", "routing").strip()
    if _ROUTING_AGENT_ID.fullmatch(value) is None:
        raise ValueError("ROUTING_AGENT_ID must be a bounded agent ID")
    return value


def _get_routing_classifier_transport(env: Mapping[str, str]) -> str:
    value = env.get("ROUTING_CLASSIFIER_TRANSPORT", "openclaw").strip()
    if value not in ("openclaw", "responses_api"):
        raise ValueError(
            "ROUTING_CLASSIFIER_TRANSPORT must be openclaw or responses_api"
        )
    return value


def _get_positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _get_int(env, name, default)
    if value < 1:
        raise ValueError("%s must be greater than 0" % name)
    return value


def _get_log_level(source: Mapping[str, str]) -> str:
    value = source.get("LOG_LEVEL", "info").strip().lower() or "info"
    if value not in ("debug", "info", "warning", "error", "critical"):
        raise ValueError(
            "LOG_LEVEL must be debug, info, warning, error, or critical"
        )
    return value


def _get_f0_orchestration_mode(source: Mapping[str, str]) -> str:
    value = source.get("ORCHESTRATION_MODE", "legacy_direct").strip()
    if value not in ("legacy_direct", "direct"):
        raise ValueError("ORCHESTRATION_MODE must be legacy_direct or direct in F0")
    return value


def _get_nonnegative_decimal(
    env: Mapping[str, str], name: str, default: Decimal
) -> Decimal:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("%s must be a non-negative number" % name) from exc
    if not value.is_finite() or value < 0:
        raise ValueError("%s must be a non-negative number" % name)
    return value


def _get_percent(env: Mapping[str, str], name: str, default: int) -> float:
    value = _get_positive_int(env, name, default)
    if value > 100:
        raise ValueError("%s must be between 1 and 100" % name)
    return value / 100.0


def _default_style_memory_path() -> str:
    return os.path.expanduser("~/.openclaw/workspace/STYLE.md")


def _default_housekeeper_memory_path() -> str:
    return os.path.expanduser("~/.openclaw/workspace/MEMORY.md")


def _default_housekeeper_todo_path() -> str:
    return os.path.expanduser("~/.openclaw/workspace/TODO.md")


def _default_reminder_state_path() -> str:
    return os.path.expanduser("~/.openclaw/workspace/REMINDER_STATE.md")


def _default_auto_backup_dir() -> str:
    return os.path.abspath("backups")


def _default_state_database_path() -> str:
    return os.path.expanduser("~/.openclaw/workspace/chichu.sqlite3")


@dataclass(frozen=True)
class Config:
    onebot_ws_url: str
    allowed_qq_users: Set[int]
    bot_qq: Optional[int] = None
    agent_adapter: str = "echo"
    openclaw_endpoint: Optional[str] = None
    openclaw_command: str = "scripts/openclaw.sh"
    openclaw_agent_id: Optional[str] = "main"
    enable_shadow_routing: bool = False
    enable_group_agent: bool = False
    style_agent_config_path: str = "config/community-style-agent.json"
    kanagawave_private_users: Set[int] = field(default_factory=set)
    enable_group_interjections: bool = False
    group_interjection_cooldown_seconds: int = 10
    enable_group_homophone_filter: bool = False
    group_homophone_map_path: str = "config/group-homophones.json"
    routing_agent_id: str = "routing"
    routing_classifier_transport: str = "openclaw"
    openclaw_model: Optional[str] = None
    openclaw_timeout_seconds: int = 120
    openclaw_session_prefix: str = "agent:main:qq"
    openclaw_use_gateway: bool = True
    openclaw_gateway_url: str = "ws://127.0.0.1:18789"
    max_concurrent_agent_calls: int = 2
    max_pending_events: int = 50
    agent_retry_attempts: int = 2
    agent_circuit_failure_threshold: int = 3
    agent_circuit_cooldown_seconds: int = 60
    outbox_retry_base_seconds: int = 2
    outbox_max_attempts: int = 8
    enable_style_memory: bool = True
    style_memory_path: str = field(default_factory=_default_style_memory_path)
    enable_housekeeper: bool = True
    housekeeper_memory_path: str = field(default_factory=_default_housekeeper_memory_path)
    housekeeper_todo_path: str = field(default_factory=_default_housekeeper_todo_path)
    enable_reminder_push: bool = True
    reminder_check_interval_seconds: int = 30
    reminder_state_path: str = field(default_factory=_default_reminder_state_path)
    enable_daily_briefing: bool = True
    daily_briefing_time: str = "09:00"
    enable_evening_review: bool = True
    evening_review_time: str = "23:00"
    quiet_hours_start: str = "00:00"
    quiet_hours_end: str = "08:30"
    enable_security_confirmation: bool = True
    confirmation_timeout_seconds: int = 120
    enable_auto_backup: bool = True
    auto_backup_interval_hours: int = 24
    auto_backup_retention_days: int = 30
    auto_backup_max_files: int = 30
    auto_backup_dir: str = field(default_factory=_default_auto_backup_dir)
    enable_sqlite_store: bool = True
    state_database_path: str = field(default_factory=_default_state_database_path)
    enable_memory2: bool = True
    memory_light_model: Optional[str] = "openai/gpt-5.4-nano"
    memory_light_timeout_seconds: int = 180
    memory_light_session_prefix: str = "agent:main:memory"
    memory_summary_trigger_turns: int = 8
    memory_summary_batch_turns: int = 24
    memory_context_max_chars: int = 2400
    memory_fact_limit: int = 6
    memory_summary_limit: int = 3
    memory_candidate_min_confidence: float = 0.75
    enable_media_pipeline: bool = False
    enable_long_tasks: bool = False
    media_cache_dir: str = "~/.openclaw/workspace/media-cache"
    napcat_media_roots: Tuple[str, ...] = ()
    media_remote_allowed_hosts: Tuple[str, ...] = ()
    media_cache_ttl_seconds: int = 3600
    media_api_timeout_seconds: int = 90
    media_api_requests_per_minute: int = 10
    media_api_daily_budget: Decimal = Decimal("0")
    media_max_concurrent_calls: int = 2
    long_task_ack_seconds: int = 15
    long_task_progress_interval_seconds: int = 60
    media_api_base_url: Optional[str] = None
    media_api_secret_ref: Optional[str] = None
    media_image_model: Optional[str] = None
    media_audio_model: Optional[str] = None
    rate_limit_messages_per_minute: int = 10
    max_reply_chars: int = 1800
    fallback_reply: str = "智能体暂时不可用，请稍后再试。"
    log_level: str = "info"
    orchestration_mode: str = "legacy_direct"
    log_max_bytes: int = 10485760
    log_backup_count: int = 5

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        source = os.environ if env is None else env
        onebot_ws_url = source.get("ONEBOT_WS_URL", "").strip()
        if not onebot_ws_url:
            raise ValueError("ONEBOT_WS_URL is required")
        allowed_qq_users = _parse_allowed_users(
            source.get("ALLOWED_QQ_USERS", "")
        )

        bot_qq_raw = source.get("BOT_QQ", "").strip()
        bot_qq = int(bot_qq_raw) if bot_qq_raw else None
        openclaw_agent_id = source.get("OPENCLAW_AGENT_ID", "main").strip() or None
        enable_sqlite_store = _get_bool(source, "ENABLE_SQLITE_STORE", True)
        enable_shadow_routing = _get_bool(source, "ENABLE_SHADOW_ROUTING", False)
        enable_group_agent = _get_bool(source, "ENABLE_GROUP_AGENT", False)
        kanagawave_private_users = _parse_allowed_users(
            source.get("KANAGAWAVE_PRIVATE_USERS", "")
        )
        if kanagawave_private_users and not enable_group_agent:
            raise ValueError(
                "KANAGAWAVE_PRIVATE_USERS requires ENABLE_GROUP_AGENT=true"
            )
        if not kanagawave_private_users.issubset(allowed_qq_users):
            raise ValueError(
                "KANAGAWAVE_PRIVATE_USERS must be included in ALLOWED_QQ_USERS"
            )
        enable_group_interjections = _get_bool(
            source, "ENABLE_GROUP_INTERJECTIONS", False
        )
        group_interjection_cooldown_seconds = _get_positive_int(
            source, "GROUP_INTERJECTION_COOLDOWN_SECONDS", 10
        )
        enable_group_homophone_filter = _get_bool(
            source, "ENABLE_GROUP_HOMOPHONE_FILTER", False
        )
        group_homophone_map_path = source.get(
            "GROUP_HOMOPHONE_MAP_PATH", "config/group-homophones.json"
        ).strip()
        if enable_group_homophone_filter and not enable_group_agent:
            raise ValueError(
                "ENABLE_GROUP_HOMOPHONE_FILTER requires ENABLE_GROUP_AGENT=true"
            )
        if enable_group_homophone_filter and not group_homophone_map_path:
            raise ValueError(
                "GROUP_HOMOPHONE_MAP_PATH is required when group filtering is enabled"
            )
        style_agent_config_path = source.get(
            "STYLE_AGENT_CONFIG_PATH", "config/community-style-agent.json"
        ).strip()
        if enable_shadow_routing and not enable_sqlite_store:
            raise ValueError("shadow routing requires a SQLite state store")
        if enable_group_agent:
            if bot_qq is None:
                raise ValueError("group agent requires BOT_QQ")
            if not enable_sqlite_store:
                raise ValueError("group agent requires a SQLite state store")
            if not style_agent_config_path:
                raise ValueError("group agent requires STYLE_AGENT_CONFIG_PATH")
        default_memory_session_prefix = "agent:%s:memory" % (
            openclaw_agent_id or "main"
        )

        media_enabled = _get_bool(source, "ENABLE_MEDIA_PIPELINE", False)
        media_base_url = source.get("MEDIA_API_BASE_URL", "").strip() or None
        media_secret_ref = source.get("MEDIA_API_SECRET_REF", "").strip() or None
        media_api_key = source.get("MEDIA_API_KEY", "").strip() or None
        media_image_model = source.get("MEDIA_IMAGE_MODEL", "").strip() or None
        media_audio_model = source.get("MEDIA_AUDIO_MODEL", "").strip() or None
        napcat_media_roots = _parse_csv_values(source.get("NAPCAT_MEDIA_ROOTS", ""))
        media_remote_allowed_hosts = _parse_remote_hosts(
            source.get("MEDIA_REMOTE_ALLOWED_HOSTS", "")
        )
        if media_enabled:
            required = {
                "MEDIA_API_BASE_URL": media_base_url,
                "MEDIA_API_SECRET_REF or MEDIA_API_KEY": media_secret_ref or media_api_key,
                "MEDIA_IMAGE_MODEL": media_image_model,
                "MEDIA_AUDIO_MODEL": media_audio_model,
            }
            for name, value in required.items():
                if not value:
                    raise ValueError("%s is required when ENABLE_MEDIA_PIPELINE=true" % name)
            if not (napcat_media_roots or media_remote_allowed_hosts):
                raise ValueError(
                    "NAPCAT_MEDIA_ROOTS or MEDIA_REMOTE_ALLOWED_HOSTS is required "
                    "when ENABLE_MEDIA_PIPELINE=true"
                )

        return cls(
            onebot_ws_url=onebot_ws_url,
            allowed_qq_users=allowed_qq_users,
            bot_qq=bot_qq,
            agent_adapter=source.get("AGENT_ADAPTER", "echo").strip().lower() or "echo",
            openclaw_endpoint=source.get("OPENCLAW_ENDPOINT", "").strip() or None,
            openclaw_command=source.get("OPENCLAW_COMMAND", "scripts/openclaw.sh").strip()
            or "scripts/openclaw.sh",
            openclaw_agent_id=openclaw_agent_id,
            enable_shadow_routing=enable_shadow_routing,
            enable_group_agent=enable_group_agent,
            style_agent_config_path=style_agent_config_path
            or "config/community-style-agent.json",
            kanagawave_private_users=kanagawave_private_users,
            enable_group_interjections=enable_group_interjections,
            group_interjection_cooldown_seconds=(
                group_interjection_cooldown_seconds
            ),
            enable_group_homophone_filter=enable_group_homophone_filter,
            group_homophone_map_path=(
                group_homophone_map_path or "config/group-homophones.json"
            ),
            routing_agent_id=_get_routing_agent_id(source),
            routing_classifier_transport=_get_routing_classifier_transport(source),
            openclaw_model=source.get("OPENCLAW_MODEL", "").strip() or None,
            openclaw_timeout_seconds=_get_positive_int(
                source, "OPENCLAW_TIMEOUT_SECONDS", 120
            ),
            openclaw_session_prefix=source.get(
                "OPENCLAW_SESSION_PREFIX", "agent:main:qq"
            ).strip()
            or "agent:main:qq",
            openclaw_use_gateway=_get_bool(
                source, "OPENCLAW_USE_GATEWAY", True
            ),
            openclaw_gateway_url=source.get(
                "OPENCLAW_GATEWAY_PROBE_URL", "ws://127.0.0.1:18789"
            ).strip()
            or "ws://127.0.0.1:18789",
            max_concurrent_agent_calls=_get_positive_int(
                source, "MAX_CONCURRENT_AGENT_CALLS", 2
            ),
            max_pending_events=_get_positive_int(
                source, "MAX_PENDING_EVENTS", 50
            ),
            agent_retry_attempts=_get_positive_int(
                source, "AGENT_RETRY_ATTEMPTS", 2
            ),
            agent_circuit_failure_threshold=_get_positive_int(
                source, "AGENT_CIRCUIT_FAILURE_THRESHOLD", 3
            ),
            agent_circuit_cooldown_seconds=_get_positive_int(
                source, "AGENT_CIRCUIT_COOLDOWN_SECONDS", 60
            ),
            outbox_retry_base_seconds=_get_positive_int(
                source, "OUTBOX_RETRY_BASE_SECONDS", 2
            ),
            outbox_max_attempts=_get_positive_int(
                source, "OUTBOX_MAX_ATTEMPTS", 8
            ),
            enable_style_memory=_get_bool(source, "ENABLE_STYLE_MEMORY", True),
            style_memory_path=source.get(
                "STYLE_MEMORY_PATH", _default_style_memory_path()
            ).strip()
            or _default_style_memory_path(),
            enable_housekeeper=_get_bool(source, "ENABLE_HOUSEKEEPER", True),
            housekeeper_memory_path=source.get(
                "HOUSEKEEPER_MEMORY_PATH", _default_housekeeper_memory_path()
            ).strip()
            or _default_housekeeper_memory_path(),
            housekeeper_todo_path=source.get(
                "HOUSEKEEPER_TODO_PATH", _default_housekeeper_todo_path()
            ).strip()
            or _default_housekeeper_todo_path(),
            enable_reminder_push=_get_bool(source, "ENABLE_REMINDER_PUSH", True),
            reminder_check_interval_seconds=_get_positive_int(
                source, "REMINDER_CHECK_INTERVAL_SECONDS", 30
            ),
            reminder_state_path=source.get(
                "REMINDER_STATE_PATH", _default_reminder_state_path()
            ).strip()
            or _default_reminder_state_path(),
            enable_daily_briefing=_get_bool(source, "ENABLE_DAILY_BRIEFING", True),
            daily_briefing_time=source.get(
                "DAILY_BRIEFING_TIME", "09:00"
            ).strip()
            or "09:00",
            enable_evening_review=_get_bool(source, "ENABLE_EVENING_REVIEW", True),
            evening_review_time=source.get(
                "EVENING_REVIEW_TIME", "23:00"
            ).strip()
            or "23:00",
            quiet_hours_start=source.get("QUIET_HOURS_START", "00:00").strip()
            or "00:00",
            quiet_hours_end=source.get("QUIET_HOURS_END", "08:30").strip()
            or "08:30",
            enable_security_confirmation=_get_bool(
                source, "ENABLE_SECURITY_CONFIRMATION", True
            ),
            confirmation_timeout_seconds=_get_positive_int(
                source, "CONFIRMATION_TIMEOUT_SECONDS", 120
            ),
            enable_auto_backup=_get_bool(source, "ENABLE_AUTO_BACKUP", True),
            auto_backup_interval_hours=_get_positive_int(
                source, "AUTO_BACKUP_INTERVAL_HOURS", 24
            ),
            auto_backup_retention_days=_get_positive_int(
                source, "AUTO_BACKUP_RETENTION_DAYS", 30
            ),
            auto_backup_max_files=_get_positive_int(
                source, "AUTO_BACKUP_MAX_FILES", 30
            ),
            auto_backup_dir=source.get(
                "AUTO_BACKUP_DIR", _default_auto_backup_dir()
            ).strip()
            or _default_auto_backup_dir(),
            enable_sqlite_store=enable_sqlite_store,
            state_database_path=source.get(
                "STATE_DATABASE_PATH", _default_state_database_path()
            ).strip()
            or _default_state_database_path(),
            enable_memory2=_get_bool(source, "ENABLE_MEMORY_2", True),
            memory_light_model=source.get(
                "MEMORY_LIGHT_MODEL", "openai/gpt-5.4-nano"
            ).strip()
            or None,
            memory_light_timeout_seconds=_get_positive_int(
                source, "MEMORY_LIGHT_TIMEOUT_SECONDS", 180
            ),
            memory_light_session_prefix=source.get(
                "MEMORY_LIGHT_SESSION_PREFIX", default_memory_session_prefix
            ).strip()
            or default_memory_session_prefix,
            memory_summary_trigger_turns=_get_positive_int(
                source, "MEMORY_SUMMARY_TRIGGER_TURNS", 8
            ),
            memory_summary_batch_turns=_get_positive_int(
                source, "MEMORY_SUMMARY_BATCH_TURNS", 24
            ),
            memory_context_max_chars=_get_positive_int(
                source, "MEMORY_CONTEXT_MAX_CHARS", 2400
            ),
            memory_fact_limit=_get_positive_int(
                source, "MEMORY_FACT_LIMIT", 6
            ),
            memory_summary_limit=_get_positive_int(
                source, "MEMORY_SUMMARY_LIMIT", 3
            ),
            memory_candidate_min_confidence=_get_percent(
                source, "MEMORY_CANDIDATE_MIN_CONFIDENCE_PERCENT", 75
            ),
            enable_media_pipeline=media_enabled,
            enable_long_tasks=_get_bool(source, "ENABLE_LONG_TASKS", False),
            media_cache_dir=source.get(
                "MEDIA_CACHE_DIR", "~/.openclaw/workspace/media-cache"
            ).strip()
            or "~/.openclaw/workspace/media-cache",
            napcat_media_roots=napcat_media_roots,
            media_remote_allowed_hosts=media_remote_allowed_hosts,
            media_cache_ttl_seconds=_get_positive_int(
                source, "MEDIA_CACHE_TTL_SECONDS", 3600
            ),
            media_api_timeout_seconds=_get_positive_int(
                source, "MEDIA_API_TIMEOUT_SECONDS", 90
            ),
            media_api_requests_per_minute=_get_positive_int(
                source, "MEDIA_API_REQUESTS_PER_MINUTE", 10
            ),
            media_api_daily_budget=_get_nonnegative_decimal(
                source, "MEDIA_API_DAILY_BUDGET", Decimal("0")
            ),
            media_max_concurrent_calls=_get_positive_int(
                source, "MEDIA_MAX_CONCURRENT_CALLS", 2
            ),
            long_task_ack_seconds=_get_positive_int(source, "LONG_TASK_ACK_SECONDS", 15),
            long_task_progress_interval_seconds=_get_positive_int(
                source, "LONG_TASK_PROGRESS_INTERVAL_SECONDS", 60
            ),
            media_api_base_url=media_base_url,
            media_api_secret_ref=media_secret_ref,
            media_image_model=media_image_model,
            media_audio_model=media_audio_model,
            rate_limit_messages_per_minute=_get_positive_int(
                source, "RATE_LIMIT_MESSAGES_PER_MINUTE", 10
            ),
            max_reply_chars=_get_positive_int(source, "MAX_REPLY_CHARS", 1800),
            fallback_reply=source.get(
                "FALLBACK_REPLY", "智能体暂时不可用，请稍后再试。"
            ),
            log_level=_get_log_level(source),
            orchestration_mode=_get_f0_orchestration_mode(source),
            log_max_bytes=_get_positive_int(source, "LOG_MAX_BYTES", 10485760),
            log_backup_count=_get_positive_int(source, "LOG_BACKUP_COUNT", 5),
        )

    def __getstate__(self):
        return dict(self.__dict__)
