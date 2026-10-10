import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest import mock
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from qq_openclaw_bridge.runtime.community_style_config import (
    AuthorizationSettings,
    CommunityStyleSettings,
    OllamaSettings,
    SourceSettings,
)
from qq_openclaw_bridge.domain.community_style import SourcePage


NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def settings(*sources, fingerprint="config-a", enabled=True, model="qwen3:8b"):
    return CommunityStyleSettings(
        enabled=enabled,
        ollama=OllamaSettings(
            base_url="http://127.0.0.1:11434",
            model=model,
            allow_remote=False,
        ),
        sources=tuple(sources),
        groups=(),
        fingerprint=fingerprint,
    )


def source(identifier, platform="tieba", community="example"):
    return SourceSettings(
        id=identifier,
        platform=platform,
        community=community,
        profile="profile-" + identifier,
        schedule_seconds=300,
        authorizations=(
            AuthorizationSettings(
                subject_type="post",
                subject_key="123",
                scope="single_post",
                evidence_ref="consent-record",
                granted_at="2026-09-20T00:00:00Z",
            ),
        ),
    )


class FakeReloader:
    def __init__(self, current):
        self.value = current
        self.changed = False
        self.reload_calls = 0

    def current(self):
        return self.value

    def reload_if_changed(self):
        self.reload_calls += 1
        changed = self.changed
        self.changed = False
        return changed


class RaisingReloader(FakeReloader):
    def __init__(self, current, error):
        super().__init__(current)
        self.error = error

    def reload_if_changed(self):
        raise self.error


class FakeRepository:
    def __init__(self):
        self.synced = []
        self.affected = ()
        self.samples = {}
        self.published = []

    def sync_configuration(self, value, now):
        self.synced.append((value, now))
        return self.affected

    def active_samples(self, profile_key):
        return self.samples.get(profile_key, ())

    def publish_profile(self, profile, now):
        self.published.append((profile, now))
        return {"version": 1}


class FakeSync:
    def __init__(self, repository, sources, analyzer, **unused):
        self.repository = repository
        self.sources = dict(sources)
        self.analyzer = analyzer
        self.recover_calls = 0
        self.run_due_calls = 0
        self.started = None
        self.release = None

    async def recover_interrupted(self):
        self.recover_calls += 1
        return 0

    async def run_due_once(self):
        self.run_due_calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        return type("Outcome", (), {"status": "idle", "source_id": None})()


class FakeSecretResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, reference, fallback):
        self.calls.append((reference, fallback))
        return "rotated-token-%d" % len(self.calls)


class StyleLearnerMainTests(unittest.TestCase):
    def test_disabled_main_exits_without_config_file_database_or_secrets(self):
        from qq_openclaw_style_learner import __main__ as learner_main

        malicious = "do-not-open-style.json"
        env = {
            "ENABLE_GROUP_AGENT": "false",
            "STYLE_AGENT_CONFIG_PATH": malicious,
            "STATE_DATABASE_PATH": "do-not-open.sqlite3",
            "REDDIT_OAUTH_TOKEN_REF": "keychain:private:secret",
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            learner_main.Config, "from_env"
        ) as parse, mock.patch.object(
            learner_main, "build_style_learner"
        ) as build, mock.patch.object(
            learner_main, "CommunityStyleConfigReloader"
        ) as reloader, mock.patch.object(
            learner_main, "SecretResolver"
        ) as resolver:
            self.assertEqual(learner_main.main([]), 0)

        parse.assert_not_called()
        build.assert_not_called()
        reloader.assert_not_called()
        resolver.assert_not_called()

    def test_once_recovers_then_runs_at_most_one_due_source(self):
        from qq_openclaw_style_learner import __main__ as learner_main

        order = []

        class Learner:
            async def recover_interrupted(self):
                order.append("recover")

            async def run_due_once(self):
                order.append("due")

            async def loop(self, stop_event):
                order.append("loop")

        env = {
            "ENABLE_GROUP_AGENT": "true",
            "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
            "BOT_QQ": "12345",
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(
            learner_main, "build_style_learner", return_value=Learner()
        ), mock.patch.object(
            learner_main, "configure_process_logging", return_value=mock.Mock()
        ):
            self.assertEqual(
                learner_main.main(["--once", "--project-dir", temporary]), 0
            )

        self.assertEqual(order, ["recover", "due"])

    def test_enabled_startup_logs_only_exception_class(self):
        from qq_openclaw_style_learner import __main__ as learner_main

        logger = mock.Mock()
        secret = "oauth-super-secret"
        env = {
            "ENABLE_GROUP_AGENT": "true",
            "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
            "BOT_QQ": "12345",
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(
            learner_main, "configure_process_logging", return_value=logger
        ), mock.patch.object(
            learner_main,
            "build_style_learner",
            side_effect=RuntimeError(secret),
        ):
            self.assertEqual(learner_main.main(["--project-dir", temporary]), 1)

        rendered = repr(logger.method_calls)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn(secret, rendered)

    def test_project_env_is_parsed_as_data_and_never_executed(self):
        from qq_openclaw_style_learner import __main__ as learner_main

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            sentinel = project / "shell-executed"
            env_file = project / ".env"
            env_file.write_text(
                "ENABLE_GROUP_AGENT=false\n"
                "UNUSED=$(touch %s)\n" % sentinel,
                encoding="utf-8",
            )
            env_file.chmod(0o600)
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                learner_main.Config, "from_env"
            ) as parse:
                self.assertEqual(
                    learner_main.main(["--project-dir", str(project)]), 0
                )
            parse.assert_not_called()
            self.assertFalse(sentinel.exists())

    def test_enabled_parser_rejects_string_subclasses_without_calling_them(self):
        from qq_openclaw_style_learner.__main__ import _enabled

        class Hostile(str):
            def strip(self):
                raise AssertionError("attacker code ran")

        with self.assertRaisesRegex(ValueError, "must be true or false"):
            _enabled({"ENABLE_GROUP_AGENT": Hostile("false")})

    def test_reddit_reference_is_lazy_and_resolves_for_each_request(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        repository = FakeRepository()
        reloader = FakeReloader(settings(source("reddit-a", "reddit")))
        resolver = FakeSecretResolver()
        providers = []

        def source_factory(value, token_provider=None):
            providers.append(token_provider)
            return object()

        runtime = StyleLearnerRuntime(
            repository,
            reloader,
            {"REDDIT_OAUTH_TOKEN_REF": "keychain:reddit:bearer"},
            source_factory=source_factory,
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            secret_resolver=resolver,
            clock=lambda: NOW,
        )
        self.assertIsNotNone(runtime)
        self.assertEqual(providers[0](), "rotated-token-1")
        self.assertEqual(providers[0](), "rotated-token-2")
        self.assertEqual(
            resolver.calls,
            [
                ("keychain:reddit:bearer", None),
                ("keychain:reddit:bearer", None),
            ],
        )

        missing_providers = []
        missing = StyleLearnerRuntime(
            FakeRepository(),
            FakeReloader(settings(source("reddit-missing", "reddit"))),
            {},
            source_factory=lambda value, token_provider=None: (
                missing_providers.append(token_provider) or object()
            ),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            secret_resolver=resolver,
            clock=lambda: NOW,
        )
        self.assertEqual(set(missing.sources), {"reddit-missing"})
        with self.assertRaisesRegex(ValueError, "token unavailable") as caught:
            missing_providers[0]()
        self.assertEqual(caught.exception.args, ("Reddit OAuth token unavailable",))
        self.assertIsNone(caught.exception.__context__)

        tieba = StyleLearnerRuntime(
            FakeRepository(),
            FakeReloader(settings(source("tieba-a"))),
            {},
            source_factory=source_factory,
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            secret_resolver=resolver,
            clock=lambda: NOW,
        )
        self.assertIsNotNone(tieba)

    def test_reddit_rejects_plaintext_fallback_lazily_and_redacts_errors(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        plaintext_providers = []
        StyleLearnerRuntime(
            FakeRepository(),
            FakeReloader(settings(source("reddit-a", "reddit"))),
            {"REDDIT_OAUTH_TOKEN": "plaintext-must-not-be-used"},
            source_factory=lambda value, token_provider=None: (
                plaintext_providers.append(token_provider) or object()
            ),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(ValueError, "token unavailable"):
            plaintext_providers[0]()

        class Resolver:
            def resolve(self, reference, fallback):
                raise RuntimeError("plaintext-must-not-leak")

        providers = []
        StyleLearnerRuntime(
            FakeRepository(),
            FakeReloader(settings(source("reddit-a", "reddit"))),
            {"REDDIT_OAUTH_TOKEN_REF": "keychain:reddit:bearer"},
            source_factory=lambda value, token_provider=None: (
                providers.append(token_provider) or object()
            ),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            secret_resolver=Resolver(),
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(ValueError, "resolution failed") as caught:
            providers[0]()
        self.assertNotIn("plaintext-must-not-leak", repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

        class HostileString(str):
            def strip(self):
                raise AssertionError("attacker string method ran")

        hostile_reference_providers = []
        StyleLearnerRuntime(
            FakeRepository(),
            FakeReloader(settings(source("reddit-a", "reddit"))),
            {"REDDIT_OAUTH_TOKEN_REF": HostileString("keychain:a:b")},
            source_factory=lambda value, token_provider=None: (
                hostile_reference_providers.append(token_provider) or object()
            ),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(ValueError, "token unavailable"):
            hostile_reference_providers[0]()

        class HostileResolver:
            def resolve(self, reference, fallback):
                return HostileString("secret")

        hostile_providers = []
        StyleLearnerRuntime(
            FakeRepository(),
            FakeReloader(settings(source("reddit-a", "reddit"))),
            {"REDDIT_OAUTH_TOKEN_REF": "keychain:reddit:bearer"},
            source_factory=lambda value, token_provider=None: (
                hostile_providers.append(token_provider) or object()
            ),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            secret_resolver=HostileResolver(),
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(ValueError, "resolution failed"):
            hostile_providers[0]()

    def test_tieba_only_config_ignores_even_hostile_reddit_reference(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        class HostileReference(str):
            def strip(self):
                raise AssertionError("unused Reddit reference was inspected")

        runtime = StyleLearnerRuntime(
            FakeRepository(),
            FakeReloader(settings(source("tieba-a"))),
            {"REDDIT_OAUTH_TOKEN_REF": HostileReference("bad")},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        self.assertEqual(set(runtime.sources), {"tieba-a"})

    def test_real_composition_builds_tieba_runtime_without_network_access(self):
        from qq_openclaw_bridge.config import Config
        from qq_openclaw_style_learner.__main__ import build_style_learner

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            config_path = project / "community-style.json"
            config_path.write_text(
                """{
                  "enabled": true,
                  "ollama": {"base_url": "http://127.0.0.1:11434", "model": "qwen3:8b", "allow_remote": false},
                  "sources": [{
                    "id": "tieba-a", "platform": "tieba", "community": "example",
                    "profile": "profile-a", "schedule_seconds": 300,
                    "authorizations": [{
                      "subject_type": "post", "subject_key": "123", "scope": "single_post",
                      "evidence_ref": "consent-record", "granted_at": "2026-09-20T00:00:00Z"
                    }]
                  }],
                  "groups": []
                }""",
                encoding="utf-8",
            )
            config = Config(
                onebot_ws_url="ws://127.0.0.1:3001",
                allowed_qq_users=set(),
                bot_qq=12345,
                enable_group_agent=True,
                style_agent_config_path=str(config_path),
                state_database_path=str(project / "state.sqlite3"),
            )
            runtime = build_style_learner(config, env={}, project_dir=project)

            self.assertEqual(set(runtime.sources), {"tieba-a"})
            self.assertTrue((project / "state.sqlite3").is_file())

    def test_reload_reuses_unchanged_adapter_and_rebuilds_only_changed_source(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        first_a = source("a", community="first")
        first_b = source("b", community="same")
        reloader = FakeReloader(settings(first_a, first_b))
        built = []

        def source_factory(value, token_provider=None):
            adapter = object()
            built.append((value.id, value.community, adapter))
            return adapter

        runtime = StyleLearnerRuntime(
            FakeRepository(), reloader, {},
            source_factory=source_factory,
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        old_a = runtime.sources["a"]
        old_b = runtime.sources["b"]
        reloader.value = settings(
            replace(first_a, community="changed"), first_b, fingerprint="config-b"
        )
        reloader.changed = True

        self.assertTrue(runtime.refresh_configuration())
        self.assertIsNot(runtime.sources["a"], old_a)
        self.assertIs(runtime.sources["b"], old_b)
        self.assertEqual([(item[0], item[1]) for item in built], [
            ("a", "first"), ("b", "same"), ("a", "changed")
        ])

    def test_invalid_or_partial_reload_keeps_snapshot_and_adapters(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        reloader = FakeReloader(settings(source("a")))
        repository = FakeRepository()
        runtime = StyleLearnerRuntime(
            repository, reloader, {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        before_settings = runtime.settings
        before_adapter = runtime.sources["a"]

        self.assertFalse(runtime.refresh_configuration())
        self.assertIs(runtime.settings, before_settings)
        self.assertIs(runtime.sources["a"], before_adapter)
        self.assertGreaterEqual(len(repository.synced), 2)

    def test_malicious_control_exception_is_sanitized_and_old_runtime_survives(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        baseline = settings(source("a"))
        reloader = FakeReloader(baseline)
        runtime = StyleLearnerRuntime(
            FakeRepository(), reloader, {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        adapter = runtime.sources["a"]

        class HostileInterrupt(KeyboardInterrupt):
            pass

        reloader.reload_if_changed = mock.Mock(
            side_effect=HostileInterrupt("source-body-and-token")
        )
        with self.assertRaisesRegex(RuntimeError, "configuration sync failed") as caught:
            runtime.refresh_configuration()
        self.assertEqual(caught.exception.args, ("style configuration sync failed",))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIs(runtime.settings, baseline)
        self.assertIs(runtime.sources["a"], adapter)

    def test_exact_keyboard_interrupt_propagates_without_payload(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        runtime = StyleLearnerRuntime(
            FakeRepository(), FakeReloader(settings(source("a"))), {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        runtime.config_reloader.reload_if_changed = mock.Mock(
            side_effect=KeyboardInterrupt("source-body-and-token")
        )
        with self.assertRaises(KeyboardInterrupt) as caught:
            runtime.refresh_configuration()
        self.assertEqual(caught.exception.args, ())
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_generator_exit_is_clean_and_subclasses_are_sanitized(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        runtime = StyleLearnerRuntime(
            FakeRepository(), FakeReloader(settings(source("a"))), {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        runtime.config_reloader.reload_if_changed = mock.Mock(
            side_effect=GeneratorExit("source-body-and-token")
        )
        with self.assertRaises(GeneratorExit) as caught:
            runtime.refresh_configuration()
        self.assertEqual(caught.exception.args, ())
        self.assertIsNone(caught.exception.__context__)

        class HostileGeneratorExit(GeneratorExit):
            pass

        runtime.config_reloader.reload_if_changed = mock.Mock(
            side_effect=HostileGeneratorExit("source-body-and-token")
        )
        with self.assertRaisesRegex(RuntimeError, "configuration sync failed") as caught:
            runtime.refresh_configuration()
        self.assertNotIn("source-body-and-token", repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    def test_main_propagates_clean_generator_exit_and_sanitizes_subclass(self):
        from qq_openclaw_style_learner import __main__ as learner_main

        env = {
            "ENABLE_GROUP_AGENT": "true",
            "ONEBOT_WS_URL": "ws://127.0.0.1:3001",
            "BOT_QQ": "12345",
        }

        class Learner:
            async def recover_interrupted(self):
                raise GeneratorExit("source-body-and-token")

            async def run_due_once(self):
                raise AssertionError

            async def loop(self, stop_event):
                raise AssertionError

        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(
            learner_main, "build_style_learner", return_value=Learner()
        ), mock.patch.object(
            learner_main, "configure_process_logging", return_value=mock.Mock()
        ):
            with self.assertRaises(GeneratorExit) as caught:
                learner_main.main(["--once", "--project-dir", temporary])
        self.assertEqual(caught.exception.args, ())
        self.assertIsNone(caught.exception.__context__)

        class HostileGeneratorExit(GeneratorExit):
            pass

        logger = mock.Mock()

        class HostileLearner(Learner):
            async def recover_interrupted(self):
                raise HostileGeneratorExit("source-body-and-token")

        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(
            learner_main, "build_style_learner", return_value=HostileLearner()
        ), mock.patch.object(
            learner_main, "configure_process_logging", return_value=logger
        ):
            self.assertEqual(
                learner_main.main(["--once", "--project-dir", temporary]), 1
            )
        self.assertNotIn("source-body-and-token", repr(logger.method_calls))

    def test_valid_reload_apply_failure_keeps_old_snapshot_and_adapters(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        repository = FakeRepository()
        reloader = FakeReloader(settings(source("a", community="old")))
        runtime = StyleLearnerRuntime(
            repository, reloader, {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        previous = runtime.settings
        adapter = runtime.sources["a"]
        reloader.value = settings(source("a", community="new"), fingerprint="new")
        reloader.changed = True
        repository.sync_configuration = mock.Mock(
            side_effect=OSError("database-content-secret")
        )

        with self.assertRaisesRegex(RuntimeError, "configuration sync failed") as caught:
            runtime.refresh_configuration()
        self.assertNotIn("database-content-secret", repr(caught.exception))
        self.assertIs(runtime.settings, previous)
        self.assertIs(runtime.sources["a"], adapter)

    def test_revocation_rebuilds_affected_profile_from_remaining_samples(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        repository = FakeRepository()
        repository.affected = ("profile-a",)
        profile = object()
        aggregate_calls = []

        def aggregate(profile_key, samples, minimum_samples=5):
            aggregate_calls.append((profile_key, samples, minimum_samples))
            return profile

        StyleLearnerRuntime(
            repository,
            FakeReloader(settings(source("a"))),
            {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            aggregate_factory=aggregate,
            clock=lambda: NOW,
        )

        self.assertEqual(aggregate_calls, [("profile-a", (), 5)])
        self.assertEqual(repository.published, [(profile, NOW)])


class StyleLearnerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_generator_exit_is_clean_and_subclass_is_fixed_failure(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        runtime = StyleLearnerRuntime(
            FakeRepository(), FakeReloader(settings(source("a"))), {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )

        class ExactSync(FakeSync):
            async def run_due_once(self):
                raise GeneratorExit("source-body-and-token")

        runtime._sync = ExactSync(None, {}, object())
        with self.assertRaises(GeneratorExit) as caught:
            await runtime.run_due_once()
        self.assertEqual(caught.exception.args, ())
        self.assertIsNone(caught.exception.__context__)

        class HostileGeneratorExit(GeneratorExit):
            pass

        class HostileSync(FakeSync):
            async def run_due_once(self):
                raise HostileGeneratorExit("source-body-and-token")

        runtime._sync = HostileSync(None, {}, object())
        with self.assertRaisesRegex(RuntimeError, "style sync failed") as caught:
            await runtime.run_due_once()
        self.assertNotIn("source-body-and-token", repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    async def test_mixed_sources_isolate_missing_reddit_credential(self):
        from qq_openclaw_bridge.runtime.reddit_source import RedditSource
        from qq_openclaw_bridge.storage import WorkspaceStateStore
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        class EmptyTieba:
            def list_candidates(self, cursor, limit):
                return SourcePage(items=(), next_cursor=())

            def fetch_content(self, candidate):
                raise AssertionError("empty source must not fetch")

        def no_network(*args, **kwargs):
            raise AssertionError("missing Reddit token must fail before network")

        def factory(value, token_provider=None):
            if value.platform == "tieba":
                return EmptyTieba()
            return RedditSource(value, token_provider=token_provider, opener=no_network)

        with tempfile.TemporaryDirectory() as temporary:
            repository = WorkspaceStateStore(
                Path(temporary) / "state.sqlite3"
            ).community_style_repository
            runtime = StyleLearnerRuntime(
                repository,
                FakeReloader(settings(
                    source("a-tieba"), source("z-reddit", "reddit")
                )),
                {},
                source_factory=factory,
                analyzer_factory=lambda value: type(
                    "Analyzer", (), {"analyze": lambda self, text: None}
                )(),
            )
            tieba_outcome = await runtime.run_due_once()
            reddit_outcome = await runtime.run_due_once()

        self.assertEqual((tieba_outcome.status, tieba_outcome.source_id), (
            "synced", "a-tieba"
        ))
        self.assertEqual((reddit_outcome.status, reddit_outcome.source_id), (
            "paused", "z-reddit"
        ))
        self.assertEqual(reddit_outcome.safe_error_class, "reddit_auth")

    async def test_failed_profile_rebuild_blocks_old_runtime_until_atomic_swap(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        repository = FakeRepository()
        reloader = FakeReloader(settings(source("a", community="old")))
        syncs = []

        def sync_factory(*args, **kwargs):
            value = FakeSync(*args, **kwargs)
            syncs.append(value)
            return value

        runtime = StyleLearnerRuntime(
            repository, reloader, {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=sync_factory,
            aggregate_factory=lambda *args, **kwargs: object(),
            clock=lambda: NOW,
        )
        old_sync = syncs[0]
        reloader.value = settings(source("a", community="new"), fingerprint="new")
        reloader.changed = True
        repository.affected = ("profile-a",)
        repository.publish_profile = mock.Mock(
            side_effect=OSError("database-secret")
        )

        with self.assertRaisesRegex(RuntimeError, "configuration sync failed"):
            await runtime.run_due_once()
        self.assertEqual(old_sync.run_due_calls, 0)
        self.assertEqual(runtime.settings.fingerprint, "config-a")

        repository.affected = ()
        repository.publish_profile = mock.Mock(return_value={"version": 1})
        await runtime.run_due_once()
        self.assertEqual(old_sync.run_due_calls, 0)
        self.assertEqual(runtime.settings.fingerprint, "new")
        self.assertEqual(len(syncs), 2)
        self.assertEqual(syncs[1].run_due_calls, 1)

    async def test_stage_or_config_sync_failure_never_polls_old_runtime(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        for failure in ("stage", "sync"):
            with self.subTest(failure=failure):
                repository = FakeRepository()
                reloader = FakeReloader(settings(source("a", community="old")))
                syncs = []

                def source_factory(value, token_provider=None):
                    if failure == "stage" and value.community == "new":
                        raise OSError("source-secret")
                    return object()

                def sync_factory(*args, **kwargs):
                    value = FakeSync(*args, **kwargs)
                    syncs.append(value)
                    return value

                runtime = StyleLearnerRuntime(
                    repository, reloader, {},
                    source_factory=source_factory,
                    analyzer_factory=lambda value: object(),
                    sync_factory=sync_factory,
                    clock=lambda: NOW,
                )
                old_sync = syncs[0]
                reloader.value = settings(
                    source("a", community="new"), fingerprint="new"
                )
                reloader.changed = True
                if failure == "sync":
                    repository.sync_configuration = mock.Mock(
                        side_effect=OSError("database-secret")
                    )

                with self.assertRaisesRegex(RuntimeError, "configuration sync failed"):
                    await runtime.run_due_once()
                self.assertEqual(old_sync.run_due_calls, 0)
                self.assertEqual(runtime.settings.fingerprint, "config-a")

    async def test_each_due_poll_attempts_hot_reload_first(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        reloader = FakeReloader(settings(source("a")))
        runtime = StyleLearnerRuntime(
            FakeRepository(), reloader, {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            clock=lambda: NOW,
        )
        initial_calls = reloader.reload_calls
        await runtime.run_due_once()
        await runtime.run_due_once()
        self.assertEqual(reloader.reload_calls, initial_calls + 2)
        self.assertEqual(runtime._sync.run_due_calls, 2)

    async def test_signal_stop_drains_current_sync_without_claiming_another(self):
        from qq_openclaw_style_learner.__main__ import StyleLearnerRuntime

        runtime = StyleLearnerRuntime(
            FakeRepository(),
            FakeReloader(settings(source("a"))),
            {},
            source_factory=lambda value, token_provider=None: object(),
            analyzer_factory=lambda value: object(),
            sync_factory=FakeSync,
            poll_seconds=0.01,
            clock=lambda: NOW,
        )
        runtime._sync.started = asyncio.Event()
        runtime._sync.release = asyncio.Event()
        stop = asyncio.Event()
        task = asyncio.create_task(runtime.loop(stop))
        await asyncio.wait_for(runtime._sync.started.wait(), timeout=1)
        stop.set()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        runtime._sync.release.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(runtime._sync.run_due_calls, 1)

    async def test_bounded_analyzer_serializes_concurrent_calls(self):
        from qq_openclaw_style_learner.__main__ import _BoundedAnalyzer

        lock = threading.Lock()
        active = 0
        peak = 0

        class Analyzer:
            def analyze(self, text):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.02)
                with lock:
                    active -= 1
                return text

        analyzer = _BoundedAnalyzer(Analyzer(), 1)
        await asyncio.gather(*(
            asyncio.to_thread(analyzer.analyze, str(index)) for index in range(4)
        ))
        self.assertEqual(peak, 1)


if __name__ == "__main__":
    unittest.main()
