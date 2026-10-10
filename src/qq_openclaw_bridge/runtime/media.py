import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..application.documents import DocumentTextExtractor
from ..application.long_tasks import LongTaskService
from ..application.media_budget import MediaBudgetPolicy
from ..application.media_ingestion import MediaIngestionService, NapCatLocalMediaSource
from ..application.media_pipeline import MediaPipeline
from ..application.quotes import QuoteResolver
from ..application.task_commands import TaskCommandHandler
from .media_api import MediaApiClient
from .onebot_rpc import OneBotRpcClient
from .secrets import SecretResolver


@dataclass(frozen=True)
class MediaRuntime:
    pipeline: Optional[object]
    quote_resolver: object
    task_service: Optional[object]
    task_commands: Optional[object]
    rpc_client: object


def create_media_runtime(
    config,
    state_store,
    *,
    secret_resolver_factory=SecretResolver,
    rpc_client_factory=OneBotRpcClient,
    quote_resolver_factory=QuoteResolver,
    ingestion_factory=MediaIngestionService,
    document_extractor_factory=DocumentTextExtractor,
    budget_policy_factory=MediaBudgetPolicy,
    media_api_factory=MediaApiClient,
    pipeline_factory=MediaPipeline,
    task_service_factory=LongTaskService,
    task_commands_factory=TaskCommandHandler,
):
    if not (config.enable_media_pipeline or config.enable_long_tasks):
        return None
    repository = getattr(state_store, "e4_repository", None)
    if repository is None:
        raise RuntimeError("E4 features require a SQLite state store")

    api_key = None
    if config.enable_media_pipeline:
        api_key = secret_resolver_factory().resolve(
            config.media_api_secret_ref,
            os.environ.get("MEDIA_API_KEY"),
        )

    rpc_client = rpc_client_factory()
    quote_resolver = quote_resolver_factory(rpc_client)
    pipeline = None
    if config.enable_media_pipeline:
        if not (config.napcat_media_roots or config.media_remote_allowed_hosts):
            raise RuntimeError(
                "media pipeline requires NAPCAT_MEDIA_ROOTS or "
                "MEDIA_REMOTE_ALLOWED_HOSTS"
            )
        cache_root = Path(config.media_cache_dir).expanduser()
        if not cache_root.is_absolute():
            raise ValueError("media cache root must be an absolute path")
        local_source = None
        if config.napcat_media_roots:
            trusted_roots = tuple(
                Path(root).expanduser() for root in config.napcat_media_roots
            )
            if any(not root.is_absolute() for root in trusted_roots):
                raise ValueError("NapCat roots must be absolute paths")
            local_source = NapCatLocalMediaSource(
                trusted_roots
            )
        ingestion = ingestion_factory(
            cache_root,
            repository,
            ttl_seconds=config.media_cache_ttl_seconds,
            local_source=local_source,
            allowed_remote_hosts=config.media_remote_allowed_hosts,
        )
        document_extractor = document_extractor_factory(cache_root)
        budget_policy = budget_policy_factory(
            daily_budget=config.media_api_daily_budget,
            consumed_cost_provider=lambda day: repository.consumed_model_cost(
                day, provider="media_api"
            ),
        )
        media_api = media_api_factory(
            base_url=config.media_api_base_url,
            api_key=api_key,
            image_model=config.media_image_model,
            audio_model=config.media_audio_model,
            repository=repository,
            timeout_seconds=config.media_api_timeout_seconds,
            requests_per_minute=config.media_api_requests_per_minute,
            max_concurrent_calls=config.media_max_concurrent_calls,
            budget_policy=budget_policy,
        )
        pipeline = pipeline_factory(
            ingestion,
            media_api,
            document_extractor,
            max_concurrent_calls=config.media_max_concurrent_calls,
        )

    task_service = None
    task_commands = None
    if config.enable_long_tasks:
        task_service = task_service_factory(
            repository,
            ack_seconds=config.long_task_ack_seconds,
            progress_interval_seconds=config.long_task_progress_interval_seconds,
        )
        task_commands = task_commands_factory(repository, task_service)

    return MediaRuntime(
        pipeline,
        quote_resolver,
        task_service,
        task_commands,
        rpc_client,
    )


__all__ = ["MediaRuntime", "create_media_runtime"]
