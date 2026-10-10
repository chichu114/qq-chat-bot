import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_openclaw_bridge.application.style_sync import (
    StyleSyncError,
    StyleSyncService,
    SyncOutcome,
)
from qq_openclaw_bridge.domain.community_style import (
    AuthorizedContent,
    DerivedSample,
    SourceCandidate,
    SourcePage,
    StyleFeatures,
    aggregate_style_profile,
)
from qq_openclaw_bridge.runtime.community_style_config import (
    AuthorizationSettings,
    CommunityStyleSettings,
    GroupBindingSettings,
    OllamaSettings,
    SourceSettings,
)
from qq_openclaw_bridge.runtime.ollama import OllamaError
from qq_openclaw_bridge.runtime.reddit_source import (
    RedditSource,
    SourcePausedError,
    SourceRateLimited,
    SourceTemporaryError,
)
from qq_openclaw_bridge.runtime.tieba_source import TiebaAccessPaused, TiebaMarkupError
from qq_openclaw_bridge.storage import WorkspaceStateStore


NOW = datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)
FEATURES = StyleFeatures(
    "short", "short", ("，",), "low", "mostly_chinese", 0.6,
    "low", "low", ("朋友",), ("不过",), ("personal_attack",),
)
SENSITIVE = "private authorized body with a secret"


def make_settings(*, platform="reddit", authorizations=None):
    if authorizations is None:
        authorizations = (
            AuthorizationSettings(
                "author", "Alice", "all_posts_until_revoke",
                "local-consent", NOW.isoformat(),
            ),
        )
    source = SourceSettings(
        "s", platform, "example", "p", 300, tuple(authorizations)
    )
    return CommunityStyleSettings(
        True,
        OllamaSettings("http://127.0.0.1:11434", "qwen3:8b", False),
        (source,),
        (GroupBindingSettings(123, "p", 5, 800),),
    )


class FakeSource:
    def __init__(self, items=(), *, next_cursor=(("after", "next"),), list_error=None,
                 fetch_error=None, on_list=None, on_fetch=None):
        self.items = tuple(items)
        self.next_cursor = tuple(next_cursor)
        self.list_error = list_error
        self.fetch_error = fetch_error
        self.on_list = on_list
        self.on_fetch = on_fetch
        self.list_calls = []
        self.fetch_calls = []

    def list_candidates(self, cursor, limit):
        self.list_calls.append((dict(cursor), limit))
        if self.on_list is not None:
            self.on_list()
        if self.list_error is not None:
            raise self.list_error
        return SourcePage(self.items, self.next_cursor)

    def fetch_content(self, candidate):
        self.fetch_calls.append(candidate.external_content_id)
        if self.on_fetch is not None:
            self.on_fetch()
        if self.fetch_error is not None:
            raise self.fetch_error
        return AuthorizedContent(candidate, SENSITIVE + " " + candidate.external_content_id)


class FakeAnalyzer:
    def __init__(self, *, fail_on_call=None, error=None):
        self.calls = []
        self.fail_on_call = fail_on_call
        self.error = error

    def analyze(self, text):
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        if self.fail_on_call == len(self.calls):
            raise RuntimeError(SENSITIVE)
        return FEATURES


class CountingRepository:
    def __init__(self, repository):
        self._repository = repository
        self.commit_calls = 0
        self.publish_calls = 0

    def __getattr__(self, name):
        return getattr(self._repository, name)

    def commit_batch(self, *args, **kwargs):
        self.commit_calls += 1
        return self._repository.commit_batch(*args, **kwargs)

    def commit_claimed_batch(self, *args, **kwargs):
        self.commit_calls += 1
        return self._repository.commit_claimed_batch(*args, **kwargs)

    def publish_profile(self, *args, **kwargs):
        self.publish_calls += 1
        return self._repository.publish_profile(*args, **kwargs)


class HostileKeyboardInterrupt(KeyboardInterrupt):
    pass


class HostileSystemExit(SystemExit):
    pass


def dirty_control(exception_type):
    try:
        raise exception_type(SENSITIVE)
    except BaseException as error:
        error.__context__ = RuntimeError(SENSITIVE)
        error.__cause__ = ValueError(SENSITIVE)
        return error


class StyleSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = WorkspaceStateStore(self.path)
        self.repo = self.store.community_style_repository
        self.settings = make_settings()
        self.repo.sync_configuration(self.settings, NOW)

    def candidate(self, index=1, *, author="Alice", platform="reddit"):
        return SourceCandidate(
            "s", platform, "example", "post-%d" % index, author,
            "2026-09-18T00:00:00Z", "https://example.test/%d" % index,
        )

    def service(self, source=None, analyzer=None, **kwargs):
        return StyleSyncService(
            repository=self.repo,
            sources={"s": source or FakeSource()},
            analyzer=analyzer or FakeAnalyzer(),
            jitter=lambda: 0.0,
            **kwargs,
        )

    def seed_profile(self):
        authorization = self.repo.active_authorizations("s", NOW)[0]
        samples = []
        for index in range(20, 25):
            candidate = self.candidate(index)
            sample = DerivedSample.from_content(
                AuthorizedContent(candidate, "seed-%d" % index), authorization, FEATURES
            )
            samples.append(sample)
        self.repo.commit_batch("s", {"after": "old"}, samples, NOW)
        profile = aggregate_style_profile("p", self.repo.active_samples("p"))
        return self.repo.publish_profile(profile, NOW)

    async def test_unauthorized_candidate_never_reaches_fetch_or_analyzer(self):
        source = FakeSource((self.candidate(author="Mallory"),))
        analyzer = FakeAnalyzer()
        outcome = await self.service(source, analyzer).sync_source("s", NOW)
        self.assertEqual(source.fetch_calls, [])
        self.assertEqual(analyzer.calls, [])
        self.assertEqual(outcome.unauthorized_count, 1)
        self.assertEqual(outcome.status, "synced")
        self.assertEqual(self.repo.source("s")["cursor"], {"after": "next"})

    async def test_expired_or_revoked_consent_is_checked_before_fetch(self):
        source = self.settings.sources[0]
        expired = replace(
            source.authorizations[0],
            expires_at=(NOW + timedelta(minutes=1)).isoformat(),
        )
        self.repo.sync_configuration(
            replace(self.settings, sources=(replace(source, authorizations=(expired,)),)), NOW
        )
        fake = FakeSource((self.candidate(),))
        outcome = await self.service(fake).sync_source("s", NOW + timedelta(minutes=1))
        self.assertEqual((outcome.unauthorized_count, fake.fetch_calls), (1, []))

    async def test_withdrawn_consent_is_checked_before_fetch(self):
        source_settings = self.settings.sources[0]
        self.repo.sync_configuration(
            replace(
                self.settings,
                sources=(replace(source_settings, authorizations=()),),
            ),
            NOW,
        )
        source = FakeSource((self.candidate(),))
        analyzer = FakeAnalyzer()
        outcome = await self.service(source, analyzer).sync_source("s", NOW)
        self.assertEqual(outcome.unauthorized_count, 1)
        self.assertEqual(source.fetch_calls, [])
        self.assertEqual(analyzer.calls, [])

    async def test_withdrawal_during_listing_prevents_fetch(self):
        source_settings = self.settings.sources[0]

        def revoke():
            self.repo.sync_configuration(
                replace(
                    self.settings,
                    sources=(replace(source_settings, authorizations=()),),
                ),
                NOW + timedelta(seconds=1),
            )

        source = FakeSource((self.candidate(),), on_list=revoke)
        analyzer = FakeAnalyzer()
        outcome = await self.service(source, analyzer).sync_source("s", NOW)
        self.assertEqual(outcome.unauthorized_count, 1)
        self.assertEqual(source.fetch_calls, [])
        self.assertEqual(analyzer.calls, [])

    async def test_withdrawal_during_fetch_prevents_analysis(self):
        source_settings = self.settings.sources[0]

        def revoke():
            self.repo.sync_configuration(
                replace(
                    self.settings,
                    sources=(replace(source_settings, authorizations=()),),
                ),
                NOW + timedelta(seconds=1),
            )

        source = FakeSource((self.candidate(),), on_fetch=revoke)
        analyzer = FakeAnalyzer()
        outcome = await self.service(source, analyzer).sync_source("s", NOW)
        self.assertEqual(source.fetch_calls, ["post-1"])
        self.assertEqual(analyzer.calls, [])
        self.assertEqual(outcome.unauthorized_count, 1)

    async def test_expiry_between_fetch_and_analysis_prevents_analysis(self):
        source_settings = self.settings.sources[0]
        expires = NOW + timedelta(seconds=10)
        expiring = replace(
            source_settings.authorizations[0], expires_at=expires.isoformat()
        )
        self.repo.sync_configuration(
            replace(
                self.settings,
                sources=(replace(source_settings, authorizations=(expiring,)),),
            ),
            NOW,
        )
        moments = iter((NOW + timedelta(seconds=5), expires))
        source = FakeSource((self.candidate(),))
        analyzer = FakeAnalyzer()
        service = self.service(source, analyzer, clock=lambda: next(moments))
        outcome = await service.sync_source("s", NOW)
        self.assertEqual(source.fetch_calls, ["post-1"])
        self.assertEqual(analyzer.calls, [])
        self.assertEqual(outcome.unauthorized_count, 1)

    async def test_mid_batch_analysis_failure_keeps_cursor_profile_and_redacts_body(self):
        old = self.seed_profile()
        run_at = NOW + timedelta(minutes=5)
        source = FakeSource((self.candidate(1), self.candidate(2)))
        analyzer = FakeAnalyzer(fail_on_call=2)
        with self.assertRaisesRegex(StyleSyncError, "analysis_failed") as caught:
            await self.service(source, analyzer).sync_source("s", run_at)
        self.assertNotIn(SENSITIVE, repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(self.repo.source("s")["cursor"], {"after": "old"})
        self.assertEqual(self.repo.source("s")["status"], "retry")
        self.assertEqual(self.repo.active_profile("p")["id"], old["id"])
        with sqlite3.connect(self.path) as connection:
            dump = " ".join(str(value) for row in connection.execute(
                "SELECT * FROM community_style_samples"
            ) for value in row)
        self.assertNotIn(SENSITIVE, dump)

    async def test_real_batch_transaction_failure_keeps_cursor_and_old_profile(self):
        old = self.seed_profile()
        run_at = NOW + timedelta(minutes=5)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_sync BEFORE INSERT ON community_style_samples "
                "WHEN NEW.external_content_id='post-2' "
                "BEGIN SELECT RAISE(ABORT, 'do not expose this database detail'); END"
            )
        source = FakeSource((self.candidate(1), self.candidate(2)))
        with self.assertRaisesRegex(StyleSyncError, "persistence_failed") as caught:
            await self.service(source).sync_source("s", run_at)
        self.assertNotIn("database detail", repr(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(self.repo.source("s")["cursor"], {"after": "old"})
        self.assertEqual(self.repo.active_profile("p")["id"], old["id"])

    async def test_five_authorized_samples_publish_profile_and_bound_outcome(self):
        source = FakeSource(tuple(self.candidate(index) for index in range(1, 6)))
        outcome = await self.service(source).sync_source("s", NOW)
        self.assertEqual(
            (outcome.status, outcome.candidate_count, outcome.derived_count),
            ("synced", 5, 5),
        )
        self.assertEqual(outcome.profile_version, 1)
        self.assertEqual(self.repo.active_profile("p")["sample_count"], 5)
        encoded = json.dumps(asdict(outcome))
        self.assertNotIn(SENSITIVE, encoded)

    async def test_each_authorized_candidate_fetches_and_analyzes_exactly_once(self):
        candidates = tuple(self.candidate(index) for index in range(1, 6))
        source = FakeSource(candidates)
        analyzer = FakeAnalyzer()
        repository = CountingRepository(self.repo)
        service = StyleSyncService(
            repository, {"s": source}, analyzer, jitter=lambda: 0.0
        )

        outcome = await service.sync_source("s", NOW)

        self.assertEqual(outcome.derived_count, 5)
        self.assertEqual(source.list_calls, [({}, 100)])
        self.assertEqual(source.fetch_calls, [candidate.external_content_id for candidate in candidates])
        self.assertEqual(len(analyzer.calls), 5)
        self.assertEqual(repository.commit_calls, 1)
        self.assertEqual(repository.publish_calls, 1)

    async def test_reddit_rate_limit_and_temporary_backoff_are_persisted(self):
        limited = FakeSource(list_error=SourceRateLimited(45))
        outcome = await self.service(limited).sync_source("s", NOW)
        self.assertEqual((outcome.status, outcome.safe_error_class), ("retry", "rate_limited"))
        self.assertEqual(
            datetime.fromisoformat(self.repo.source("s")["next_attempt_at"]),
            NOW + timedelta(seconds=45),
        )

        first_at = NOW + timedelta(seconds=45)
        temporary = FakeSource(list_error=SourceTemporaryError("remote secret"))
        outcome = await self.service(temporary).sync_source("s", first_at)
        self.assertEqual((outcome.status, outcome.safe_error_class), ("retry", "temporary"))
        self.assertEqual(
            datetime.fromisoformat(self.repo.source("s")["next_attempt_at"]),
            first_at + timedelta(seconds=30),
        )
        second_at = first_at + timedelta(seconds=30)
        outcome = await self.service(temporary).sync_source("s", second_at)
        self.assertEqual((outcome.status, outcome.safe_error_class), ("retry", "temporary"))
        self.assertEqual(
            datetime.fromisoformat(self.repo.source("s")["next_attempt_at"]),
            second_at + timedelta(seconds=60),
        )

    async def test_access_auth_schema_errors_pause_with_only_fixed_classes(self):
        cases = (
            (SourcePausedError("reddit_auth"), "reddit_auth"),
            (TiebaAccessPaused("captcha"), "captcha"),
            (TiebaMarkupError("invalid_markup"), "invalid_markup"),
        )
        for index, (error, expected) in enumerate(cases):
            with self.subTest(error=expected):
                path = Path(self.temp.name) / ("pause-%d.sqlite3" % index)
                repo = WorkspaceStateStore(path).community_style_repository
                repo.sync_configuration(self.settings, NOW)
                service = StyleSyncService(
                    repo, {"s": FakeSource(list_error=error)}, FakeAnalyzer(),
                    jitter=lambda: 0.0,
                )
                outcome = await service.sync_source("s", NOW)
                self.assertEqual((outcome.status, outcome.safe_error_class), ("paused", expected))
                self.assertEqual(repo.source("s")["status"], "paused")

    async def test_ollama_failure_retries_without_cursor_advance(self):
        analyzer = FakeAnalyzer(error=OllamaError("timeout", transient=True))
        with self.assertRaisesRegex(StyleSyncError, "ollama_timeout"):
            await self.service(FakeSource((self.candidate(),)), analyzer).sync_source("s", NOW)
        state = self.repo.source("s")
        self.assertEqual((state["cursor"], state["status"]), ({}, "retry"))

    async def test_tieba_tuple_cursor_is_converted_to_repository_mapping(self):
        self.settings = make_settings(platform="tieba")
        self.repo.sync_configuration(self.settings, NOW)
        source = FakeSource(
            (self.candidate(platform="tieba"),),
            next_cursor=(("pn", "50"), ("page", "2")),
        )
        outcome = await self.service(source).sync_source("s", NOW)
        self.assertEqual(outcome.derived_count, 1)
        self.assertEqual(self.repo.source("s")["cursor"], {"pn": "50", "page": "2"})

    async def test_run_due_once_claims_at_most_one_source(self):
        source = replace(self.settings.sources[0], id="a", profile="a-profile")
        settings = replace(self.settings, sources=self.settings.sources + (source,))
        self.repo.sync_configuration(settings, NOW)
        first, second = FakeSource(), FakeSource()
        service = StyleSyncService(
            self.repo, {"a": first, "s": second}, FakeAnalyzer(), jitter=lambda: 0.0
        )
        outcome = await service.run_due_once(NOW)
        self.assertEqual(outcome.source_id, "a")
        self.assertEqual(len(first.list_calls) + len(second.list_calls), 1)

    async def test_recover_interrupted_does_not_call_sources(self):
        source = FakeSource()
        self.assertIsNotNone(self.repo.claim_source("s", NOW))
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE community_style_sources SET last_attempt_at=? WHERE id='s'",
                ((NOW - timedelta(minutes=10)).isoformat(),),
            )
        recovered = await self.service(source).recover_interrupted(NOW)
        self.assertEqual(recovered, 1)
        self.assertEqual(source.list_calls, [])
        self.assertEqual(self.repo.source("s")["safe_error_class"], "interrupted")

    async def test_one_service_never_overlaps_source_runs(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingService(StyleSyncService):
            async def _sync_claimed(self, source_state, now):
                entered.set()
                await release.wait()
                return SyncOutcome("synced", source_state["id"])

        service = BlockingService(
            self.repo, {"s": FakeSource()}, FakeAnalyzer(), jitter=lambda: 0.0
        )
        first = asyncio.create_task(service.sync_source("s", NOW))
        await entered.wait()
        second = await service.sync_source("s", NOW)
        self.assertEqual(second.status, "busy")
        release.set()
        await first

    async def test_loop_with_pre_set_stop_takes_no_work(self):
        source = FakeSource((self.candidate(),))
        stop = asyncio.Event()
        stop.set()
        await self.service(source, idle_poll_seconds=30).loop(stop)
        self.assertEqual(source.list_calls, [])

    async def test_cancellation_marks_claimed_source_retry_without_cursor_advance(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingSource(FakeSource):
            def list_candidates(self, cursor, limit):
                entered.set()
                release.wait(2)
                return super().list_candidates(cursor, limit)

        service = self.service(BlockingSource((self.candidate(),)))
        task = asyncio.create_task(service.sync_source("s", NOW))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = self.repo.source("s")
        self.assertEqual((state["status"], state["cursor"]), ("retry", {}))
        self.assertEqual(state["safe_error_class"], "interrupted")

    async def test_unknown_source_exception_is_sanitized_before_outcome_and_state(self):
        class HostileError(RuntimeError):
            pass

        source = FakeSource(list_error=HostileError(SENSITIVE))
        outcome = await self.service(source).sync_source("s", NOW)
        self.assertEqual((outcome.status, outcome.safe_error_class), ("paused", "source_schema"))
        self.assertNotIn(SENSITIVE, repr(outcome))
        self.assertNotIn(SENSITIVE, json.dumps(self.repo.status_snapshot()))

    async def test_hostile_baseexception_subclasses_from_list_and_fetch_are_sanitized(self):
        for phase, source in (
            ("list", FakeSource(list_error=HostileKeyboardInterrupt(SENSITIVE))),
            ("fetch", FakeSource(
                (self.candidate(),), fetch_error=HostileSystemExit(SENSITIVE)
            )),
        ):
            with self.subTest(phase=phase):
                path = Path(self.temp.name) / ("hostile-%s.sqlite3" % phase)
                repo = WorkspaceStateStore(path).community_style_repository
                repo.sync_configuration(self.settings, NOW)
                service = StyleSyncService(
                    repo, {"s": source}, FakeAnalyzer(), jitter=lambda: 0.0
                )
                outcome = await service.sync_source("s", NOW)
                self.assertEqual(
                    (outcome.status, outcome.safe_error_class),
                    ("paused", "source_schema"),
                )
                self.assertNotIn(SENSITIVE, repr(outcome))
                self.assertNotIn(SENSITIVE, json.dumps(repo.status_snapshot()))

    async def test_hostile_baseexception_subclass_from_source_open_is_sanitized(self):
        def opener(*args, **kwargs):
            raise HostileSystemExit(SENSITIVE)

        source = RedditSource(
            self.settings.sources[0], token_provider=lambda: "token", opener=opener
        )
        outcome = await self.service(source).sync_source("s", NOW)
        self.assertEqual(
            (outcome.status, outcome.safe_error_class), ("paused", "source_schema")
        )
        self.assertNotIn(SENSITIVE, repr(outcome))
        self.assertNotIn(SENSITIVE, json.dumps(self.repo.status_snapshot()))

    async def test_hostile_baseexception_subclass_from_analyzer_is_safe_retry(self):
        analyzer = FakeAnalyzer(error=HostileKeyboardInterrupt(SENSITIVE))
        with self.assertRaisesRegex(StyleSyncError, "analysis_failed") as caught:
            await self.service(FakeSource((self.candidate(),)), analyzer).sync_source("s", NOW)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(SENSITIVE, repr(caught.exception))
        state = self.repo.source("s")
        self.assertEqual((state["status"], state["safe_error_class"]), ("retry", "analysis_failed"))

    async def test_hostile_baseexception_subclass_from_repository_commit_is_safe_retry(self):
        class HostileCommitRepository(CountingRepository):
            def commit_claimed_batch(self, *args, **kwargs):
                raise HostileSystemExit(SENSITIVE)

        repository = HostileCommitRepository(self.repo)
        service = StyleSyncService(
            repository, {"s": FakeSource()}, FakeAnalyzer(), jitter=lambda: 0.0
        )
        with self.assertRaisesRegex(StyleSyncError, "persistence_failed") as caught:
            await service.sync_source("s", NOW)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(SENSITIVE, repr(caught.exception))
        state = self.repo.source("s")
        self.assertEqual((state["status"], state["safe_error_class"]), ("retry", "persistence_failed"))

    async def test_exact_control_exceptions_are_rebuilt_without_external_payload(self):
        for phase, exception_type in (
            ("open", SystemExit),
            ("list", KeyboardInterrupt),
            ("fetch", SystemExit),
            ("analyze", KeyboardInterrupt),
            ("commit", SystemExit),
        ):
            with self.subTest(phase=phase, exception_type=exception_type.__name__):
                path = Path(self.temp.name) / ("exact-control-%s.sqlite3" % phase)
                repo = WorkspaceStateStore(path).community_style_repository
                repo.sync_configuration(self.settings, NOW)
                original = dirty_control(exception_type)
                analyzer = FakeAnalyzer(error=original if phase == "analyze" else None)
                if phase == "open":
                    def opener(*args, **kwargs):
                        raise original

                    source = RedditSource(
                        self.settings.sources[0],
                        token_provider=lambda: "token",
                        opener=opener,
                    )
                elif phase == "list":
                    source = FakeSource(list_error=original)
                elif phase == "fetch":
                    source = FakeSource((self.candidate(),), fetch_error=original)
                else:
                    source = FakeSource((self.candidate(),))

                if phase == "commit":
                    class ControlCommitRepository(CountingRepository):
                        def commit_claimed_batch(self, *args, **kwargs):
                            raise original

                    repository = ControlCommitRepository(repo)
                else:
                    repository = repo
                service = StyleSyncService(
                    repository, {"s": source}, analyzer, jitter=lambda: 0.0
                )
                caught = None
                try:
                    await service.sync_source("s", NOW)
                except BaseException as error:
                    caught = error
                self.assertIs(type(caught), exception_type)
                self.assertIsNot(caught, original)
                self.assertEqual(caught.args, ())
                self.assertIsNone(caught.__context__)
                self.assertIsNone(caught.__cause__)
                self.assertNotIn(SENSITIVE, "".join(
                    __import__("traceback").format_exception(
                        type(caught), caught, caught.__traceback__
                    )
                ))
                self.assertEqual(original.args, ())
                self.assertIsNone(original.__context__)
                self.assertIsNone(original.__cause__)
                self.assertIsNone(original.__traceback__)
                state = repo.source("s")
                self.assertEqual(
                    (state["status"], state["safe_error_class"]),
                    ("retry", "interrupted"),
                )

    async def test_cancelled_commit_waits_for_worker_before_releasing_claim(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingCommitRepository(CountingRepository):
            def commit_claimed_batch(self, *args, **kwargs):
                entered.set()
                release.wait(2)
                return self._repository.commit_claimed_batch(*args, **kwargs)

        repository = BlockingCommitRepository(self.repo)
        service = StyleSyncService(
            repository, {"s": FakeSource()}, FakeAnalyzer(), jitter=lambda: 0.0
        )
        task = asyncio.create_task(service.sync_source("s", NOW))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        self.assertEqual(self.repo.source("s")["status"], "running")
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = self.repo.source("s")
        self.assertEqual((state["status"], state["cursor"]), ("idle", {"after": "next"}))

    async def test_recovered_and_reclaimed_generation_stops_before_fetch(self):
        replacement = {}
        second = WorkspaceStateStore(self.path).community_style_repository

        def reclaim():
            recovered_at = NOW + timedelta(minutes=10)
            replacement["recovered"] = second.recover_interrupted_sources(
                recovered_at, recovered_at - timedelta(minutes=5)
            )
            replacement["claim"] = second.claim_source("s", recovered_at)

        source = FakeSource((self.candidate(),), on_list=reclaim)
        analyzer = FakeAnalyzer()
        with self.assertRaisesRegex(StyleSyncError, "persistence_failed") as caught:
            await self.service(source, analyzer).sync_source("s", NOW)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(replacement["recovered"], 1)
        self.assertEqual(source.fetch_calls, [])
        self.assertEqual(analyzer.calls, [])
        new_claim = replacement["claim"]
        state = second.source("s")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["last_attempt_at"], new_claim["claim_token"])
        self.assertEqual(state["cursor"], {})
        self.assertEqual(second.active_samples("p"), ())

    async def test_recovered_and_reclaimed_after_fetch_stops_before_analysis(self):
        replacement = {}
        second = WorkspaceStateStore(self.path).community_style_repository

        def reclaim():
            recovered_at = NOW + timedelta(minutes=10)
            replacement["recovered"] = second.recover_interrupted_sources(
                recovered_at, recovered_at - timedelta(minutes=5)
            )
            replacement["claim"] = second.claim_source("s", recovered_at)

        source = FakeSource((self.candidate(),), on_fetch=reclaim)
        analyzer = FakeAnalyzer()
        with self.assertRaisesRegex(StyleSyncError, "persistence_failed"):
            await self.service(source, analyzer).sync_source("s", NOW)
        self.assertEqual(replacement["recovered"], 1)
        self.assertEqual(source.fetch_calls, ["post-1"])
        self.assertEqual(analyzer.calls, [])
        new_claim = replacement["claim"]
        state = second.source("s")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["last_attempt_at"], new_claim["claim_token"])
        self.assertEqual(state["cursor"], {})
        self.assertEqual(second.active_samples("p"), ())

    async def test_public_values_and_time_input_are_strict(self):
        with self.assertRaises(ValueError):
            await self.service().sync_source("s", NOW.replace(tzinfo=None))
        for invalid in (
            dict(status="unknown", source_id=None),
            dict(status="idle", source_id="s\n" + SENSITIVE),
            dict(status="idle", source_id=None, candidate_count=-1),
            dict(status="retry", source_id="s", safe_error_class="secret: body"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SyncOutcome(**invalid)


if __name__ == "__main__":
    unittest.main()
