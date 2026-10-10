from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
from typing import Callable, Optional, Sequence

from .config import Config


STYLE_MEMORY_SECTION = "## 用户反馈沉淀"
DEFAULT_STYLE_MEMORY_PATH = "~/.openclaw/workspace/STYLE.md"


@dataclass(frozen=True)
class StyleFeedbackRule:
    markers: Sequence[str]
    guidance: str
    reply: str


STYLE_FEEDBACK_RULES = (
    StyleFeedbackRule(
        markers=("太客服", "有点客服", "客服腔"),
        guidance="避免客服腔：少模板化确认，先给结论，语气更像 chichu 本人。",
        reply="记下了。以后少点客服味，先给结论，少废话。",
    ),
    StyleFeedbackRule(
        markers=("少废话", "太长", "啰嗦", "别废话"),
        guidance="回复要更短更利落：先办事，再吐槽，不铺垫。",
        reply="行，少废话，多办事。你总算说到重点了。",
    ),
    StyleFeedbackRule(
        markers=("太凶", "有点凶", "别太凶", "太刻薄", "太阴阳"),
        guidance="收敛毒舌：保留判断和锋芒，但减少压迫感，不羞辱用户。",
        reply="行，刀收半寸。毒舌归毒舌，正事不能被我说炸。",
    ),
    StyleFeedbackRule(
        markers=("太油", "油腻", "太暧昧", "别撩", "装可爱"),
        guidance="避免油腻：不要暧昧、卖萌或过度表演，保持冷静利落的管家感。",
        reply="懂，去油。管家不是小广告，我会收干净点。",
    ),
    StyleFeedbackRule(
        markers=("太冷", "太硬", "没温度", "太严肃"),
        guidance="增加一点温度：保留利落判断，但给用户一点照顾感和缓冲。",
        reply="行，温度加一点。不是让你泡温泉，是别把话说成冰块。",
    ),
    StyleFeedbackRule(
        markers=("更毒舌", "嘴毒一点", "吐槽狠一点", "更犀利"),
        guidance="毒舌更锋利：吐槽可以更直接，但必须服务于推进事情，不攻击人格。",
        reply="行，刀磨亮一点。但你放心，我砍问题，不砍你。",
    ),
    StyleFeedbackRule(
        markers=("少自我介绍", "别老自我介绍", "不要每次介绍自己", "少说你是谁"),
        guidance="减少自我介绍：除非用户问身份，否则不要反复介绍 chichu。",
        reply="记下。少报户口，多干活。你终于嫌我啰嗦得有点道理。",
    ),
    StyleFeedbackRule(
        markers=("更像管家", "管家感强一点", "像管家一点", "更有管家感"),
        guidance="强化管家感：主动归纳下一步、提醒风险、收束行动，不只聊天。",
        reply="行，管家感拉高。你负责别乱丢事，我负责收拢。",
    ),
    StyleFeedbackRule(
        markers=("更御姐", "御姐一点", "毒舌一点", "管家一点", "利落一点"),
        guidance="御姐管家风更明显：冷静、利落、带一点吐槽，但不要耽误正事。",
        reply="行，御姐管家模式再往上拧一点。你别离谱，我来收拾。",
    ),
    StyleFeedbackRule(
        markers=("这个味对了", "这句味对了", "风格可以", "保留这种", "以后都这样", "记住这句"),
        guidance="保留这类表达：有判断、有轻微吐槽、但把事情推进清楚。",
        reply="保留。以后按这个劲儿来，别让我又端回客服腔。",
    ),
)


class StyleMemory:
    def __init__(
        self,
        style_path: Path,
        date_provider: Callable[[], date] = date.today,
        state_store: Optional[object] = None,
    ) -> None:
        self.style_path = Path(style_path).expanduser()
        self._date_provider = date_provider
        self.state_store = state_store

    def handle_command(self, text: str) -> Optional[str]:
        if self.state_store is not None:
            with self.state_store.locked():
                return self._handle_command_unlocked(text)
        return self._handle_command_unlocked(text)

    def _handle_command_unlocked(self, text: str) -> Optional[str]:
        normalized = _normalize_feedback_text(text)
        if not normalized:
            return None

        content = self._read_existing_content()
        entries = _extract_feedback_entries(content)

        if normalized in ("查看风格", "风格沉淀", "风格反馈", "当前风格"):
            return _format_feedback_entries(entries)

        if normalized == "风格自检":
            return _format_style_self_check(entries)

        delete_match = re.fullmatch(r"删除风格\s*(\d+)", normalized)
        if delete_match:
            index = int(delete_match.group(1))
            if index < 1 or index > len(entries):
                return "没有第 %d 条风格反馈。别乱点菜单，账本就这么厚。" % index
            removed = entries.pop(index - 1)
            self._write_content(_replace_feedback_entries(content, entries))
            return "已删除第 %d 条风格反馈：%s" % (index, removed)

        if normalized in ("清空风格反馈", "清空风格沉淀"):
            if not entries:
                return "还没有风格反馈可清空。空账本就别擦了。"
            self._write_content(_replace_feedback_entries(content, []))
            return "已清空 %d 条风格反馈，只保留 chichu 的基础人设。" % len(entries)

        return None

    def record_if_feedback(self, text: str) -> Optional[str]:
        if self.state_store is not None:
            with self.state_store.locked():
                return self._record_if_feedback_unlocked(text)
        return self._record_if_feedback_unlocked(text)

    def _record_if_feedback_unlocked(self, text: str) -> Optional[str]:
        normalized = _normalize_feedback_text(text)
        if not normalized:
            return None

        rule = _match_feedback_rule(normalized)
        if rule is None:
            return None

        content = self._read_existing_content()
        fingerprint = "用户反馈「%s」" % normalized
        if fingerprint not in content:
            content = _append_feedback_line(
                content,
                "- %s: %s；处理方式：%s"
                % (self._date_provider().isoformat(), fingerprint, rule.guidance),
            )
            self._write_content(content)

        return rule.reply

    def _read_existing_content(self) -> str:
        if self.state_store is not None:
            return self.state_store.read_document(
                "style", self.style_path, "# chichu STYLE\n"
            )
        try:
            return self.style_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "# chichu STYLE\n"

    def _write_content(self, content: str) -> None:
        if self.state_store is not None:
            self.state_store.write_document("style", self.style_path, content)
            return
        self.style_path.parent.mkdir(parents=True, exist_ok=True)
        self.style_path.write_text(content, encoding="utf-8")


def create_style_memory(
    config: Config, state_store: Optional[object] = None
) -> Optional[StyleMemory]:
    if not config.enable_style_memory:
        return None
    return StyleMemory(Path(config.style_memory_path), state_store=state_store)


def _normalize_feedback_text(text: str) -> str:
    return " ".join(text.strip().split())


def _match_feedback_rule(text: str) -> Optional[StyleFeedbackRule]:
    for rule in STYLE_FEEDBACK_RULES:
        if any(marker in text for marker in rule.markers):
            return rule
    return None


def _append_feedback_line(content: str, line: str) -> str:
    if not content.strip():
        content = "# chichu STYLE\n"

    if STYLE_MEMORY_SECTION not in content:
        content = content.rstrip() + "\n\n" + STYLE_MEMORY_SECTION + "\n"

    return content.rstrip() + "\n" + line + "\n"


def _extract_feedback_entries(content: str) -> list[str]:
    lines = content.splitlines()
    section = _find_style_memory_section(lines)
    if section is None:
        return []

    start, end = section
    entries = []
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if stripped.startswith("- "):
            entries.append(stripped[2:].strip())
    return entries


def _replace_feedback_entries(content: str, entries: Sequence[str]) -> str:
    if not content.strip():
        content = "# chichu STYLE\n"

    lines = content.splitlines()
    section = _find_style_memory_section(lines)
    if section is None:
        lines = lines + ["", STYLE_MEMORY_SECTION]
        section = (len(lines) - 1, len(lines))

    start, end = section
    preserved_section_lines = [
        line for line in lines[start + 1 : end] if not line.strip().startswith("- ")
    ]
    replacement_lines = (
        lines[: start + 1]
        + ["- %s" % entry for entry in entries]
        + preserved_section_lines
        + lines[end:]
    )
    return "\n".join(replacement_lines).rstrip() + "\n"


def _find_style_memory_section(lines: Sequence[str]) -> Optional[tuple[int, int]]:
    for index, line in enumerate(lines):
        if line.strip() != STYLE_MEMORY_SECTION:
            continue

        end = len(lines)
        for next_index in range(index + 1, len(lines)):
            if lines[next_index].startswith("## "):
                end = next_index
                break
        return index, end
    return None


def _format_feedback_entries(entries: Sequence[str]) -> str:
    if not entries:
        return "还没有风格反馈沉淀。chichu 账本空着呢，别急着装大师。"
    lines = ["当前风格反馈沉淀："]
    lines.extend("%d. %s" % (index, entry) for index, entry in enumerate(entries, 1))
    return "\n".join(lines)


def _format_style_self_check(entries: Sequence[str]) -> str:
    if not entries:
        return "风格自检：当前 0 条反馈。她还没学到你的新偏好，别指望凭空进化。"

    recent = list(entries[-3:])
    lines = [
        "风格自检：当前沉淀 %d 条。" % len(entries),
        "最近 %d 条：" % len(recent),
    ]
    lines.extend("%d. %s" % (index, entry) for index, entry in enumerate(recent, 1))
    return "\n".join(lines)
