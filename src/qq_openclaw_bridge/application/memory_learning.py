import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Tuple

from ..models import AgentRequest


_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.I | re.S)
_ALLOWED_LAYERS = {"profile", "long_term"}
_TRANSIENT_CATEGORIES = {"temporary", "transient", "mood", "smalltalk"}


@dataclass(frozen=True)
class CandidateFact:
    content: str
    layer: str
    category: str
    confidence: float


@dataclass(frozen=True)
class AnalysisResult:
    summary: str
    facts: Tuple[CandidateFact, ...] = ()


def parse_analysis_response(
    text: str, min_confidence: float = 0.75
) -> AnalysisResult:
    candidate = text.strip()
    fenced = _FENCED_JSON.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        payload = None
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(candidate[index:])
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            raise ValueError("memory analysis response is not valid JSON")
    if not isinstance(payload, dict):
        raise ValueError("memory analysis response must be an object")
    summary = str(payload.get("summary", "")).strip()
    if not summary:
        raise ValueError("memory analysis response has no summary")
    facts = []
    raw_facts = payload.get("facts", [])
    if isinstance(raw_facts, list):
        for raw in raw_facts:
            if not isinstance(raw, dict) or raw.get("important") is not True:
                continue
            content = str(raw.get("content", "")).strip()
            layer = str(raw.get("layer", "long_term")).strip()
            category = str(raw.get("category", "general")).strip()
            try:
                confidence = float(raw.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            if (
                not content
                or layer not in _ALLOWED_LAYERS
                or category.casefold() in _TRANSIENT_CATEGORIES
                or confidence < min_confidence
            ):
                continue
            facts.append(
                CandidateFact(
                    content=content[:500],
                    layer=layer,
                    category=category[:50] or "general",
                    confidence=min(1.0, confidence),
                )
            )
    return AnalysisResult(summary=summary[:1200], facts=tuple(facts[:8]))


class LocalMemoryAnalyzer:
    model_name = "local"

    def analyze(
        self, user_id: int, turns: Iterable[Mapping[str, object]]
    ) -> AnalysisResult:
        del user_id
        lines = []
        for turn in turns:
            role = "用户" if turn["role"] == "user" else "chichu"
            content = " ".join(str(turn["content"]).split())[:180]
            lines.append("%s：%s" % (role, content))
        summary = "；".join(lines)
        return AnalysisResult(summary=summary[:1000] or "本轮没有可摘要文本。")


class OpenClawMemoryAnalyzer:
    def __init__(
        self,
        adapter,
        model_name: str,
        min_confidence: float = 0.75,
        session_prefix: str = "agent:memory:qq",
    ) -> None:
        self._adapter = adapter
        self.model_name = model_name
        self._min_confidence = min_confidence
        self._session_prefix = session_prefix

    def analyze(
        self, user_id: int, turns: Iterable[Mapping[str, object]]
    ) -> AnalysisResult:
        turns_tuple = tuple(turns)
        transcript = "\n".join(
            "%s: %s" % (turn["role"], turn["content"])
            for turn in turns_tuple
        )
        prompt = (
            "你是 chichu 的低成本记忆整理器。只输出 JSON，不要 Markdown。\n"
            "格式：{\"summary\":\"不超过300字的客观摘要\",\"facts\":["
            "{\"content\":\"可独立理解的稳定事实\",\"layer\":\"profile或long_term\","
            "\"category\":\"preference/project/constraint/identity/decision/general\","
            "\"confidence\":0到1,\"important\":true或false}]}。\n"
            "只提取用户明确表达且未来仍有用的事实；临时情绪、寒暄、模型推测、"
            "未确定计划不得标 important=true；不要从 assistant 的话推断用户事实。\n"
            "对话：\n%s" % transcript[:10000]
        )
        start_id = int(turns_tuple[0]["id"]) if turns_tuple else 0
        end_id = int(turns_tuple[-1]["id"]) if turns_tuple else 0
        response = self._adapter.reply(
            AgentRequest(
                user_id=user_id,
                message_id="e3-analysis-%d-%d" % (start_id, end_id),
                text=prompt,
                timestamp=time.time(),
                session_key="%s-%s-%d-%d" % (
                    self._session_prefix,
                    user_id,
                    start_id,
                    end_id,
                ),
            )
        )
        return parse_analysis_response(response.text, self._min_confidence)


class MemoryLearningService:
    def __init__(
        self,
        repository,
        analyzer,
        summary_trigger_turns: int = 8,
        summary_batch_turns: int = 24,
        metric_setter=None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._repository = repository
        self._analyzer = analyzer
        self._trigger = summary_trigger_turns
        self._batch = max(summary_batch_turns, summary_trigger_turns)
        self._metric_setter = metric_setter
        self._logger = logger or logging.getLogger("qq_openclaw_bridge.memory")
        self._tasks: Dict[int, asyncio.Task] = {}

    async def observe(self, request: AgentRequest, reply: str) -> None:
        base_key = request.message_id or "%s-%s" % (request.user_id, request.timestamp)
        await asyncio.to_thread(
            self._record_exchange,
            request.user_id,
            request.text,
            reply,
            base_key,
        )
        turns = await asyncio.to_thread(
            self._repository.unsummarized_turns, request.user_id, self._batch
        )
        self._set_metric("memory_unsummarized_turns", str(len(turns)))
        existing = self._tasks.get(request.user_id)
        if len(turns) >= self._trigger and (existing is None or existing.done()):
            task = asyncio.create_task(self._analyze_user(request.user_id))
            self._tasks[request.user_id] = task
            task.add_done_callback(
                lambda completed, user_id=request.user_id: self._task_done(
                    user_id, completed
                )
            )

    def _record_exchange(
        self, user_id: int, user_text: str, reply: str, base_key: str
    ) -> None:
        self._repository.record_turn(
            user_id, "user", user_text, "memory:%s:user" % base_key
        )
        self._repository.record_turn(
            user_id, "assistant", reply, "memory:%s:assistant" % base_key
        )

    async def _analyze_user(self, user_id: int) -> None:
        while True:
            turns = await asyncio.to_thread(
                self._repository.unsummarized_turns, user_id, self._batch
            )
            if len(turns) < self._trigger:
                self._set_metric("memory_unsummarized_turns", str(len(turns)))
                return
            start_id = int(turns[0]["id"])
            end_id = int(turns[-1]["id"])
            run_id = await asyncio.to_thread(
                self._repository.start_analysis_run,
                user_id,
                start_id,
                end_id,
                self._analyzer.model_name,
            )
            try:
                result = await asyncio.to_thread(
                    self._analyzer.analyze, user_id, tuple(turns)
                )
                await asyncio.to_thread(
                    self._save_result,
                    user_id,
                    turns,
                    result,
                    self._analyzer.model_name,
                )
                await asyncio.to_thread(
                    self._repository.finish_analysis_run, run_id, "success", None
                )
                self._set_metric("last_memory_analysis_success_at", str(time.time()))
            except Exception as exc:
                self._logger.exception("memory light analysis failed; using local fallback")
                fallback = LocalMemoryAnalyzer().analyze(user_id, turns)
                await asyncio.to_thread(
                    self._save_result,
                    user_id,
                    turns,
                    fallback,
                    "local-fallback",
                )
                await asyncio.to_thread(
                    self._repository.finish_analysis_run,
                    run_id,
                    "degraded",
                    str(exc)[:300],
                )
                self._set_metric("last_memory_analysis_failure", str(exc)[:300])

    def _save_result(
        self,
        user_id: int,
        turns,
        result: AnalysisResult,
        model: str,
    ) -> None:
        self._repository.save_summary(
            user_id=user_id,
            start_turn_id=int(turns[0]["id"]),
            end_turn_id=int(turns[-1]["id"]),
            content=result.summary,
            model=model,
        )
        source_message_id = next(
            (
                str(turn["dedupe_key"])
                for turn in reversed(turns)
                if turn["role"] == "user"
            ),
            None,
        )
        for fact in result.facts:
            self._repository.add_candidate(
                user_id=user_id,
                layer=fact.layer,
                category=fact.category,
                content=fact.content,
                confidence=fact.confidence,
                source_message_id=source_message_id,
            )

    def _set_metric(self, name: str, value: str) -> None:
        if self._metric_setter is not None:
            self._metric_setter(name, value)

    def _task_done(self, user_id: int, task: asyncio.Task) -> None:
        if self._tasks.get(user_id) is task:
            self._tasks.pop(user_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error(
                "memory analysis task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def wait(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
