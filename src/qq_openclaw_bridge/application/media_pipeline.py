import asyncio
import inspect
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class MediaArtifact:
    kind: str
    label: str
    content: str
    status: str
    source_asset_id: Optional[str]


_LABELS = {
    "image": "图片描述",
    "audio": "语音转写",
    "file": "文件正文",
}
_FAILURES = {
    "image": "图片处理失败",
    "audio": "语音处理失败",
    "file": "文件处理失败",
}


class MediaPipeline:
    def __init__(
        self,
        ingestion,
        media_api,
        document_extractor,
        max_concurrent_calls: int = 2,
        document_max_chars: int = 12000,
        image_prompt: str = "请简要描述图片中的可见内容。",
        processors: Optional[Dict[str, Callable]] = None,
    ) -> None:
        if type(max_concurrent_calls) is not int or max_concurrent_calls < 1:
            raise ValueError("max_concurrent_calls must be at least 1")
        self._ingestion = ingestion
        self._media_api = media_api
        self._document_extractor = document_extractor
        self._document_max_chars = max(0, int(document_max_chars))
        self._image_prompt = image_prompt
        self._semaphore = asyncio.Semaphore(max_concurrent_calls)
        self._processors = {}
        self.register_processor("image", self._describe_image)
        self.register_processor("audio", self._transcribe_audio)
        self.register_processor("file", self._extract_document)
        for kind, processor in (processors or {}).items():
            self.register_processor(kind, processor)

    def register_processor(self, kind: str, processor: Callable) -> None:
        if not isinstance(kind, str) or not kind or not callable(processor):
            raise ValueError("kind and processor are required")
        self._processors[kind] = processor

    async def process(self, envelope, progress, cancellation) -> Tuple[MediaArtifact, ...]:
        image_count = 0
        tasks = []
        for index, ref in enumerate(envelope.attachments, start=1):
            image_over_limit = False
            if ref.kind == "image":
                image_count += 1
                image_over_limit = image_count > 4
            tasks.append(
                asyncio.create_task(
                    self._process_attachment(
                        envelope, index, ref, image_over_limit, progress, cancellation
                    )
                )
            )
        try:
            return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _process_attachment(
        self, envelope, index, ref, image_over_limit, progress, cancellation
    ) -> MediaArtifact:
        label = _LABELS.get(ref.kind, "附件")
        self._raise_if_cancelled(cancellation)
        if ref.kind not in self._processors:
            return self._failed(ref.kind, label)
        if image_over_limit:
            return self._failed(ref.kind, label, "图片处理失败（图片数量超过上限）")

        async with self._semaphore:
            self._raise_if_cancelled(cancellation)
            await self._report(progress, "downloading", self._detail(index, ref.kind))
            self._raise_if_cancelled(cancellation)
            try:
                media = await asyncio.to_thread(
                    self._ingestion.fetch, ref, envelope.user_id, envelope.message_id
                )
            except Exception:
                media = None
            if media is None:
                self._raise_if_cancelled(cancellation)
                await self._report(
                    progress, "attachment_failed", self._detail(index, ref.kind)
                )
                return self._failed(ref.kind, label)

            self._raise_if_cancelled(cancellation)
            stage = self._stage_for_kind(ref.kind)
            await self._report(progress, stage, self._detail(index, ref.kind, media.asset_id))
            self._raise_if_cancelled(cancellation)
            try:
                artifact = await self._run_processor(self._processors[ref.kind], ref, media)
            except Exception:
                artifact = None
            if artifact is None:
                self._raise_if_cancelled(cancellation)
                await self._report(
                    progress, "attachment_failed", self._detail(index, ref.kind, media.asset_id)
                )
                return self._failed(ref.kind, label, source_asset_id=media.asset_id)
            self._raise_if_cancelled(cancellation)
            return artifact

    async def _run_processor(self, processor, ref, media) -> MediaArtifact:
        result = await asyncio.to_thread(processor, ref, media)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, MediaArtifact):
            raise TypeError("media processor must return MediaArtifact")
        return result

    def _describe_image(self, ref, media) -> MediaArtifact:
        result = self._media_api.describe_image(media, self._image_prompt)
        return MediaArtifact("image", _LABELS["image"], result.text, "ready", media.asset_id)

    def _transcribe_audio(self, ref, media) -> MediaArtifact:
        result = self._media_api.transcribe_audio(media)
        return MediaArtifact("audio", _LABELS["audio"], result.text, "ready", media.asset_id)

    def _extract_document(self, ref, media) -> MediaArtifact:
        result = self._document_extractor.extract(media, self._document_max_chars)
        content = result.text
        if result.truncated:
            content = "%s\n[文件正文已截断]" % content
        return MediaArtifact("file", _LABELS["file"], content, "ready", media.asset_id)

    @staticmethod
    def _detail(index, kind, asset_id=None) -> str:
        detail = "attachment=%s kind=%s" % (index, kind)
        if asset_id:
            detail = "%s asset_id=%s" % (detail, asset_id)
        return detail

    @staticmethod
    def _stage_for_kind(kind: str) -> str:
        return {
            "image": "describing_image",
            "audio": "transcribing_audio",
            "file": "extracting_document",
        }.get(kind, "processing_attachment")

    @staticmethod
    def _raise_if_cancelled(cancellation) -> None:
        if cancellation is not None:
            cancellation.raise_if_cancelled()

    @staticmethod
    async def _report(progress, stage: str, detail: str) -> None:
        if progress is None:
            return
        failed = False
        try:
            result = progress(stage, detail)
            if inspect.isawaitable(result):
                await result
        except Exception:
            failed = True
        if failed:
            raise RuntimeError("media progress callback failed")

    @staticmethod
    def _failed(kind, label, content=None, source_asset_id=None) -> MediaArtifact:
        return MediaArtifact(
            kind,
            label,
            content or _FAILURES.get(kind, "附件处理失败"),
            "failed",
            source_asset_id,
        )


__all__ = ["MediaArtifact", "MediaPipeline"]
