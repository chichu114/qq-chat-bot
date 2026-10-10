from decimal import Decimal
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ConfigTests(unittest.TestCase):
    def base_env(self, **overrides):
        env = {"ONEBOT_WS_URL": "ws://127.0.0.1:3001"}
        env.update(overrides)
        return env

    def test_f0_orchestration_mode_defaults_to_legacy_direct(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env())
        self.assertEqual(config.orchestration_mode, "legacy_direct")

    def test_f0_accepts_direct_and_rejects_unimplemented_modes(self):
        from qq_openclaw_bridge.config import Config

        env = self.base_env(ORCHESTRATION_MODE="direct")
        self.assertEqual(Config.from_env(env).orchestration_mode, "direct")
        for value in ("shadow", "active", "unknown", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config.from_env(self.base_env(ORCHESTRATION_MODE=value))

    def test_shadow_routing_defaults_off(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env())

        self.assertFalse(config.enable_shadow_routing)
        self.assertEqual(config.routing_agent_id, "routing")
        self.assertEqual(config.routing_classifier_transport, "openclaw")

    def test_group_agent_bootstrap_defaults_off(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env())

        self.assertFalse(config.enable_group_agent)
        self.assertEqual(
            config.style_agent_config_path, "config/community-style-agent.json"
        )
        self.assertFalse(config.enable_group_interjections)
        self.assertEqual(config.group_interjection_cooldown_seconds, 10)
        self.assertFalse(config.enable_group_homophone_filter)
        self.assertEqual(
            config.group_homophone_map_path,
            "config/group-homophones.json",
        )

    def test_group_interjection_settings_are_explicit_and_positive(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env(
            ENABLE_GROUP_INTERJECTIONS="true",
            GROUP_INTERJECTION_COOLDOWN_SECONDS="10",
        ))
        self.assertTrue(config.enable_group_interjections)
        self.assertEqual(config.group_interjection_cooldown_seconds, 10)

        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config.from_env(self.base_env(
                    GROUP_INTERJECTION_COOLDOWN_SECONDS=value,
                ))

    def test_group_homophone_filter_requires_group_agent_and_map_path(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env(
            BOT_QQ="999",
            ENABLE_GROUP_AGENT="true",
            ENABLE_GROUP_HOMOPHONE_FILTER="true",
            GROUP_HOMOPHONE_MAP_PATH=" /tmp/group-homophones.json ",
        ))
        self.assertTrue(config.enable_group_homophone_filter)
        self.assertEqual(
            config.group_homophone_map_path, "/tmp/group-homophones.json"
        )

        with self.assertRaisesRegex(ValueError, "ENABLE_GROUP_AGENT"):
            Config.from_env(self.base_env(
                ENABLE_GROUP_HOMOPHONE_FILTER="true",
            ))
        with self.assertRaisesRegex(ValueError, "GROUP_HOMOPHONE_MAP_PATH"):
            Config.from_env(self.base_env(
                BOT_QQ="999",
                ENABLE_GROUP_AGENT="true",
                ENABLE_GROUP_HOMOPHONE_FILTER="true",
                GROUP_HOMOPHONE_MAP_PATH=" ",
            ))

    def test_kanagawave_private_users_are_parsed_as_an_explicit_qq_allowlist(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env(
            ALLOWED_QQ_USERS="10001,20002",
            BOT_QQ="999",
            ENABLE_GROUP_AGENT="true",
            KANAGAWAVE_PRIVATE_USERS="20002",
        ))

        self.assertEqual(config.kanagawave_private_users, {20002})

    def test_kanagawave_private_users_must_also_be_general_private_users(self):
        from qq_openclaw_bridge.config import Config

        with self.assertRaisesRegex(ValueError, "ALLOWED_QQ_USERS"):
            Config.from_env(self.base_env(
                ALLOWED_QQ_USERS="10001",
                BOT_QQ="999",
                ENABLE_GROUP_AGENT="true",
                KANAGAWAVE_PRIVATE_USERS="20002",
            ))

    def test_kanagawave_private_users_require_community_model_runtime(self):
        from qq_openclaw_bridge.config import Config

        with self.assertRaisesRegex(ValueError, "ENABLE_GROUP_AGENT"):
            Config.from_env(self.base_env(
                ALLOWED_QQ_USERS="20002",
                KANAGAWAVE_PRIVATE_USERS="20002",
            ))

    def test_group_agent_requires_bot_sqlite_and_config_path_when_enabled(self):
        from qq_openclaw_bridge.config import Config

        required = {
            "ENABLE_GROUP_AGENT": "true",
            "BOT_QQ": "123456789",
            "ENABLE_SQLITE_STORE": "true",
            "STYLE_AGENT_CONFIG_PATH": "config/community-style-agent.json",
        }
        for missing in ("BOT_QQ", "ENABLE_SQLITE_STORE", "STYLE_AGENT_CONFIG_PATH"):
            with self.subTest(missing=missing):
                env = self.base_env(**required)
                if missing == "ENABLE_SQLITE_STORE":
                    env[missing] = "false"
                elif missing == "STYLE_AGENT_CONFIG_PATH":
                    env[missing] = ""
                else:
                    env.pop(missing)
                with self.assertRaises(ValueError):
                    Config.from_env(env)

    def test_group_agent_parses_custom_path_without_loading_json(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env(
            ENABLE_GROUP_AGENT="true", BOT_QQ="123456789",
            STYLE_AGENT_CONFIG_PATH=" /nonexistent/community-style.json ",
        ))

        self.assertTrue(config.enable_group_agent)
        self.assertEqual(config.style_agent_config_path, "/nonexistent/community-style.json")

    def test_shadow_routing_classifier_transport_accepts_only_supported_values(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env(
            ROUTING_CLASSIFIER_TRANSPORT="responses_api"
        ))

        self.assertEqual(config.routing_classifier_transport, "responses_api")
        for value in ("", "openai", "responses", "OPENCLAW"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "ROUTING_CLASSIFIER_TRANSPORT"
            ):
                Config.from_env(self.base_env(
                    ROUTING_CLASSIFIER_TRANSPORT=value
                ))

    def test_shadow_routing_requires_sqlite(self):
        from qq_openclaw_bridge.config import Config

        with self.assertRaisesRegex(ValueError, "SQLite"):
            Config.from_env(self.base_env(
                ENABLE_SHADOW_ROUTING="true", ENABLE_SQLITE_STORE="false"
            ))

    def test_shadow_routing_keeps_legacy_main_agent_id_parsing_when_disabled(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(self.base_env(
            OPENCLAW_AGENT_ID="legacy/智能体"
        ))

        self.assertEqual(config.openclaw_agent_id, "legacy/智能体")

    def test_shadow_routing_rejects_unbounded_routing_agent_id(self):
        from qq_openclaw_bridge.config import Config

        for value in ("routing/child", "路由", "a" * 129):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "ROUTING_AGENT_ID"
            ):
                Config.from_env(self.base_env(ROUTING_AGENT_ID=value))

    def test_log_rotation_values_must_be_positive(self):
        from qq_openclaw_bridge.config import Config

        env = self.base_env(LOG_MAX_BYTES="10485760", LOG_BACKUP_COUNT="5")
        config = Config.from_env(env)
        self.assertEqual((config.log_max_bytes, config.log_backup_count), (10485760, 5))

    def test_log_level_is_normalized_and_rejects_unknown_values(self):
        from qq_openclaw_bridge.config import Config

        self.assertEqual(Config.from_env(self.base_env(LOG_LEVEL="WARNING")).log_level, "warning")
        with self.assertRaisesRegex(ValueError, "LOG_LEVEL"):
            Config.from_env(self.base_env(LOG_LEVEL="verbose"))

    def test_remote_hosts_normalize_idna_and_deduplicate_in_order(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env({
            "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
            "MEDIA_REMOTE_ALLOWED_HOSTS": (
                "BÜCHER.Example., xn--bcher-kva.example, 例子.测试"
            ),
        })

        self.assertEqual(
            config.media_remote_allowed_hosts,
            ("xn--bcher-kva.example", "xn--fsqu00a.xn--0zwm56d"),
        )

    def test_remote_hosts_reject_non_host_and_invalid_dns_forms(self):
        from qq_openclaw_bridge.config import Config

        invalid = (
            "https://media.example",
            "user@media.example",
            "media.example:443",
            "media.example/path",
            "media.example?query=1",
            "media.example#fragment",
            "*.media.example",
            "bad_host.example",
            "-bad.example",
            "bad-.example",
            "bad..example",
            "a" * 64 + ".example",
            ".".join(("a" * 63,) * 4),
            "127.0.0.1",
            "[2001:db8::1]",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError, "MEDIA_REMOTE_ALLOWED_HOSTS"
                ):
                    Config.from_env({
                        "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                        "MEDIA_REMOTE_ALLOWED_HOSTS": value,
                    })

    def test_loads_defaults_and_required_websocket_url(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env({"ONEBOT_WS_URL": "ws://127.0.0.1:3001"})

        self.assertEqual(config.onebot_ws_url, "ws://127.0.0.1:3001")
        self.assertEqual(config.allowed_qq_users, set())
        self.assertEqual(config.agent_adapter, "echo")
        self.assertEqual(config.openclaw_agent_id, "main")
        self.assertEqual(config.openclaw_session_prefix, "agent:main:qq")
        self.assertTrue(config.openclaw_use_gateway)
        self.assertEqual(config.openclaw_gateway_url, "ws://127.0.0.1:18789")
        self.assertEqual(config.max_concurrent_agent_calls, 2)
        self.assertEqual(config.max_pending_events, 50)
        self.assertEqual(config.agent_retry_attempts, 2)
        self.assertEqual(config.agent_circuit_failure_threshold, 3)
        self.assertEqual(config.agent_circuit_cooldown_seconds, 60)
        self.assertEqual(config.outbox_retry_base_seconds, 2)
        self.assertEqual(config.outbox_max_attempts, 8)
        self.assertTrue(config.enable_style_memory)
        self.assertTrue(config.style_memory_path.endswith(".openclaw/workspace/STYLE.md"))
        self.assertTrue(config.enable_housekeeper)
        self.assertTrue(config.housekeeper_memory_path.endswith(".openclaw/workspace/MEMORY.md"))
        self.assertTrue(config.housekeeper_todo_path.endswith(".openclaw/workspace/TODO.md"))
        self.assertTrue(config.enable_reminder_push)
        self.assertEqual(config.reminder_check_interval_seconds, 30)
        self.assertTrue(config.reminder_state_path.endswith(".openclaw/workspace/REMINDER_STATE.md"))
        self.assertTrue(config.enable_daily_briefing)
        self.assertEqual(config.daily_briefing_time, "09:00")
        self.assertTrue(config.enable_evening_review)
        self.assertEqual(config.evening_review_time, "23:00")
        self.assertEqual(config.quiet_hours_start, "00:00")
        self.assertEqual(config.quiet_hours_end, "08:30")
        self.assertTrue(config.enable_security_confirmation)
        self.assertEqual(config.confirmation_timeout_seconds, 120)
        self.assertTrue(config.enable_auto_backup)
        self.assertEqual(config.auto_backup_interval_hours, 24)
        self.assertEqual(config.auto_backup_retention_days, 30)
        self.assertEqual(config.auto_backup_max_files, 30)
        self.assertTrue(config.enable_sqlite_store)
        self.assertTrue(config.state_database_path.endswith(".openclaw/workspace/chichu.sqlite3"))
        self.assertEqual(config.memory_light_timeout_seconds, 180)
        self.assertEqual(config.rate_limit_messages_per_minute, 10)
        self.assertEqual(config.max_reply_chars, 1800)
        self.assertFalse(config.enable_media_pipeline)
        self.assertFalse(config.enable_long_tasks)
        self.assertEqual(config.media_cache_dir, "~/.openclaw/workspace/media-cache")
        self.assertEqual(config.napcat_media_roots, ())
        self.assertEqual(config.media_remote_allowed_hosts, ())
        self.assertEqual(config.media_cache_ttl_seconds, 3600)
        self.assertEqual(config.media_api_timeout_seconds, 90)
        self.assertEqual(config.media_api_requests_per_minute, 10)
        self.assertEqual(config.media_api_daily_budget, Decimal("0"))
        self.assertEqual(config.media_max_concurrent_calls, 2)
        self.assertEqual(config.long_task_ack_seconds, 15)
        self.assertEqual(config.long_task_progress_interval_seconds, 60)
        self.assertEqual(config.log_level, "info")

    def test_parses_media_pipeline_options_when_enabled(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_MEDIA_PIPELINE": "true",
                "ENABLE_LONG_TASKS": "true",
                "MEDIA_CACHE_DIR": "/tmp/media-cache",
                "MEDIA_CACHE_TTL_SECONDS": "120",
                "MEDIA_API_TIMEOUT_SECONDS": "45",
                "MEDIA_API_REQUESTS_PER_MINUTE": "8",
                "MEDIA_API_DAILY_BUDGET": "1.25",
                "MEDIA_MAX_CONCURRENT_CALLS": "3",
                "LONG_TASK_ACK_SECONDS": "10",
                "LONG_TASK_PROGRESS_INTERVAL_SECONDS": "30",
                "MEDIA_API_BASE_URL": "https://media.example/api",
                "MEDIA_API_SECRET_REF": "keychain:service:account",
                "MEDIA_IMAGE_MODEL": "vision-1",
                "MEDIA_AUDIO_MODEL": "audio-1",
                "NAPCAT_MEDIA_ROOTS": " /srv/napcat/files , /srv/napcat/cache ",
                "MEDIA_REMOTE_ALLOWED_HOSTS": "media.example, cdn.example",
            }
        )

        self.assertTrue(config.enable_media_pipeline)
        self.assertTrue(config.enable_long_tasks)
        self.assertEqual(config.media_cache_dir, "/tmp/media-cache")
        self.assertEqual(config.media_api_daily_budget, Decimal("1.25"))
        self.assertEqual(config.media_api_secret_ref, "keychain:service:account")
        self.assertEqual(config.media_image_model, "vision-1")
        self.assertEqual(config.media_audio_model, "audio-1")
        self.assertEqual(
            config.napcat_media_roots,
            ("/srv/napcat/files", "/srv/napcat/cache"),
        )
        self.assertEqual(
            config.media_remote_allowed_hosts,
            ("media.example", "cdn.example"),
        )

    def test_media_pipeline_requires_at_least_one_trusted_source(self):
        from qq_openclaw_bridge.config import Config

        with self.assertRaisesRegex(ValueError, "NAPCAT_MEDIA_ROOTS.*MEDIA_REMOTE_ALLOWED_HOSTS"):
            Config.from_env(
                {
                    "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                    "ENABLE_MEDIA_PIPELINE": "true",
                    "MEDIA_API_BASE_URL": "https://media.example",
                    "MEDIA_API_KEY": "not-a-real-key",
                    "MEDIA_IMAGE_MODEL": "vision-1",
                    "MEDIA_AUDIO_MODEL": "audio-1",
                }
            )

    def test_media_pipeline_requires_complete_credentials_and_models(self):
        from qq_openclaw_bridge.config import Config

        required = {
            "MEDIA_API_BASE_URL": "https://media.example",
            "MEDIA_API_KEY": "not-a-real-key",
            "MEDIA_IMAGE_MODEL": "vision-1",
            "MEDIA_AUDIO_MODEL": "audio-1",
        }
        for missing in required:
            with self.subTest(missing=missing):
                env = {"ONEBOT_WS_URL": "ws://127.0.0.1:3001", "ENABLE_MEDIA_PIPELINE": "true"}
                env.update(required)
                del env[missing]
                with self.assertRaisesRegex(ValueError, missing):
                    Config.from_env(env)

    def test_media_budget_rejects_negative_values_and_positive_limits_reject_zero(self):
        from qq_openclaw_bridge.config import Config

        base = {"ONEBOT_WS_URL": "ws://127.0.0.1:3001"}
        for name in (
            "MEDIA_CACHE_TTL_SECONDS",
            "MEDIA_API_TIMEOUT_SECONDS",
            "MEDIA_API_REQUESTS_PER_MINUTE",
            "MEDIA_MAX_CONCURRENT_CALLS",
            "LONG_TASK_ACK_SECONDS",
            "LONG_TASK_PROGRESS_INTERVAL_SECONDS",
        ):
            with self.subTest(name=name):
                env = dict(base)
                env[name] = "0"
                with self.assertRaisesRegex(ValueError, "greater than 0"):
                    Config.from_env(env)

        with self.assertRaisesRegex(ValueError, "MEDIA_API_DAILY_BUDGET"):
            Config.from_env(dict(base, MEDIA_API_DAILY_BUDGET="-0.01"))

    def test_media_budget_rejects_nonfinite_values_and_preserves_decimal_precision(self):
        from qq_openclaw_bridge.config import Config

        base = {"ONEBOT_WS_URL": "ws://127.0.0.1:3001"}
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "MEDIA_API_DAILY_BUDGET"):
                    Config.from_env(dict(base, MEDIA_API_DAILY_BUDGET=value))

        config = Config.from_env(
            dict(base, MEDIA_API_DAILY_BUDGET="0.1000000000000000001")
        )
        self.assertEqual(
            config.media_api_daily_budget,
            Decimal("0.1000000000000000001"),
        )

    def test_media_key_is_not_exposed_by_repr_or_serialized_state(self):
        from qq_openclaw_bridge.config import Config

        secret = "not-a-real-key"
        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_MEDIA_PIPELINE": "true",
                "MEDIA_API_BASE_URL": "https://media.example",
                "MEDIA_API_KEY": secret,
                "MEDIA_IMAGE_MODEL": "vision-1",
                "MEDIA_AUDIO_MODEL": "audio-1",
                "MEDIA_REMOTE_ALLOWED_HOSTS": "media.example",
            }
        )

        self.assertNotIn(secret, repr(config))
        self.assertNotIn(secret, repr(config.__getstate__()))

    def test_parses_reminder_push_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_REMINDER_PUSH": "false",
                "REMINDER_CHECK_INTERVAL_SECONDS": "10",
                "REMINDER_STATE_PATH": "/tmp/chichu-reminders.md",
            }
        )

        self.assertFalse(config.enable_reminder_push)
        self.assertEqual(config.reminder_check_interval_seconds, 10)
        self.assertEqual(config.reminder_state_path, "/tmp/chichu-reminders.md")

    def test_parses_phase_a_housekeeper_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_DAILY_BRIEFING": "false",
                "DAILY_BRIEFING_TIME": "08:45",
                "ENABLE_EVENING_REVIEW": "false",
                "EVENING_REVIEW_TIME": "22:30",
                "QUIET_HOURS_START": "23:30",
                "QUIET_HOURS_END": "09:15",
            }
        )

        self.assertFalse(config.enable_daily_briefing)
        self.assertEqual(config.daily_briefing_time, "08:45")
        self.assertFalse(config.enable_evening_review)
        self.assertEqual(config.evening_review_time, "22:30")
        self.assertEqual(config.quiet_hours_start, "23:30")
        self.assertEqual(config.quiet_hours_end, "09:15")

    def test_parses_housekeeper_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_HOUSEKEEPER": "false",
                "HOUSEKEEPER_MEMORY_PATH": "/tmp/chichu-memory.md",
                "HOUSEKEEPER_TODO_PATH": "/tmp/chichu-todo.md",
            }
        )

        self.assertFalse(config.enable_housekeeper)
        self.assertEqual(config.housekeeper_memory_path, "/tmp/chichu-memory.md")
        self.assertEqual(config.housekeeper_todo_path, "/tmp/chichu-todo.md")

    def test_parses_security_confirmation_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_SECURITY_CONFIRMATION": "false",
                "CONFIRMATION_TIMEOUT_SECONDS": "300",
            }
        )

        self.assertFalse(config.enable_security_confirmation)
        self.assertEqual(config.confirmation_timeout_seconds, 300)

    def test_parses_auto_backup_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_AUTO_BACKUP": "false",
                "AUTO_BACKUP_INTERVAL_HOURS": "12",
                "AUTO_BACKUP_RETENTION_DAYS": "14",
                "AUTO_BACKUP_MAX_FILES": "10",
                "AUTO_BACKUP_DIR": "/tmp/chichu-backups",
            }
        )

        self.assertFalse(config.enable_auto_backup)
        self.assertEqual(config.auto_backup_interval_hours, 12)
        self.assertEqual(config.auto_backup_retention_days, 14)
        self.assertEqual(config.auto_backup_max_files, 10)
        self.assertEqual(config.auto_backup_dir, "/tmp/chichu-backups")

    def test_parses_sqlite_store_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_SQLITE_STORE": "false",
                "STATE_DATABASE_PATH": "/tmp/chichu.sqlite3",
            }
        )

        self.assertFalse(config.enable_sqlite_store)
        self.assertEqual(config.state_database_path, "/tmp/chichu.sqlite3")

    def test_parses_e3_memory_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_MEMORY_2": "true",
                "MEMORY_LIGHT_MODEL": "openai/gpt-5.4-nano",
                "MEMORY_LIGHT_TIMEOUT_SECONDS": "45",
                "MEMORY_SUMMARY_TRIGGER_TURNS": "6",
                "MEMORY_SUMMARY_BATCH_TURNS": "18",
                "MEMORY_CONTEXT_MAX_CHARS": "1800",
                "MEMORY_FACT_LIMIT": "5",
                "MEMORY_SUMMARY_LIMIT": "2",
                "MEMORY_CANDIDATE_MIN_CONFIDENCE_PERCENT": "80",
            }
        )

        self.assertTrue(config.enable_memory2)
        self.assertEqual(config.memory_light_model, "openai/gpt-5.4-nano")
        self.assertEqual(config.memory_light_timeout_seconds, 45)
        self.assertEqual(config.memory_summary_trigger_turns, 6)
        self.assertEqual(config.memory_summary_batch_turns, 18)
        self.assertEqual(config.memory_context_max_chars, 1800)
        self.assertEqual(config.memory_candidate_min_confidence, 0.8)

    def test_memory_session_prefix_defaults_to_configured_agent(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "OPENCLAW_AGENT_ID": "qq",
            }
        )

        self.assertEqual(config.memory_light_session_prefix, "agent:qq:memory")

    def test_parses_style_memory_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ENABLE_STYLE_MEMORY": "false",
                "STYLE_MEMORY_PATH": "/tmp/chichu-style.md",
            }
        )

        self.assertFalse(config.enable_style_memory)
        self.assertEqual(config.style_memory_path, "/tmp/chichu-style.md")

    def test_parses_openclaw_session_prefix(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "OPENCLAW_SESSION_PREFIX": "agent:main:qq-chichu-v2",
            }
        )

        self.assertEqual(config.openclaw_session_prefix, "agent:main:qq-chichu-v2")

    def test_parses_openclaw_agent_id(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "OPENCLAW_AGENT_ID": "qq",
            }
        )

        self.assertEqual(config.openclaw_agent_id, "qq")

    def test_parses_e2_reliability_options(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "OPENCLAW_USE_GATEWAY": "false",
                "OPENCLAW_GATEWAY_PROBE_URL": "ws://127.0.0.1:19999",
                "MAX_CONCURRENT_AGENT_CALLS": "4",
                "MAX_PENDING_EVENTS": "80",
                "AGENT_RETRY_ATTEMPTS": "3",
                "AGENT_CIRCUIT_FAILURE_THRESHOLD": "5",
                "AGENT_CIRCUIT_COOLDOWN_SECONDS": "90",
                "OUTBOX_RETRY_BASE_SECONDS": "4",
                "OUTBOX_MAX_ATTEMPTS": "10",
            }
        )

        self.assertFalse(config.openclaw_use_gateway)
        self.assertEqual(config.openclaw_gateway_url, "ws://127.0.0.1:19999")
        self.assertEqual(config.max_concurrent_agent_calls, 4)
        self.assertEqual(config.max_pending_events, 80)
        self.assertEqual(config.agent_retry_attempts, 3)
        self.assertEqual(config.agent_circuit_failure_threshold, 5)
        self.assertEqual(config.agent_circuit_cooldown_seconds, 90)
        self.assertEqual(config.outbox_retry_base_seconds, 4)
        self.assertEqual(config.outbox_max_attempts, 10)

    def test_parses_comma_separated_allowed_users(self):
        from qq_openclaw_bridge.config import Config

        config = Config.from_env(
            {
                "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                "ALLOWED_QQ_USERS": "10001, 10002,,10003",
            }
        )

        self.assertEqual(config.allowed_qq_users, {10001, 10002, 10003})

    def test_requires_onebot_websocket_url(self):
        from qq_openclaw_bridge.config import Config

        with self.assertRaisesRegex(ValueError, "ONEBOT_WS_URL"):
            Config.from_env({})

    def test_rejects_invalid_allowed_user_id(self):
        from qq_openclaw_bridge.config import Config

        with self.assertRaisesRegex(ValueError, "ALLOWED_QQ_USERS"):
            Config.from_env(
                {
                    "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
                    "ALLOWED_QQ_USERS": "123,abc",
                }
            )

    def test_rejects_non_positive_runtime_limits(self):
        from qq_openclaw_bridge.config import Config

        for name in (
            "OPENCLAW_TIMEOUT_SECONDS",
            "REMINDER_CHECK_INTERVAL_SECONDS",
            "CONFIRMATION_TIMEOUT_SECONDS",
            "AUTO_BACKUP_INTERVAL_HOURS",
            "AUTO_BACKUP_RETENTION_DAYS",
            "AUTO_BACKUP_MAX_FILES",
            "RATE_LIMIT_MESSAGES_PER_MINUTE",
            "MAX_REPLY_CHARS",
            "MAX_CONCURRENT_AGENT_CALLS",
            "MAX_PENDING_EVENTS",
            "AGENT_RETRY_ATTEMPTS",
            "AGENT_CIRCUIT_FAILURE_THRESHOLD",
            "AGENT_CIRCUIT_COOLDOWN_SECONDS",
            "OUTBOX_RETRY_BASE_SECONDS",
            "OUTBOX_MAX_ATTEMPTS",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "greater than 0"):
                    Config.from_env(
                        {"ONEBOT_WS_URL": "ws://127.0.0.1:3001", name: "0"}
                    )


if __name__ == "__main__":
    unittest.main()
