"""Process entry point and composition for the standalone style learner."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
from typing import Mapping, Optional, Sequence

from qq_openclaw_bridge.application.style_sync import StyleSyncService
from qq_openclaw_bridge.config import Config
from qq_openclaw_bridge.domain.community_style import aggregate_style_profile
from qq_openclaw_bridge.runtime.community_style_config import (
    CommunityStyleConfigReloader,
)
from qq_openclaw_bridge.runtime.logging import (
    configure_process_logging,
    logging_options_from_env,
    write_safe_bootstrap_error,
)
from qq_openclaw_bridge.runtime.ollama import OllamaClient, OllamaStyleAnalyzer
from qq_openclaw_bridge.runtime.reddit_source import RedditSource
from qq_openclaw_bridge.runtime.secrets import SecretResolver
from qq_openclaw_bridge.runtime.tieba_source import TiebaSource
from qq_openclaw_bridge.storage import WorkspaceStateStore


_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_ENV_BYTES = 64 * 1024
_MAX_ENV_LINE = 8 * 1024
_TRUE = frozenset(("1", "true", "yes", "on"))
_FALSE = frozenset(("0", "false", "no", "off"))
_REDDIT_REF = "REDDIT_OAUTH_TOKEN_REF"
_DEFAULT_POLL_SECONDS = 30.0


def _sanitized_control(error):
    """Return a payload-free exact control exception, or None for subclasses."""
    if type(error) not in (GeneratorExit, KeyboardInterrupt, SystemExit):
        return None
    error.args = ()
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None
    if type(error) is GeneratorExit:
        return GeneratorExit()
    return KeyboardInterrupt() if type(error) is KeyboardInterrupt else SystemExit()


def _capture_call(function, *arguments):
    try:
        return "ok", function(*arguments)
    except BaseException as error:
        control = _sanitized_control(error)
        if control is not None:
            return "control", control
        return "failure", None


class _BoundedAnalyzer:
    """Share one bounded inference lane across every configured source."""

    def __init__(self, analyzer, maximum_concurrency: int = 1) -> None:
        if not callable(getattr(analyzer, "analyze", None)):
            raise ValueError("invalid analyzer")
        if type(maximum_concurrency) is not int or not 1 <= maximum_concurrency <= 4:
            raise ValueError("invalid analyzer concurrency")
        self._analyzer = analyzer
        self._semaphore = threading.BoundedSemaphore(maximum_concurrency)

    def analyze(self, text):
        with self._semaphore:
            return self._analyzer.analyze(text)


class _DeferredSyncService:
    """Create asyncio-bound Task 7 state only inside the serving event loop."""

    def __init__(self, factory, arguments, keywords) -> None:
        self._factory = factory
        self._arguments = arguments
        self._keywords = keywords
        self._service = None

    def _current(self):
        if self._service is None:
            self._service = self._factory(*self._arguments, **self._keywords)
            self._factory = None
            self._arguments = None
            self._keywords = None
        return self._service

    async def recover_interrupted(self):
        return await self._current().recover_interrupted()

    async def run_due_once(self):
        return await self._current().run_due_once()


def _default_analyzer_factory(settings):
    return _BoundedAnalyzer(OllamaStyleAnalyzer(OllamaClient(settings)), 1)


def _default_source_factory(settings, token_provider=None):
    if settings.platform == "reddit":
        if not callable(token_provider):
            raise ValueError("Reddit source requires a token provider")
        return RedditSource(settings, token_provider=token_provider)
    if settings.platform == "tieba":
        return TiebaSource(settings)
    raise ValueError("unsupported style source platform")


def _utc_now():
    return datetime.now(timezone.utc)


def _safe_reference(env: Mapping[str, str], has_reddit: bool) -> Optional[str]:
    if not has_reddit:
        return None
    raw = env.get(_REDDIT_REF)
    if type(raw) is not str:
        return None
    reference = raw.strip()
    if not reference:
        return None
    parts = reference.split(":")
    if (
        len(parts) != 3
        or parts[0] != "keychain"
        or not parts[1]
        or not parts[2]
        or len(reference) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in reference)
    ):
        return None
    return reference


def _adapter_fingerprint(source, reddit_reference: Optional[str]) -> str:
    payload = {
        "id": source.id,
        "platform": source.platform,
        "community": source.community,
        "credential_reference": reddit_reference if source.platform == "reddit" else None,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StyleLearnerRuntime:
    """Own hot reload, source adapters, and one authorization-first sync service."""

    def __init__(
        self,
        repository,
        config_reloader,
        env: Mapping[str, str],
        *,
        source_factory=_default_source_factory,
        analyzer_factory=_default_analyzer_factory,
        sync_factory=StyleSyncService,
        aggregate_factory=aggregate_style_profile,
        secret_resolver=None,
        clock=None,
        logger=None,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
    ) -> None:
        for name in ("sync_configuration", "active_samples", "publish_profile"):
            if not callable(getattr(repository, name, None)):
                raise ValueError("invalid community style repository")
        if not callable(getattr(config_reloader, "current", None)) or not callable(
            getattr(config_reloader, "reload_if_changed", None)
        ):
            raise ValueError("invalid community style config reloader")
        if not isinstance(env, Mapping):
            raise ValueError("invalid learner environment")
        if not all(callable(value) for value in (
            source_factory, analyzer_factory, sync_factory, aggregate_factory
        )):
            raise ValueError("invalid learner factories")
        if type(poll_seconds) not in (int, float) or not 0 < float(poll_seconds) <= 30:
            raise ValueError("invalid learner poll interval")
        self.repository = repository
        self.config_reloader = config_reloader
        self._env = dict(env)
        self._source_factory = source_factory
        self._analyzer_factory = analyzer_factory
        self._sync_factory = sync_factory
        self._aggregate_factory = aggregate_factory
        self._secret_resolver = secret_resolver or SecretResolver()
        self._clock = clock or _utc_now
        self._logger = logger or logging.getLogger("style-learner")
        self._poll_seconds = float(poll_seconds)
        self.settings = None
        self.sources = {}
        self._source_fingerprints = {}
        self._analyzer = None
        self._sync = None
        self._pending_profiles = set()
        self.refresh_configuration(force=True)

    def _time(self):
        kind, current = _capture_call(self._clock)
        if kind == "control":
            raise current from None
        if kind != "ok" or not isinstance(current, datetime) or current.tzinfo is None:
            raise RuntimeError("learner clock failed") from None
        try:
            return current.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            raise RuntimeError("learner clock failed") from None

    def _token_provider(self, reference: str):
        resolver = self._secret_resolver

        def provide():
            kind, value = _capture_call(resolver.resolve, reference, None)
            if kind == "control":
                raise value from None
            if kind != "ok":
                raise ValueError("Reddit OAuth token resolution failed") from None
            if type(value) is not str or not value.strip() or len(value) > 8192:
                raise ValueError("Reddit OAuth token resolution failed")
            return value.strip()

        return provide

    @staticmethod
    def _unavailable_token_provider():
        raise ValueError("Reddit OAuth token unavailable") from None

    def _stage(self, candidate):
        active_sources = candidate.sources if candidate.enabled else ()
        has_reddit = any(item.platform == "reddit" for item in active_sources)
        reference = _safe_reference(self._env, has_reddit)
        staged = {}
        fingerprints = {}
        for source in active_sources:
            fingerprint = _adapter_fingerprint(source, reference)
            fingerprints[source.id] = fingerprint
            if self._source_fingerprints.get(source.id) == fingerprint:
                staged[source.id] = self.sources[source.id]
                continue
            provider = (
                self._token_provider(reference)
                if source.platform == "reddit" and reference is not None
                else (
                    self._unavailable_token_provider
                    if source.platform == "reddit"
                    else None
                )
            )
            staged[source.id] = self._source_factory(
                source, token_provider=provider
            )
        analyzer = self._analyzer
        if self.settings is None or self.settings.ollama != candidate.ollama:
            analyzer = self._analyzer_factory(candidate.ollama)
        return staged, fingerprints, analyzer

    def _rebuild_profiles(self, now):
        for profile_key in tuple(sorted(self._pending_profiles)):
            samples = self.repository.active_samples(profile_key)
            profile = self._aggregate_factory(
                profile_key, samples, minimum_samples=5
            )
            if profile is not None:
                self.repository.publish_profile(profile, now)
            self._pending_profiles.remove(profile_key)

    def _new_sync(self, sources, analyzer):
        arguments = (self.repository, sources, analyzer)
        keywords = {"idle_poll_seconds": self._poll_seconds}
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _DeferredSyncService(self._sync_factory, arguments, keywords)
        return self._sync_factory(*arguments, **keywords)

    def refresh_configuration(self, force: bool = False) -> bool:
        kind, value = _capture_call(self._refresh_configuration, force)
        if kind == "control":
            raise value from None
        if kind != "ok":
            raise RuntimeError("style configuration sync failed") from None
        return value

    def _refresh_configuration(self, force: bool) -> bool:
        self.config_reloader.reload_if_changed()
        candidate = self.config_reloader.current()
        changed = force or self.settings is None or (
            candidate.fingerprint != self.settings.fingerprint
        )
        if changed:
            staged, fingerprints, analyzer = self._stage(candidate)
        else:
            staged = self.sources
            fingerprints = self._source_fingerprints
            analyzer = self._analyzer
        now = self._time()
        affected = self.repository.sync_configuration(candidate, now)
        self._pending_profiles.update(affected)
        self._rebuild_profiles(now)
        if not changed:
            return False
        sync = self._new_sync(staged, analyzer)
        self.settings = candidate
        self.sources = staged
        self._source_fingerprints = fingerprints
        self._analyzer = analyzer
        self._sync = sync
        return True

    async def recover_interrupted(self):
        return await self._sync.recover_interrupted()

    async def run_due_once(self):
        self.refresh_configuration()
        control = None
        failed = False
        result = None
        try:
            result = await self._sync.run_due_once()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            control = _sanitized_control(error)
            failed = control is None
        if control is not None:
            raise control from None
        if failed:
            raise RuntimeError("style sync failed") from None
        return result

    async def loop(self, stop_event):
        if not isinstance(stop_event, asyncio.Event):
            raise ValueError("invalid stop event")
        while not stop_event.is_set():
            control = None
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                control = _sanitized_control(error)
                if control is None:
                    self._logger.warning(
                        "style_sync_failed",
                        extra={"error_class": error.__class__.__name__},
                    )
            if control is not None:
                raise control from None
            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._poll_seconds
                )
            except asyncio.TimeoutError:
                pass


def _resolved_path(project: Path, raw: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def build_style_learner(config, *, env=None, project_dir=None):
    if not isinstance(config, Config) or config.enable_group_agent is not True:
        raise ValueError("style learner requires enabled group-agent config")
    project = Path.cwd().resolve() if project_dir is None else Path(project_dir).resolve()
    source_env = os.environ if env is None else env
    store = WorkspaceStateStore(_resolved_path(project, config.state_database_path))
    reloader = CommunityStyleConfigReloader(
        _resolved_path(project, config.style_agent_config_path)
    )
    return StyleLearnerRuntime(
        store.community_style_repository,
        reloader,
        source_env,
    )


async def run_learner(learner, stop_event, *, once: bool = False):
    await learner.recover_interrupted()
    if once:
        if not stop_event.is_set():
            await learner.run_due_once()
        return
    await learner.loop(stop_event)


def _install_signal_handlers(stop_event):
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass


def _parse_env(content: str):
    values = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        if len(raw_line) > _MAX_ENV_LINE:
            raise ValueError(".env line is too long")
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(".env line is invalid")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if _ENV_KEY.fullmatch(key) is None or key in values:
            raise ValueError(".env key is invalid")
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(".env quote is invalid")
            value = value[1:-1]
        values[key] = value
    return values


def _load_project_env(project: Path):
    path = project / ".env"
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise RuntimeError("secure environment loading unavailable")
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(".env must be a regular file")
        if metadata.st_uid != os.getuid():
            raise ValueError(".env has an invalid owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError(".env permissions must be 0600")
        if metadata.st_size > _MAX_ENV_BYTES:
            raise ValueError(".env is too large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(_MAX_ENV_BYTES + 1)
        if len(payload) > _MAX_ENV_BYTES:
            raise ValueError(".env is too large")
        return _parse_env(payload.decode("utf-8"))
    finally:
        os.close(descriptor)


def _enabled(env: Mapping[str, str]) -> bool:
    raw = env.get("ENABLE_GROUP_AGENT", "false")
    if type(raw) is not str:
        raise ValueError("ENABLE_GROUP_AGENT must be true or false")
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE or not value:
        return False
    raise ValueError("ENABLE_GROUP_AGENT must be true or false")


def _arguments(argv: Optional[Sequence[str]]):
    values = list(sys.argv[1:] if argv is None else argv)
    once = False
    project = Path.cwd()
    index = 0
    while index < len(values):
        value = values[index]
        if value == "--once" and not once:
            once = True
        elif value == "--project-dir" and index + 1 < len(values):
            index += 1
            project = Path(values[index])
        elif value in ("-h", "--help"):
            return None
        else:
            raise ValueError("invalid style learner argument")
        index += 1
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise ValueError("project directory must exist")
    return once, project


def main(argv: Optional[Sequence[str]] = None) -> int:
    logger = None
    generator_exit = None
    try:
        arguments = _arguments(argv)
        if arguments is None:
            print("Usage: python -m qq_openclaw_style_learner [--once] [--project-dir PATH]")
            return 0
        once, project = arguments
        process_env = dict(os.environ)
        env = (
            process_env
            if "ENABLE_GROUP_AGENT" in process_env
            else _load_project_env(project)
        )
        if not _enabled(env):
            return 0
        logger = configure_process_logging(
            "style-learner", project / "logs", "info", 10485760, 5
        )
        level, max_bytes, backup_count = logging_options_from_env(env)
        logger = configure_process_logging(
            "style-learner", project / "logs", level, max_bytes, backup_count
        )
        config = Config.from_env(env)
        learner = build_style_learner(config, env=env, project_dir=project)

        async def run():
            stop_event = asyncio.Event()
            _install_signal_handlers(stop_event)
            await run_learner(learner, stop_event, once=once)

        asyncio.run(run())
        return 0
    except BaseException as error:
        if type(error) is GeneratorExit:
            generator_exit = _sanitized_control(error)
        elif type(error) is KeyboardInterrupt:
            return 0
        elif type(error) is SystemExit:
            code = getattr(error, "code", 1)
            return code if type(code) is int and 0 <= code <= 255 else 1
        else:
            if logger is None:
                write_safe_bootstrap_error("style-learner", error)
            else:
                logger.critical(
                    "startup_failed",
                    extra={"error_class": error.__class__.__name__},
                )
            return 1
    raise generator_exit from None


if __name__ == "__main__":
    raise SystemExit(main())
