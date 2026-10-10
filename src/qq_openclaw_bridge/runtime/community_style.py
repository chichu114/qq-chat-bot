"""Composition root for the optional, isolated group-chat agent."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..application.group_chat import GroupMessageCoordinator
from ..application.group_delivery import GroupDeliveryService
from ..application.group_homophone_filter import GroupHomophoneFilter
from ..application.group_interjection import GroupInterjectionDecider
from ..application.private_ollama import KanagawaWavePrivateAdapter
from ..rate_limit import RateLimiter
from .community_style_config import CommunityStyleConfigReloader
from .ollama import OllamaClient
from .onebot_rpc import OneBotRpcClient


class _LazySemaphore:
    """Bind asyncio synchronization only after an event loop is running."""

    def __init__(self, value: int) -> None:
        self._value = value
        self._semaphore = None

    async def __aenter__(self):
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._value)
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self._semaphore.release()


class _ClockedRateLimiter(RateLimiter):
    """Use the injected runtime clock at the otherwise unchanged limiter seam."""

    def __init__(self, limit: int, window_seconds: float, clock) -> None:
        super().__init__(limit, window_seconds=window_seconds)
        self._clock = clock

    def allow_pair(self, key, other, other_key, now=None):
        if now is None:
            now = self._clock().timestamp()
        return super().allow_pair(key, other, other_key, now=now)

    def allow(self, key, now=None):
        if now is None:
            now = self._clock().timestamp()
        return super().allow(key, now=now)


@dataclass(frozen=True)
class GroupRuntime:
    repository: object
    config_reloader: object
    rpc_client: OneBotRpcClient
    coordinator: GroupMessageCoordinator
    delivery: GroupDeliveryService
    allowed_groups_provider: Callable[[], Optional[frozenset]]
    group_limiter: RateLimiter
    user_limiter: RateLimiter
    generation_semaphore: object
    interjection_decider: Optional[GroupInterjectionDecider]
    interjection_limiter: Optional[RateLimiter]
    private_agent_adapter: Optional[KanagawaWavePrivateAdapter]


def create_group_runtime(
    config,
    state_store,
    *,
    repository_factory=None,
    config_reloader_factory=CommunityStyleConfigReloader,
    rpc_factory=OneBotRpcClient,
    ollama_factory=OllamaClient,
    interjection_factory=GroupInterjectionDecider,
    private_adapter_factory=KanagawaWavePrivateAdapter,
    coordinator_factory=GroupMessageCoordinator,
    delivery_factory=GroupDeliveryService,
    clock_factory=None,
) -> Optional[GroupRuntime]:
    if getattr(config, "enable_group_agent", False) is not True:
        return None

    repository = (
        getattr(state_store, "community_style_repository", None)
        if repository_factory is None
        else repository_factory(state_store)
    )
    if repository is None:
        raise RuntimeError("group agent requires a community style repository")
    reloader = config_reloader_factory(Path(config.style_agent_config_path))
    settings = reloader.current()
    if settings.enabled is not True:
        return None

    rpc_client = rpc_factory()
    ollama = ollama_factory(
        settings.ollama,
        timeout_seconds=float(config.openclaw_timeout_seconds),
    )
    delivery_kwargs = {}
    if getattr(config, "enable_group_homophone_filter", False) is True:
        homophone_filter = GroupHomophoneFilter(
            Path(config.group_homophone_map_path)
        )
        delivery_kwargs["text_filter"] = homophone_filter.replace
    clock = None
    if clock_factory is None:
        coordinator = coordinator_factory(repository, reloader, ollama)
        delivery = delivery_factory(repository, rpc_client, **delivery_kwargs)
    else:
        clock = clock_factory()
        if not callable(clock):
            raise ValueError("group runtime clock factory must return a callable")
        coordinator = coordinator_factory(
            repository, reloader, ollama, clock=clock
        )
        delivery_kwargs["now_provider"] = clock
        delivery = delivery_factory(repository, rpc_client, **delivery_kwargs)
    cooldown = max((group.cooldown_seconds for group in settings.groups), default=60)
    interjection_decider = None
    interjection_limiter = None
    if getattr(config, "enable_group_interjections", False) is True:
        interjection_decider = interjection_factory(ollama)
        interjection_cooldown = float(config.group_interjection_cooldown_seconds)
        interjection_limiter = (
            RateLimiter(1, window_seconds=interjection_cooldown)
            if clock is None
            else _ClockedRateLimiter(1, interjection_cooldown, clock)
        )
    private_agent_adapter = None
    if getattr(config, "kanagawave_private_users", set()):
        private_agent_adapter = private_adapter_factory(
            ollama, model=settings.ollama.model
        )
    return GroupRuntime(
        repository=repository,
        config_reloader=reloader,
        rpc_client=rpc_client,
        coordinator=coordinator,
        delivery=delivery,
        allowed_groups_provider=coordinator.current_group_ids,
        group_limiter=(
            RateLimiter(1, window_seconds=float(cooldown))
            if clock is None
            else _ClockedRateLimiter(1, float(cooldown), clock)
        ),
        user_limiter=RateLimiter(
            config.rate_limit_messages_per_minute, window_seconds=60.0
        ),
        generation_semaphore=_LazySemaphore(config.max_concurrent_agent_calls),
        interjection_decider=interjection_decider,
        interjection_limiter=interjection_limiter,
        private_agent_adapter=private_agent_adapter,
    )


__all__ = ["GroupRuntime", "create_group_runtime"]
