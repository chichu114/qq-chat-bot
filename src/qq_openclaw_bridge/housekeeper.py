import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import Config
from .domain.memory import (
    MEMORY_SECTION,
    append_to_section as _append_to_section,
    extract_memory_items as _extract_memory_items,
    parse_memories as _parse_memories,
    replace_memory_items as _replace_memory_items,
)
from .domain.models import MemoryItem, TodoDraft, TodoItem
from .domain.scheduling import (
    next_recurring_due as _next_recurring_due,
    parse_due_at as _parse_due_at,
    parse_int as _parse_int,
    parse_metadata_datetime as _parse_metadata_datetime,
    parse_recurrence_metadata as _parse_recurrence_metadata,
)
from .domain.todos import (
    clean_todo_task_text as _clean_todo_task_text,
    format_todo_line as _format_todo_line,
    format_todo_text as _format_todo_text,
    next_todo_id as _next_todo_id,
    parse_todo_line as _parse_todo_line,
    parse_todos as _parse_todos,
    replace_todo_item as _replace_todo_item,
)


TODO_SECTION = "## 待办"
DEFAULT_TIMEZONE = "Asia/Shanghai"
TODO_CATEGORIES = ("工作", "生活", "学习", "项目", "其他")


class Housekeeper:
    def __init__(
        self,
        memory_path: Path,
        todo_path: Path,
        date_provider: Callable[[], date] = date.today,
        now_provider: Optional[Callable[[], datetime]] = None,
        timezone_name: str = DEFAULT_TIMEZONE,
        reminder_state_path: Optional[Path] = None,
        state_store: Optional[object] = None,
    ) -> None:
        self.memory_path = Path(memory_path).expanduser()
        self.todo_path = Path(todo_path).expanduser()
        self.reminder_state_path = (
            Path(reminder_state_path).expanduser()
            if reminder_state_path is not None
            else self.todo_path.with_name("REMINDER_STATE.md")
        )
        self._date_provider = date_provider
        self.state_store = state_store
        self.timezone = ZoneInfo(timezone_name)
        self._now_provider = now_provider or (
            lambda: datetime.now(tz=self.timezone)
        )

    def handle(self, text: str, user_id: Optional[int] = None) -> Optional[str]:
        if self.state_store is not None:
            with self.state_store.locked():
                return self._handle_unlocked(text, user_id=user_id)
        return self._handle_unlocked(text, user_id=user_id)

    def _handle_unlocked(
        self, text: str, user_id: Optional[int] = None
    ) -> Optional[str]:
        normalized = _normalize(text)
        if not normalized:
            return None

        memory = _extract_memory_to_record(normalized)
        if memory:
            return self._record_memory(memory, user_id=user_id)

        if _is_memory_list_command(normalized):
            return self._list_memories()

        memory_query = _extract_memory_search_query(normalized)
        if memory_query is not None:
            return self._search_memories(memory_query)

        memory_delete_id = _extract_memory_delete_id(normalized)
        if memory_delete_id is not None:
            return self._delete_memory(memory_delete_id, user_id=user_id)

        if _is_memory_clear_command(normalized):
            return self._clear_memories(user_id=user_id)

        if _is_pause_reminders_command(normalized):
            return self._set_reminders_paused(True)

        if _is_resume_reminders_command(normalized):
            return self._set_reminders_paused(False)

        reschedule = _extract_reschedule_reminder_command(normalized)
        if reschedule is not None:
            return self._reschedule_reminder(
                reschedule[0], reschedule[1], user_id=user_id
            )

        cancel_reminder_id = _extract_cancel_reminder_id(normalized)
        if cancel_reminder_id is not None:
            return self._cancel_reminder(cancel_reminder_id)

        delete_id = _extract_delete_id(normalized)
        if delete_id is not None:
            return self._delete_todo(delete_id)

        modification = _extract_modify_command(normalized)
        if modification is not None:
            return self._modify_todo(modification[0], modification[1], user_id=user_id)

        if _is_clear_completed_command(normalized):
            return self._clear_completed_todos()

        if _is_today_reminders_command(normalized):
            return self._list_today_reminders()

        if _is_next_seven_days_command(normalized):
            return self._list_next_seven_days_reminders()

        completion_id = _extract_completion_id(normalized)
        if completion_id is not None:
            return self._complete_todo(completion_id)

        list_category = _extract_todo_list_category(normalized)
        if list_category is not None or _is_todo_list_command(normalized):
            return self._list_todos(category=list_category)

        todo = _extract_todo_to_record(normalized)
        if todo:
            return self._record_todo(
                todo.text,
                due_hint=todo.due_hint,
                user_id=user_id,
                category=todo.category,
            )

        return None

    def _record_memory(self, text: str, user_id: Optional[int] = None) -> str:
        content = self._read_file(self.memory_path, "# chichu MEMORY\n")
        line = "- %s: %s" % (self._date_provider().isoformat(), text)
        if not any(item.text == text for item in _extract_memory_items(content)):
            content = _append_to_section(content, MEMORY_SECTION, line)
            self._write_file(self.memory_path, content)
            if self.state_store is not None:
                self.state_store.add_confirmed_memory(user_id or 0, text)
            return "记下了。以后我会按这个来，别又装作自己没说过。"
        return "这条已经记过了。你不用重复念，我还没健忘到那程度。"

    def _list_memories(self) -> str:
        content = self._read_file(self.memory_path, "# chichu MEMORY\n")
        memories = _parse_memories(content)
        if not memories:
            return "还没什么长期记忆。你倒是可以先交代几条，别等我读心。"

        lines = ["我现在记着这些："]
        lines.extend("%d. %s" % (index, item) for index, item in enumerate(memories, 1))
        return "\n".join(lines)

    def _search_memories(self, query: str) -> str:
        content = self._read_file(self.memory_path, "# chichu MEMORY\n")
        memories = _extract_memory_items(content)
        lowered_query = query.casefold()
        matches = [
            (index, item)
            for index, item in enumerate(memories, 1)
            if lowered_query in item.text.casefold()
        ]
        if not matches:
            return "没找到包含“%s”的记忆。看来这回不是我忘了，是你没交代。" % query

        lines = ["找到 %d 条相关记忆：" % len(matches)]
        lines.extend("%d. %s" % (index, item.text) for index, item in matches)
        return "\n".join(lines)

    def _delete_memory(self, index: int, user_id: Optional[int] = None) -> str:
        content = self._read_file(self.memory_path, "# chichu MEMORY\n")
        memories = _extract_memory_items(content)
        if index < 1 or index > len(memories):
            return "没有第 %d 条长期记忆。别对着空气删账。" % index

        removed = memories.pop(index - 1)
        self._write_file(self.memory_path, _replace_memory_items(content, memories))
        if self.state_store is not None:
            self.state_store.delete_confirmed_memory(user_id or 0, removed.text)
        return "已删除第 %d 条记忆：%s" % (index, removed.text)

    def _clear_memories(self, user_id: Optional[int] = None) -> str:
        content = self._read_file(self.memory_path, "# chichu MEMORY\n")
        memories = _extract_memory_items(content)
        if not memories:
            return "长期记忆本来就是空的。别擦了，再擦纸都要破了。"

        self._write_file(self.memory_path, _replace_memory_items(content, []))
        if self.state_store is not None:
            self.state_store.clear_confirmed_memories(user_id or 0)
        return "已清空 %d 条长期记忆，其他工作区内容都保留着。" % len(memories)

    def _record_todo(
        self,
        text: str,
        due_hint: Optional[str] = None,
        user_id: Optional[int] = None,
        category: Optional[str] = None,
    ) -> str:
        content = self._read_file(self.todo_path, "# chichu TODO\n")
        active = [item for item in _parse_todos(content) if not item.done]
        display_text = _format_todo_text(text, due_hint)
        metadata: Dict[str, str] = {}
        if category:
            metadata["category"] = category

        now = self._now_provider()
        recurrence = _parse_recurrence_metadata(due_hint)
        if recurrence:
            metadata.update(recurrence)

        due_at = _parse_due_at(due_hint, now, self.timezone)
        if due_at is None and recurrence:
            due_at = _next_recurring_due(recurrence, now, self.timezone)
        if due_at is not None:
            metadata["due"] = due_at.isoformat(timespec="seconds")
            metadata["reminded"] = ""
            if user_id is not None:
                metadata["user"] = str(user_id)

        if any(item.text == display_text for item in active):
            return "这条待办已经在清单里了。别重复下单，我不是备忘录打印机。"

        item_id = _next_todo_id(content)
        line = _format_todo_line(
            TodoItem(
                status=" ",
                item_id=item_id,
                created_at=self._date_provider().isoformat(),
                text=display_text,
                metadata=metadata,
            )
        )
        content = _append_to_section(content, TODO_SECTION, line)
        self._write_file(self.todo_path, content)

        if due_at is not None:
            return "提醒定好了。待办 #%d：%s -> %s。" % (
                item_id,
                due_at.strftime("%Y-%m-%d %H:%M"),
                text,
            )
        if due_hint:
            return "提醒我先记成待办 #%d：%s -> %s。" % (item_id, due_hint, text)
        return "收下。待办 #%d：%s。" % (item_id, text)

    def _list_todos(self, category: Optional[str] = None) -> str:
        content = self._read_file(self.todo_path, "# chichu TODO\n")
        active = [item for item in _parse_todos(content) if not item.done]
        if category:
            active = [item for item in active if item.metadata.get("category") == category]
        if not active:
            if category:
                return "%s分类下没待办。这个角落暂时干净，挺稀奇。" % category
            return "待办空着。难得，先别得意。"

        lines = ["当前待办："]
        for index, item in enumerate(active, 1):
            category_label = (
                "[%s] " % item.metadata["category"]
                if item.metadata.get("category")
                else ""
            )
            lines.append("%d. #%d %s%s" % (index, item.item_id, category_label, item.text))
        return "\n".join(lines)

    def _complete_todo(self, item_id: int) -> str:
        content = self._read_file(self.todo_path, "# chichu TODO\n")
        lines = content.splitlines()
        found = False
        updated: List[str] = []

        for line in lines:
            item = _parse_todo_line(line)
            if item and item.item_id == item_id and not item.done:
                updated.append(
                    _format_todo_line(
                        TodoItem(
                            status="x",
                            item_id=item.item_id,
                            created_at=item.created_at,
                            text=item.text,
                            metadata=item.metadata,
                        )
                    )
                )
                found = True
            else:
                updated.append(line)

        if not found:
            return "没找到待办 #%d。先看清楚编号，别让我跟空气较劲。" % item_id

        self._write_file(self.todo_path, "\n".join(updated).rstrip() + "\n")
        return "处理掉 #%d。你看，事情也不是不能动。" % item_id

    def _delete_todo(self, item_id: int) -> str:
        content = self._read_file(self.todo_path, "# chichu TODO\n")
        lines = content.splitlines()
        found = False
        updated: List[str] = []

        for line in lines:
            item = _parse_todo_line(line)
            if item and item.item_id == item_id:
                found = True
                continue
            updated.append(line)

        if not found:
            return "没找到待办 #%d。删空气这种事，还是留给玄学吧。" % item_id

        self._write_file(self.todo_path, "\n".join(updated).rstrip() + "\n")
        return "删掉 #%d。账本干净一点，人也不至于看着头大。" % item_id

    def _modify_todo(
        self,
        item_id: int,
        replacement: str,
        user_id: Optional[int] = None,
    ) -> str:
        parsed = _extract_todo_to_record(replacement)
        if parsed is not None:
            task_text = parsed.text
            due_hint = parsed.due_hint
            category = parsed.category
        else:
            task_text = replacement
            due_hint = None
            category = None
        display_text = _format_todo_text(task_text, due_hint)
        now = self._now_provider()
        recurrence = _parse_recurrence_metadata(due_hint)
        due_at = _parse_due_at(due_hint, now, self.timezone)
        if due_at is None and recurrence:
            due_at = _next_recurring_due(recurrence, now, self.timezone)

        metadata: Dict[str, str] = {}
        if category:
            metadata["category"] = category
        if recurrence:
            metadata.update(recurrence)
        if due_at is not None:
            metadata["due"] = due_at.isoformat(timespec="seconds")
            metadata["reminded"] = ""
            if user_id is not None:
                metadata["user"] = str(user_id)

        content = self._read_file(self.todo_path, "# chichu TODO\n")
        lines = content.splitlines()
        found = False
        updated: List[str] = []

        for line in lines:
            item = _parse_todo_line(line)
            if item and item.item_id == item_id:
                updated.append(
                    _format_todo_line(
                        TodoItem(
                            status=item.status,
                            item_id=item.item_id,
                            created_at=item.created_at,
                            text=display_text,
                            metadata=metadata,
                        )
                    )
                )
                found = True
            else:
                updated.append(line)

        if not found:
            return "没找到待办 #%d。你先把编号看准，别让我瞎改。" % item_id

        self._write_file(self.todo_path, "\n".join(updated).rstrip() + "\n")
        return "改好了。待办 #%d：%s。" % (item_id, display_text)

    def _clear_completed_todos(self) -> str:
        content = self._read_file(self.todo_path, "# chichu TODO\n")
        lines = content.splitlines()
        cleared = 0
        updated: List[str] = []

        for line in lines:
            item = _parse_todo_line(line)
            if item and item.done:
                cleared += 1
                continue
            updated.append(line)

        self._write_file(self.todo_path, "\n".join(updated).rstrip() + "\n")
        return "清掉 %d 条已完成。终于像个能看的账本了。" % cleared

    def _list_today_reminders(self) -> str:
        today = self._now_provider().astimezone(self.timezone).date()
        content = self._read_file(self.todo_path, "# chichu TODO\n")
        items = []
        for item in _parse_todos(content):
            due_at = _parse_metadata_datetime(item.metadata.get("due"), self.timezone)
            if due_at is not None and due_at.date() == today:
                items.append((item, due_at))

        if not items:
            return "今天没提醒。难得清净，你最好珍惜。"

        lines = ["今天的提醒："]
        for item, due_at in items:
            status = "已提醒" if item.metadata.get("reminded") else "未提醒"
            lines.append(
                "#%d %s %s（%s）"
                % (item.item_id, due_at.strftime("%H:%M"), item.text, status)
            )
        return "\n".join(lines)

    def _list_next_seven_days_reminders(self) -> str:
        now = self._now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.timezone)
        else:
            now = now.astimezone(self.timezone)
        end = now + timedelta(days=7)

        content = self._read_file(self.todo_path, "# chichu TODO\n")
        scheduled = []
        for item in _parse_todos(content):
            if item.done or item.metadata.get("reminded"):
                continue
            due_at = _parse_metadata_datetime(item.metadata.get("due"), self.timezone)
            if due_at is not None and now <= due_at < end:
                scheduled.append((due_at, item))

        if not scheduled:
            return "未来七天没有安排提醒。难得清净，你可别转头又忘事。"

        weekday_names = ("一", "二", "三", "四", "五", "六", "日")
        lines = ["未来七天提醒："]
        for due_at, item in sorted(scheduled, key=lambda value: value[0]):
            repeat_label = _format_repeat_label(item.metadata)
            lines.append(
                "%s 周%s %s #%d %s%s"
                % (
                    due_at.strftime("%m-%d"),
                    weekday_names[due_at.weekday()],
                    due_at.strftime("%H:%M"),
                    item.item_id,
                    _clean_todo_task_text(item.text),
                    repeat_label,
                )
            )
        return "\n".join(lines)

    def _reschedule_reminder(
        self,
        item_id: int,
        due_hint: str,
        user_id: Optional[int] = None,
    ) -> str:
        content = self._read_file(self.todo_path, "# chichu TODO\n")
        target = next(
            (
                item
                for item in _parse_todos(content)
                if item.item_id == item_id and not item.done
            ),
            None,
        )
        if target is None:
            return "没找到可改期的提醒 #%d。编号先看准，别让我给空气排班。" % item_id

        now = self._now_provider()
        recurrence = _parse_recurrence_metadata(due_hint)
        due_at = _parse_due_at(due_hint, now, self.timezone)
        if due_at is None and recurrence:
            due_at = _next_recurring_due(recurrence, now, self.timezone)
        if due_at is None:
            return "没看懂时间“%s”。说清楚点，比如“明天晚上”或“2026-07-20 09:00”。" % due_hint

        metadata = _without_reminder_metadata(target.metadata)
        metadata.update(recurrence)
        metadata["due"] = due_at.isoformat(timespec="seconds")
        metadata["reminded"] = ""
        resolved_user_id = user_id or _parse_int(target.metadata.get("user"))
        if resolved_user_id is not None:
            metadata["user"] = str(resolved_user_id)

        updated = TodoItem(
            status=target.status,
            item_id=target.item_id,
            created_at=target.created_at,
            text=_format_todo_text(_clean_todo_task_text(target.text), due_hint),
            metadata=metadata,
        )
        self._write_file(self.todo_path, _replace_todo_item(content, updated))
        return "改期完成。提醒 #%d -> %s。总算不是临到点才想起来。" % (
            item_id,
            due_at.strftime("%Y-%m-%d %H:%M"),
        )

    def _cancel_reminder(self, item_id: int) -> str:
        content = self._read_file(self.todo_path, "# chichu TODO\n")
        target = next(
            (
                item
                for item in _parse_todos(content)
                if item.item_id == item_id and not item.done
            ),
            None,
        )
        if target is None:
            return "没找到可取消的提醒 #%d。别拿不存在的日程考我。" % item_id
        if not target.metadata.get("due") and not target.metadata.get("repeat"):
            return "待办 #%d 本来就没设提醒。你这是取消了个寂寞。" % item_id

        updated = TodoItem(
            status=target.status,
            item_id=target.item_id,
            created_at=target.created_at,
            text=_clean_todo_task_text(target.text),
            metadata=_without_reminder_metadata(target.metadata),
        )
        self._write_file(self.todo_path, _replace_todo_item(content, updated))
        return "提醒 #%d 已取消，待办还保留着。别误会，我只是闭嘴，不是替你做完。" % item_id

    def _set_reminders_paused(self, paused: bool) -> str:
        status = "paused" if paused else "active"
        self._write_file(self.reminder_state_path, "status=%s\n" % status)
        if paused:
            return "提醒已暂停。行吧，先让你耳根清净一会儿。"
        return "提醒已恢复。该叫你的时候，我还是会叫。"

    def _read_file(self, path: Path, default: str) -> str:
        document_name = self._document_name(path)
        if self.state_store is not None and document_name is not None:
            return self.state_store.read_document(document_name, path, default)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default

    def _write_file(self, path: Path, content: str) -> None:
        document_name = self._document_name(path)
        if self.state_store is not None and document_name is not None:
            self.state_store.write_document(document_name, path, content)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _document_name(self, path: Path) -> Optional[str]:
        target = Path(path).expanduser()
        for name, candidate in (
            ("memory", self.memory_path),
            ("todo", self.todo_path),
            ("reminder_state", self.reminder_state_path),
        ):
            if target == candidate:
                return name
        return None


def create_housekeeper(
    config: Config, state_store: Optional[object] = None
) -> Optional[object]:
    if not config.enable_housekeeper:
        return None
    from .domain.housekeeper import HousekeeperService

    return HousekeeperService(
        Housekeeper(
            memory_path=Path(config.housekeeper_memory_path),
            todo_path=Path(config.housekeeper_todo_path),
            reminder_state_path=Path(config.reminder_state_path),
            state_store=state_store,
        )
    )


def _normalize(text: str) -> str:
    return " ".join(text.strip().split())


def _extract_memory_to_record(text: str) -> Optional[str]:
    for prefix in ("记住：", "记住:", "帮我记住：", "帮我记住:"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip() or None

    if text.startswith("记住 "):
        return text[len("记住 ") :].strip() or None

    return None


def _is_memory_list_command(text: str) -> bool:
    return text in (
        "我的记忆",
        "查看记忆",
        "长期记忆",
        "记忆列表",
        "列一下记忆",
        "你记住了什么",
    )


def _extract_memory_search_query(text: str) -> Optional[str]:
    match = re.fullmatch(r"(?:搜索|查找)记忆\s*[：:]?\s*(.+)", text)
    if not match:
        return None
    return match.group(1).strip() or None


def _extract_memory_delete_id(text: str) -> Optional[int]:
    match = re.fullmatch(r"(?:删除|删掉|移除)记忆\s*#?(\d+)", text)
    if not match:
        return None
    return int(match.group(1))


def _is_memory_clear_command(text: str) -> bool:
    return text in ("清空记忆", "清除记忆", "清空长期记忆")


def _is_pause_reminders_command(text: str) -> bool:
    return text in ("暂停提醒", "停止提醒", "别提醒了")


def _is_resume_reminders_command(text: str) -> bool:
    return text in ("恢复提醒", "继续提醒", "开启提醒")


def _extract_reschedule_reminder_command(text: str) -> Optional[Tuple[int, str]]:
    patterns = (
        r"^(?:把)?(?:提醒|待办)\s*#?(\d+)\s*(?:改到|改为|延到|延期到)\s*(.+)$",
        r"^改期提醒\s*#?(\d+)\s*(?:到|为|[：:])\s*(.+)$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return int(match.group(1)), match.group(2).strip()
    return None


def _extract_cancel_reminder_id(text: str) -> Optional[int]:
    match = re.fullmatch(r"(?:取消|关闭)提醒\s*#?(\d+)", text)
    if not match:
        return None
    return int(match.group(1))


def _is_next_seven_days_command(text: str) -> bool:
    return text in (
        "未来七天提醒",
        "未来7天提醒",
        "查看未来七天提醒",
        "未来一周提醒",
    )


def _extract_delete_id(text: str) -> Optional[int]:
    match = re.match(r"^(?:删除|删掉|移除)待办\s*#?(\d+)$", text)
    if not match:
        return None
    return int(match.group(1))


def _extract_modify_command(text: str) -> Optional[Tuple[int, str]]:
    match = re.match(r"^(?:修改|改)待办\s*#?(\d+)\s*[：:]\s*(.+)$", text)
    if not match:
        return None
    replacement = match.group(2).strip()
    if not replacement:
        return None
    return int(match.group(1)), replacement


def _is_clear_completed_command(text: str) -> bool:
    return text in ("清空已完成", "清理已完成", "删除已完成")


def _is_today_reminders_command(text: str) -> bool:
    return text in ("查看今天提醒", "今天提醒", "今日提醒", "今天有什么提醒")


def _extract_completion_id(text: str) -> Optional[int]:
    match = re.match(r"^(?:完成|搞定|办完)待办\s*#?(\d+)$", text)
    if not match:
        return None
    return int(match.group(1))


def _is_todo_list_command(text: str) -> bool:
    return text in ("待办列表", "我的待办", "列一下待办", "还有什么待办")


def _extract_todo_list_category(text: str) -> Optional[str]:
    match = re.match(r"^(?:待办列表|我的待办|列一下待办)\s+(.+)$", text)
    if not match:
        return None
    return _normalize_category(match.group(1))


def _extract_todo_to_record(text: str) -> Optional[TodoDraft]:
    category_match = re.match(
        r"^(%s)待办[：:]\s*(.+)$" % "|".join(TODO_CATEGORIES),
        text,
    )
    if category_match:
        task = category_match.group(2).strip()
        return _draft_from_task(task, default_category=category_match.group(1))

    for prefix in ("待办：", "待办:", "todo:", "TODO:"):
        if text.startswith(prefix):
            task = text[len(prefix) :].strip()
            return _draft_from_task(task) if task else None

    if "提醒我" not in text:
        return None

    before, after = text.split("提醒我", 1)
    before = before.strip()
    after = after.strip()
    if not after:
        return None

    if before:
        task_text, category = _extract_category_from_task(after)
        return TodoDraft(text=task_text, due_hint=before, category=category)

    due_hint, task = _split_leading_due_hint(after)
    if not task:
        task_text, category = _extract_category_from_task(after)
        return TodoDraft(text=task_text, due_hint=due_hint, category=category)
    task_text, category = _extract_category_from_task(task)
    return TodoDraft(text=task_text, due_hint=due_hint, category=category)


def _draft_from_task(
    text: str,
    default_category: Optional[str] = None,
) -> Optional[TodoDraft]:
    task_text, category = _extract_category_from_task(text, default_category)
    if not task_text:
        return None
    return TodoDraft(text=task_text, category=category)


def _extract_category_from_task(
    text: str,
    default_category: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    category = default_category
    task = text.strip()

    inline_match = re.match(r"^(%s)[：:]\s*(.+)$" % "|".join(TODO_CATEGORIES), task)
    if inline_match:
        category = inline_match.group(1)
        task = inline_match.group(2).strip()

    tag_match = re.search(r"\s#(%s)$" % "|".join(TODO_CATEGORIES), task)
    if tag_match:
        category = tag_match.group(1)
        task = task[: tag_match.start()].strip()

    return task, category


def _normalize_category(raw: str) -> Optional[str]:
    value = raw.strip().lstrip("#")
    return value if value in TODO_CATEGORIES else None


def _split_leading_due_hint(text: str) -> Tuple[Optional[str], str]:
    due_patterns = (
        "后天晚上",
        "后天早上",
        "明天晚上",
        "明天早上",
        "今天晚上",
        "今天早上",
        "今晚",
        "明晚",
        "明早",
        "明天",
        "后天",
        "今天",
        "晚上",
        "早上",
        "上午",
        "中午",
        "下午",
    )
    for prefix in due_patterns:
        if text.startswith(prefix):
            return prefix, text[len(prefix) :].strip()

    match = re.match(
        r"^((?:周|星期)[一二三四五六日天]|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:\s+[0-9]{1,2}[:：][0-9]{1,2})?|[0-9]{1,2}月[0-9]{1,2}[日号]?(?:\s*[0-9]{1,2}[:：点][0-9]{0,2})?)\s*(.*)$",
        text,
    )
    if match:
        return match.group(1), match.group(2).strip()

    return None, text


def _without_reminder_metadata(metadata: Dict[str, str]) -> Dict[str, str]:
    reminder_keys = {"user", "due", "reminded"}
    return {
        key: value
        for key, value in metadata.items()
        if key not in reminder_keys and not key.startswith("repeat")
    }


def _format_repeat_label(metadata: Dict[str, str]) -> str:
    labels = {
        "daily": "（每天）",
        "weekly": "（每周）",
        "monthly": "（每月）",
    }
    return labels.get(metadata.get("repeat"), "")
