import re
from dataclasses import replace
from typing import Callable, Optional, Tuple

from ..domain.memory2 import build_memory_context
from ..models import AgentRequest


_CONFIRM_PATTERN = re.compile(r"^确认记忆\s*#?(\d+)$")
_REJECT_PATTERN = re.compile(r"^(?:拒绝|忽略)记忆\s*#?(\d+)$")


class E3MemoryCommandHandler:
    def __init__(
        self,
        repository,
        confirmed_exporter: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self._repository = repository
        self._confirmed_exporter = confirmed_exporter

    def handle(self, request: AgentRequest) -> Optional[str]:
        text = request.text.strip()
        if text in ("待确认记忆", "候选记忆", "查看候选记忆"):
            return self._pending_reply(request.user_id)
        if text in ("记忆状态", "长期记忆状态"):
            return self._status_reply(request.user_id)
        if text in ("用户档案", "查看用户档案"):
            return self._profile_reply(request.user_id)

        match = _CONFIRM_PATTERN.fullmatch(text)
        if match:
            fact = self._repository.confirm_fact(request.user_id, int(match.group(1)))
            if fact is None or fact["status"] != "confirmed":
                return "这条候选记忆不存在或已经处理过了。别拿过期号码糊弄我。"
            if self._confirmed_exporter is not None:
                self._confirmed_exporter(request.user_id, str(fact["content"]))
            return "已确认记忆 #%d：%s" % (fact["id"], fact["content"])

        match = _REJECT_PATTERN.fullmatch(text)
        if match:
            fact = self._repository.reject_fact(request.user_id, int(match.group(1)))
            if fact is None or fact["status"] != "rejected":
                return "这条候选记忆不存在或已经处理过了。"
            return "已拒绝记忆 #%d。没确认的东西，我不会擅自当真。" % fact["id"]
        return None

    def _pending_reply(self, user_id: int) -> str:
        pending = self._repository.list_facts(user_id, status="pending", limit=20)
        if not pending:
            return "目前没有待确认记忆。账本很干净，难得。"
        lines = ["这些事实还在等你拍板："]
        for fact in reversed(pending):
            lines.append(
                "#%d [%s] %s" % (fact["id"], fact["category"], fact["content"])
            )
        lines.append("回复“确认记忆 编号”或“拒绝记忆 编号”。")
        return "\n".join(lines)

    def _status_reply(self, user_id: int) -> str:
        stats = self._repository.stats(user_id)
        return (
            "E3 记忆账本：已确认 %(confirmed)d，待确认 %(pending)d，"
            "已拒绝 %(rejected)d，对话 %(turns)d 轮，摘要 %(summaries)d 条。"
            % stats
        )

    def _profile_reply(self, user_id: int) -> str:
        profile = [
            fact
            for fact in self._repository.list_facts(user_id, status="confirmed")
            if fact["layer"] == "profile"
        ]
        if not profile:
            return "用户档案还是空的。放心，我不会靠猜给你乱贴标签。"
        lines = ["已确认用户档案："]
        lines.extend("- %s" % fact["content"] for fact in reversed(profile))
        return "\n".join(lines)


class MemoryContextService:
    def __init__(
        self,
        repository,
        persona_rules_provider: Callable[[], Tuple[str, ...]],
        max_chars: int = 2400,
        fact_limit: int = 6,
        summary_limit: int = 3,
    ) -> None:
        self._repository = repository
        self._persona_rules_provider = persona_rules_provider
        self._max_chars = max_chars
        self._fact_limit = fact_limit
        self._summary_limit = summary_limit

    def enrich(self, request: AgentRequest) -> AgentRequest:
        facts = self._repository.search_confirmed(
            request.user_id, request.text, self._fact_limit
        )
        summaries = self._repository.search_summaries(
            request.user_id, request.text, self._summary_limit
        )
        profile = tuple(
            str(item["content"]) for item in facts if item["layer"] == "profile"
        )
        long_term = tuple(
            str(item["content"]) for item in facts if item["layer"] == "long_term"
        )
        context = build_memory_context(
            persona_rules=self._persona_rules_provider(),
            profile=profile,
            facts=long_term,
            summaries=tuple(str(item["content"]) for item in summaries),
            max_chars=self._max_chars,
        )
        return replace(request, context=context)

