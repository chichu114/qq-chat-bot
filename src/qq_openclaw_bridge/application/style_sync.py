"""Authorization-first orchestration for derived community style samples.

Candidate discovery is metadata-only.  Content is fetched only after a current
grant matches, remains local to one loop iteration, and is never included in
public outcomes or persistence calls.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
import unicodedata
from typing import Optional

from qq_openclaw_bridge.domain.community_style import (
    AuthorizedContent,
    DerivedSample,
    SourcePage,
    aggregate_style_profile,
    authorization_matches,
)
from qq_openclaw_bridge.runtime.ollama import OllamaError
from qq_openclaw_bridge.runtime.reddit_source import (
    SourcePausedError,
    SourceRateLimited,
    SourceTemporaryError,
)
from qq_openclaw_bridge.runtime.tieba_source import TiebaAccessPaused, TiebaMarkupError


_OUTCOME_STATUSES = frozenset(("idle", "busy", "synced", "retry", "paused"))
_SAFE_ERROR_CLASSES = frozenset((
    "analysis_failed", "persistence_failed", "profile_failed", "interrupted",
    "rate_limited", "temporary", "source_unavailable", "source_schema",
    "reddit_auth", "reddit_candidate", "reddit_cursor", "reddit_http",
    "reddit_redirect", "reddit_request", "reddit_response_too_large",
    "reddit_schema", "robots_disallow", "robots_unavailable", "captcha",
    "login_required", "http_403", "http_429", "http_error", "redirect",
    "network", "invalid_request", "invalid_cursor", "invalid_candidate",
    "response_exceeded", "invalid_markup", "identity_mismatch",
    "ollama_timeout", "ollama_connection", "ollama_http_status",
    "ollama_model_missing", "ollama_invalid_response",
    "ollama_response_too_large", "ollama_redirect_rejected",
))
_REDDIT_PAUSE_CLASSES = frozenset((
    "reddit_auth", "reddit_candidate", "reddit_cursor", "reddit_http",
    "reddit_redirect", "reddit_request", "reddit_response_too_large",
    "reddit_schema",
))
_TIEBA_ACCESS_CLASSES = frozenset((
    "robots_disallow", "robots_unavailable", "captcha", "login_required",
    "http_403", "http_429", "http_error", "redirect", "network",
))
_TIEBA_MARKUP_CLASSES = frozenset((
    "invalid_request", "invalid_cursor", "invalid_candidate",
    "response_exceeded", "invalid_markup", "identity_mismatch",
))
_MAX_COUNT = 10_000
_MAX_RETRY_SECONDS = 24 * 60 * 60
_MAX_TEMPORARY_BACKOFF_SECONDS = 60 * 60
_BASE_TEMPORARY_BACKOFF_SECONDS = 30
_MAX_SOURCE_ID_CHARS = 128


def _capture_plain_call(function, arguments):
    """Keep arbitrary BaseException values out of executor futures."""
    try:
        return "ok", function(*arguments)
    except BaseException as error:
        if type(error) in (KeyboardInterrupt, SystemExit):
            return "control", _sanitize_control_exception(error)
        return "failure", None


def _sanitize_control_exception(error):
    """Destroy external payload and return a fresh exact control exception."""
    exception_type = type(error)
    if exception_type not in (KeyboardInterrupt, SystemExit):
        raise ValueError("invalid_control_exception")
    # Exact built-in instances have writable exception metadata.  Clear the
    # external object too, so executor futures and local references cannot
    # retain source payload or its traceback after this boundary.
    error.args = ()
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None
    if exception_type is KeyboardInterrupt:
        return KeyboardInterrupt()
    return SystemExit()


class StyleSyncError(RuntimeError):
    """A bounded orchestration failure which carries no source content."""

    def __init__(self, error_class: str) -> None:
        if error_class not in _SAFE_ERROR_CLASSES:
            error_class = "persistence_failed"
        self.error_class = error_class
        super().__init__(error_class)


def _safe_token(value, maximum, field):
    if not isinstance(value, str):
        raise ValueError("invalid_%s" % field)
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized.strip() or normalized != normalized.strip()
        or len(normalized) > maximum
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        raise ValueError("invalid_%s" % field)
    return normalized


def _safe_now(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid_time")
    try:
        offset = value.utcoffset()
        earlier = value.replace(fold=0).utcoffset()
        later = value.replace(fold=1).utcoffset()
    except (OverflowError, ValueError):
        raise ValueError("invalid_time") from None
    if offset is None or earlier is None or earlier != later:
        raise ValueError("invalid_time")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError("invalid_time") from None


def _safe_claim_token(value):
    if type(value) is not str or len(value) > 64 or "\x00" in value:
        raise ValueError("invalid_claim_token")
    try:
        parsed = datetime.fromisoformat(value)
        canonical = _safe_now(parsed).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_claim_token") from None
    if canonical != value:
        raise ValueError("invalid_claim_token")
    return value


@dataclass(frozen=True)
class SyncOutcome:
    status: str
    source_id: Optional[str]
    candidate_count: int = 0
    unauthorized_count: int = 0
    derived_count: int = 0
    profile_version: Optional[int] = None
    safe_error_class: Optional[str] = None

    def __post_init__(self):
        if self.status not in _OUTCOME_STATUSES:
            raise ValueError("invalid_status")
        if self.source_id is not None:
            object.__setattr__(
                self, "source_id", _safe_token(self.source_id, _MAX_SOURCE_ID_CHARS, "source_id")
            )
        for field in ("candidate_count", "unauthorized_count", "derived_count"):
            value = getattr(self, field)
            if type(value) is not int or not 0 <= value <= _MAX_COUNT:
                raise ValueError("invalid_count")
        if self.unauthorized_count > self.candidate_count or self.derived_count > self.candidate_count:
            raise ValueError("invalid_count")
        if self.profile_version is not None and (
            type(self.profile_version) is not int or self.profile_version < 1
        ):
            raise ValueError("invalid_profile_version")
        if self.safe_error_class is not None and self.safe_error_class not in _SAFE_ERROR_CLASSES:
            raise ValueError("invalid_error_class")
        if self.status in ("retry", "paused") and self.safe_error_class is None:
            raise ValueError("missing_error_class")
        if self.status not in ("retry", "paused") and self.safe_error_class is not None:
            raise ValueError("unexpected_error_class")

    @classmethod
    def idle(cls):
        return cls(status="idle", source_id=None)


class StyleSyncService:
    def __init__(
        self,
        repository,
        sources,
        analyzer,
        *,
        jitter=None,
        clock=None,
        idle_poll_seconds=30.0,
        recovery_after_seconds=300,
    ):
        required_repository = (
            "active_samples", "claim_source", "claimed_authorizations", "commit_claimed_batch",
            "due_sources", "pause_source", "publish_profile",
            "recover_interrupted_sources", "retry_source",
        )
        if any(not callable(getattr(repository, name, None)) for name in required_repository):
            raise ValueError("invalid_repository")
        if not isinstance(sources, Mapping) or len(sources) > 100:
            raise ValueError("invalid_sources")
        copied_sources = {}
        for source_id, source in tuple(sources.items()):
            source_id = _safe_token(source_id, _MAX_SOURCE_ID_CHARS, "source_id")
            if (
                source_id in copied_sources
                or not callable(getattr(source, "list_candidates", None))
                or not callable(getattr(source, "fetch_content", None))
            ):
                raise ValueError("invalid_sources")
            copied_sources[source_id] = source
        if not callable(getattr(analyzer, "analyze", None)):
            raise ValueError("invalid_analyzer")
        if jitter is None:
            import random
            jitter = random.random
        if clock is None:
            clock = lambda: datetime.now(timezone.utc)
        if not callable(jitter) or not callable(clock):
            raise ValueError("invalid_scheduler")
        if type(idle_poll_seconds) not in (int, float):
            raise ValueError("invalid_idle_poll")
        idle_poll = float(idle_poll_seconds)
        if not math.isfinite(idle_poll) or not 0.0 < idle_poll <= 30.0:
            raise ValueError("invalid_idle_poll")
        if type(recovery_after_seconds) is not int or not 1 <= recovery_after_seconds <= 86400:
            raise ValueError("invalid_recovery_window")
        self.repository = repository
        self.sources = copied_sources
        self.analyzer = analyzer
        self._jitter = jitter
        self._clock = clock
        self._idle_poll_seconds = idle_poll
        self._recovery_after = timedelta(seconds=recovery_after_seconds)
        # Style analysis is deliberately serialized.  It cannot consume every
        # inference slot while group reply generation is active.
        self._run_lock = asyncio.Lock()

    def _time(self, value=None):
        if value is None:
            control = None
            failed = False
            try:
                value = self._clock()
            except BaseException as error:
                if type(error) in (KeyboardInterrupt, SystemExit):
                    control = _sanitize_control_exception(error)
                else:
                    failed = True
            if control is not None:
                raise control from None
            if failed:
                raise ValueError("invalid_time")
        return _safe_now(value)

    async def run_due_once(self, now=None):
        current = self._time(now)
        failed = False
        control = None
        try:
            due = await asyncio.to_thread(self.repository.due_sources, current, 1)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit):
                control = _sanitize_control_exception(error)
            else:
                failed = True
        if control is not None:
            raise control from None
        if failed:
            raise StyleSyncError("persistence_failed")
        if not due:
            return SyncOutcome.idle()
        try:
            source_id = due[0]["id"]
        except (KeyError, TypeError, IndexError):
            raise StyleSyncError("persistence_failed") from None
        return await self.sync_source(source_id, current)

    async def sync_source(self, source_id, now=None):
        source_id = _safe_token(source_id, _MAX_SOURCE_ID_CHARS, "source_id")
        current = self._time(now)
        if self._run_lock.locked():
            return SyncOutcome("busy", source_id)
        async with self._run_lock:
            claim_failed = False
            control = None
            try:
                source_state = await asyncio.to_thread(
                    self.repository.claim_source, source_id, current
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if type(error) in (KeyboardInterrupt, SystemExit):
                    control = _sanitize_control_exception(error)
                else:
                    claim_failed = True
            if control is not None:
                raise control from None
            if claim_failed:
                raise StyleSyncError("persistence_failed")
            if source_state is None:
                return SyncOutcome("idle", source_id)
            try:
                claim_token = _safe_claim_token(source_state["claim_token"])
            except (KeyError, TypeError, ValueError):
                raise StyleSyncError("persistence_failed") from None
            try:
                return await self._sync_claimed(source_state, current)
            except asyncio.CancelledError:
                await self._retry_after_cancellation(source_id, claim_token, current)
                raise
            except StyleSyncError:
                raise
            except BaseException as error:
                if type(error) in (KeyboardInterrupt, SystemExit):
                    control = _sanitize_control_exception(error)
                    unexpected_failure = False
                else:
                    control = None
                    unexpected_failure = True
            else:
                control = None
                unexpected_failure = False
            if control is not None:
                await self._best_effort_interrupted(source_id, claim_token, current)
                raise control from None
            if unexpected_failure:
                await self._record_retry(
                    source_id, claim_token, "persistence_failed", current, 30
                )
                raise StyleSyncError("persistence_failed") from None

    async def _sync_claimed(self, source_state, now):
        try:
            source_id = _safe_token(source_state["id"], _MAX_SOURCE_ID_CHARS, "source_id")
            profile_key = _safe_token(source_state["profile_key"], 128, "profile_key")
            claim_token = _safe_claim_token(source_state["claim_token"])
            cursor = source_state["cursor"]
            limit = source_state["max_items_per_sync"]
            if not isinstance(cursor, Mapping) or type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise StyleSyncError("persistence_failed") from None
        source = self.sources.get(source_id)
        if source is None:
            await self._record_pause(source_id, claim_token, "source_unavailable", now)
            return SyncOutcome("paused", source_id, safe_error_class="source_unavailable")
        control = None
        try:
            page = await asyncio.to_thread(source.list_candidates, dict(cursor), limit)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit):
                control = _sanitize_control_exception(error)
                source_failure = None
            else:
                source_failure = self._classify_source_error(error, source_state)
        else:
            source_failure = None
        if control is not None:
            raise control from None
        if source_failure is not None:
            return await self._handle_source_error(
                source_id, claim_token, source_failure, now
            )
        if not isinstance(page, SourcePage):
            await self._record_pause(source_id, claim_token, "source_schema", now)
            return SyncOutcome("paused", source_id, safe_error_class="source_schema")

        derived = []
        unauthorized = 0
        for candidate in page.items:
            authorization_now = self._time()
            authorization = await self._current_authorization(
                source_id, claim_token, candidate, authorization_now
            )
            if authorization is None:
                unauthorized += 1
                continue
            control = None
            try:
                content = await asyncio.to_thread(source.fetch_content, candidate)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if type(error) in (KeyboardInterrupt, SystemExit):
                    control = _sanitize_control_exception(error)
                    source_failure = None
                else:
                    source_failure = self._classify_source_error(error, source_state)
            else:
                source_failure = None
            if control is not None:
                raise control from None
            if source_failure is not None:
                return await self._handle_source_error(
                    source_id, claim_token, source_failure, now,
                    candidate_count=len(page.items), unauthorized_count=unauthorized,
                )
            if not isinstance(content, AuthorizedContent) or content.candidate != candidate:
                content = None
                await self._record_pause(
                    source_id, claim_token, "source_schema", now
                )
                return SyncOutcome(
                    "paused", source_id, len(page.items), unauthorized, len(derived),
                    safe_error_class="source_schema",
                )
            recheck_now = self._time()
            still_authorized = await self._current_authorization(
                source_id,
                claim_token,
                candidate,
                recheck_now,
                authorization.authorization_id,
            )
            if still_authorized is None:
                content = None
                unauthorized += 1
                continue
            analysis_failure = None
            control = None
            try:
                features = await asyncio.to_thread(self.analyzer.analyze, content.text)
                sample = DerivedSample.from_content(content, still_authorized, features)
            except asyncio.CancelledError:
                content = None
                raise
            except OllamaError as error:
                error_class = "ollama_" + error.error_class
                if error_class not in _SAFE_ERROR_CLASSES:
                    error_class = "analysis_failed"
                analysis_failure = (error_class, bool(error.transient))
            except BaseException as error:
                if type(error) in (KeyboardInterrupt, SystemExit):
                    control = _sanitize_control_exception(error)
                else:
                    analysis_failure = ("analysis_failed", True)
            if control is not None:
                content = None
                raise control from None
            if analysis_failure is not None:
                content = None
                error_class, retryable = analysis_failure
                if retryable:
                    await self._record_retry(
                        source_id, claim_token, error_class, now,
                        self._temporary_delay(source_state),
                    )
                else:
                    await self._record_pause(
                        source_id, claim_token, error_class, now
                    )
                raise StyleSyncError(error_class) from None
            content = None
            derived.append(sample)

        commit_failed = False
        control = None
        committed = False
        try:
            next_cursor = dict(page.next_cursor)
            committed = await self._commit_with_cancellation_barrier(
                source_id, claim_token, next_cursor, tuple(derived), now
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit):
                control = _sanitize_control_exception(error)
            else:
                commit_failed = True
        if control is not None:
            raise control from None
        if commit_failed:
            await self._record_retry(
                source_id, claim_token, "persistence_failed", now, 30
            )
            raise StyleSyncError("persistence_failed")
        if committed is None:
            raise StyleSyncError("persistence_failed") from None

        profile_version = None
        profile_failed = False
        control = None
        try:
            samples = await asyncio.to_thread(self.repository.active_samples, profile_key)
            profile = aggregate_style_profile(profile_key, samples, minimum_samples=5)
            if profile is not None:
                published = await asyncio.to_thread(self.repository.publish_profile, profile, now)
                profile_version = published["version"]
                if type(profile_version) is not int or profile_version < 1:
                    raise ValueError
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit):
                control = _sanitize_control_exception(error)
            else:
                profile_failed = True
        if control is not None:
            raise control from None
        if profile_failed:
            # The batch is already durable and the previous profile remains active.
            raise StyleSyncError("profile_failed")
        return SyncOutcome(
            "synced", source_id, len(page.items), unauthorized, len(derived),
            profile_version=profile_version,
        )

    async def _current_authorization(
        self, source_id, claim_token, candidate, now, authorization_id=None,
    ):
        failed = False
        control = None
        try:
            authorizations = await asyncio.to_thread(
                self.repository.claimed_authorizations,
                source_id,
                claim_token,
                now,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit):
                control = _sanitize_control_exception(error)
            else:
                failed = True
        if control is not None:
            raise control from None
        if failed:
            await self._record_retry(
                source_id, claim_token, "persistence_failed", now, 30
            )
            raise StyleSyncError("persistence_failed")
        if authorizations is None:
            raise StyleSyncError("persistence_failed") from None
        for authorization in authorizations:
            if (
                (authorization_id is None or authorization.authorization_id == authorization_id)
                and authorization_matches(candidate, authorization, now)
            ):
                return authorization
        return None

    async def _commit_with_cancellation_barrier(
        self, source_id, claim_token, next_cursor, samples, now,
    ):
        task = asyncio.create_task(asyncio.to_thread(
            _capture_plain_call,
            self.repository.commit_claimed_batch,
            (source_id, claim_token, next_cursor, samples, now),
        ))
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not mark/reclaim this generation until its synchronous SQLite
            # worker has definitely stopped; otherwise a late worker could write
            # a cursor after a newer generation starts.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if task.done():
                try:
                    task.result()
                except BaseException:
                    pass
            raise
        kind, value = outcome
        if kind == "control":
            raise value from None
        if kind == "failure":
            raise StyleSyncError("persistence_failed") from None
        return value

    def _classify_source_error(self, error, source_state):
        if isinstance(error, SourceRateLimited):
            retry_after = getattr(error, "retry_after", 60)
            if type(error) is not SourceRateLimited or type(retry_after) is not int or not 1 <= retry_after <= _MAX_RETRY_SECONDS:
                retry_after = 60
            return "retry", "rate_limited", retry_after
        if isinstance(error, SourceTemporaryError):
            return "retry", "temporary", self._temporary_delay(source_state)
        if isinstance(error, SourcePausedError):
            reason = self._exception_reason(error, _REDDIT_PAUSE_CLASSES, "reddit_schema")
        elif isinstance(error, TiebaAccessPaused):
            reason = self._exception_reason(error, _TIEBA_ACCESS_CLASSES, "network")
        elif isinstance(error, TiebaMarkupError):
            reason = self._exception_reason(error, _TIEBA_MARKUP_CLASSES, "invalid_markup")
        else:
            reason = "source_schema"
        return "paused", reason, 0

    async def _handle_source_error(
        self, source_id, claim_token, failure, now, *,
        candidate_count=0, unauthorized_count=0,
    ):
        status, reason, retry_after = failure
        if status == "retry":
            await self._record_retry(
                source_id, claim_token, reason, now, retry_after
            )
            return SyncOutcome(
                "retry", source_id, candidate_count, unauthorized_count, 0,
                safe_error_class=reason,
            )
        await self._record_pause(source_id, claim_token, reason, now)
        return SyncOutcome(
            "paused", source_id, candidate_count, unauthorized_count, 0,
            safe_error_class=reason,
        )

    @staticmethod
    def _exception_reason(error, allowed, fallback):
        args = getattr(error, "args", ())
        if type(args) is tuple and len(args) == 1 and type(args[0]) is str and args[0] in allowed:
            return args[0]
        return fallback

    def _temporary_delay(self, source_state):
        previous = source_state.get("previous_retry_seconds")
        if type(previous) not in (int, float) or not math.isfinite(float(previous)) or previous < 0:
            base = _BASE_TEMPORARY_BACKOFF_SECONDS
        else:
            base = max(_BASE_TEMPORARY_BACKOFF_SECONDS, int(math.ceil(float(previous) * 2.0)))
        base = min(base, _MAX_TEMPORARY_BACKOFF_SECONDS)
        try:
            jitter = self._jitter()
        except Exception:
            jitter = 0.0
        if type(jitter) not in (int, float) or not math.isfinite(float(jitter)) or not 0.0 <= float(jitter) <= 1.0:
            jitter = 0.0
        return min(
            _MAX_TEMPORARY_BACKOFF_SECONDS,
            max(1, int(math.ceil(base * (1.0 + float(jitter) * 0.25)))),
        )

    async def _record_retry(self, source_id, claim_token, error_class, now, delay_seconds):
        retry_at = now + timedelta(seconds=max(0, min(int(delay_seconds), _MAX_RETRY_SECONDS)))
        failed = False
        control = None
        try:
            changed = await asyncio.to_thread(
                self.repository.retry_source,
                source_id,
                claim_token,
                error_class,
                retry_at,
                now,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit):
                control = _sanitize_control_exception(error)
            else:
                failed = True
        if control is not None:
            raise control from None
        if failed:
            raise StyleSyncError("persistence_failed")
        if changed is not True:
            raise StyleSyncError("persistence_failed") from None

    async def _record_pause(self, source_id, claim_token, error_class, now):
        failed = False
        control = None
        try:
            changed = await asyncio.to_thread(
                self.repository.pause_source,
                source_id,
                claim_token,
                error_class,
                now,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit):
                control = _sanitize_control_exception(error)
            else:
                failed = True
        if control is not None:
            raise control from None
        if failed:
            raise StyleSyncError("persistence_failed")
        if changed is not True:
            raise StyleSyncError("persistence_failed") from None

    async def _retry_after_cancellation(self, source_id, claim_token, now):
        task = asyncio.create_task(asyncio.to_thread(
            _capture_plain_call,
            self.repository.retry_source,
            (
                source_id,
                claim_token,
                "interrupted",
                now + timedelta(seconds=_BASE_TEMPORARY_BACKOFF_SECONDS),
                now,
            ),
        ))
        try:
            await asyncio.shield(task)
        except BaseException:
            # Cancellation must propagate; startup recovery is the fallback if
            # persistence itself is unavailable during shutdown.
            pass

    async def _best_effort_interrupted(self, source_id, claim_token, now):
        try:
            await self._retry_after_cancellation(source_id, claim_token, now)
        except BaseException:
            pass

    async def recover_interrupted(self, now=None):
        current = self._time(now)
        cutoff = current - self._recovery_after
        failed = False
        control = None
        try:
            result = await asyncio.to_thread(
                self.repository.recover_interrupted_sources, current, cutoff
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if type(error) in (KeyboardInterrupt, SystemExit):
                control = _sanitize_control_exception(error)
            else:
                failed = True
        if control is not None:
            raise control from None
        if failed:
            raise StyleSyncError("persistence_failed")
        if type(result) is not int or not 0 <= result <= 100:
            raise StyleSyncError("persistence_failed")
        return result

    async def loop(self, stop_event):
        if not isinstance(stop_event, asyncio.Event):
            raise ValueError("invalid_stop_event")
        while not stop_event.is_set():
            await asyncio.sleep(0)
            if stop_event.is_set():
                break
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except StyleSyncError:
                pass
            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._idle_poll_seconds
                )
            except asyncio.TimeoutError:
                pass
