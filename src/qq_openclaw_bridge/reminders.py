from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from calendar import monthrange
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from .config import Config
from .domain.models import TodoItem
from .domain.todos import (
    format_todo_line as _format_todo_line,
    parse_todo_line as _parse_todo_line,
)


DEFAULT_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class ReminderNotification:
    todo_id: Optional[int]
    user_id: int
    message: str
    due_at: datetime
    kind: str = "todo"

    @property
    def notification_id(self) -> str:
        if self.todo_id is not None:
            return str(self.todo_id)
        return self.kind


class ReminderStore:
    def __init__(
        self,
        todo_path: Path,
        timezone_name: str = DEFAULT_TIMEZONE,
        state_path: Optional[Path] = None,
        quiet_hours_start: str = "00:00",
        quiet_hours_end: str = "08:30",
        proactive_user_ids: Optional[Sequence[int]] = None,
        enable_daily_briefing: bool = True,
        daily_briefing_time: str = "09:00",
        enable_evening_review: bool = True,
        evening_review_time: str = "23:00",
        state_store: Optional[object] = None,
    ) -> None:
        self.todo_path = Path(todo_path).expanduser()
        self.state_path = (
            Path(state_path).expanduser()
            if state_path is not None
            else self.todo_path.with_name("REMINDER_STATE.md")
        )
        self.timezone = ZoneInfo(timezone_name)
        self.quiet_hours_start = quiet_hours_start
        self.quiet_hours_end = quiet_hours_end
        self.proactive_user_ids = list(proactive_user_ids or [])
        self.enable_daily_briefing = enable_daily_briefing
        self.daily_briefing_time = daily_briefing_time
        self.enable_evening_review = enable_evening_review
        self.evening_review_time = evening_review_time
        self.state_store = state_store

    def collect_due(self, now: datetime) -> List[ReminderNotification]:
        if self.state_store is not None:
            with self.state_store.locked():
                return self._collect_due_unlocked(now)
        return self._collect_due_unlocked(now)

    def _collect_due_unlocked(self, now: datetime) -> List[ReminderNotification]:
        if self._is_paused():
            return []

        now = _ensure_timezone(now, self.timezone)
        if _is_quiet_time(now, self.quiet_hours_start, self.quiet_hours_end):
            return []

        reminders = []
        for item in self._read_todos():
            reminder = self._notification_for_item(item, now)
            if reminder is not None:
                reminders.append(reminder)
        reminders.extend(self._scheduled_notifications(now))
        return reminders

    def mark_sent(self, reminder: ReminderNotification, now: datetime) -> None:
        if self.state_store is not None:
            with self.state_store.locked():
                self._mark_sent_unlocked(reminder, now)
                return
        self._mark_sent_unlocked(reminder, now)

    def _mark_sent_unlocked(
        self, reminder: ReminderNotification, now: datetime
    ) -> None:
        now = _ensure_timezone(now, self.timezone)
        if reminder.kind in ("daily_briefing", "evening_review"):
            state = self._read_state()
            state["%s_sent" % reminder.kind] = now.date().isoformat()
            self._write_state(state)
            return

        lines = self._read_lines()
        updated = []
        for line in lines:
            item = _parse_todo_line(line)
            if item and item.item_id == reminder.todo_id:
                metadata = dict(item.metadata)
                next_due = _next_recurring_due(metadata, now, self.timezone)
                if next_due is not None:
                    metadata["due"] = next_due.isoformat(timespec="seconds")
                    metadata["reminded"] = ""
                else:
                    metadata["reminded"] = now.isoformat(timespec="seconds")
                updated.append(
                    _format_todo_line(
                        TodoItem(
                            status=item.status,
                            item_id=item.item_id,
                            created_at=item.created_at,
                            text=item.text,
                            metadata=metadata,
                        )
                    )
                )
            else:
                updated.append(line)
        self._write_todo_content("\n".join(updated).rstrip() + "\n")

    def _notification_for_item(
        self,
        item: TodoItem,
        now: datetime,
    ) -> Optional[ReminderNotification]:
        if item.done or item.metadata.get("reminded"):
            return None

        user_id = _parse_int(item.metadata.get("user"))
        due_at = _parse_datetime(item.metadata.get("due"), self.timezone)
        if user_id is None or due_at is None or due_at > now:
            return None

        return ReminderNotification(
            todo_id=item.item_id,
            user_id=user_id,
            message="chichu 提醒你：%s。该办了，别又假装没看见。" % _clean_task_text(item.text),
            due_at=due_at,
        )

    def _scheduled_notifications(self, now: datetime) -> List[ReminderNotification]:
        user_ids = self._scheduled_user_ids()
        if not user_ids:
            return []

        state = self._read_state()
        notifications = []
        today_key = now.date().isoformat()

        if (
            self.enable_daily_briefing
            and state.get("daily_briefing_sent") != today_key
            and _time_reached(now, self.daily_briefing_time)
        ):
            message = self._daily_briefing_message(now)
            notifications.extend(
                ReminderNotification(
                    todo_id=None,
                    user_id=user_id,
                    message=message,
                    due_at=now,
                    kind="daily_briefing",
                )
                for user_id in user_ids
            )

        if (
            self.enable_evening_review
            and state.get("evening_review_sent") != today_key
            and _time_reached(now, self.evening_review_time)
        ):
            message = self._evening_review_message()
            notifications.extend(
                ReminderNotification(
                    todo_id=None,
                    user_id=user_id,
                    message=message,
                    due_at=now,
                    kind="evening_review",
                )
                for user_id in user_ids
            )

        return notifications

    def _scheduled_user_ids(self) -> List[int]:
        return self.proactive_user_ids

    def _daily_briefing_message(self, now: datetime) -> str:
        active = [item for item in self._read_todos() if not item.done]
        today_reminders = []
        for item in active:
            due_at = _parse_datetime(item.metadata.get("due"), self.timezone)
            if due_at is not None and due_at.date() == now.date():
                today_reminders.append(item)

        lines = ["chichu 早报："]
        if today_reminders:
            lines.append("今天提醒：")
            for item in today_reminders[:5]:
                lines.append("- %s" % _clean_task_text(item.text))
        else:
            lines.append("今天没定时提醒。难得清净，别拿来发呆。")

        if active:
            lines.append("当前待办：")
            for item in active[:5]:
                category = "[%s] " % item.metadata["category"] if item.metadata.get("category") else ""
                lines.append("- #%d %s%s" % (item.item_id, category, _clean_task_text(item.text)))
        else:
            lines.append("当前待办为空。不错，今天别立刻把它填满。")

        return "\n".join(lines)

    def _evening_review_message(self) -> str:
        active = [item for item in self._read_todos() if not item.done]
        return (
            "chichu 晚间复盘：今天还剩 %d 条待办。"
            "完成了就回我“完成待办 #编号”，别让它们在清单里过夜装睡。"
            % len(active)
        )

    def _read_todos(self) -> List[TodoItem]:
        return [
            item
            for item in (_parse_todo_line(line) for line in self._read_lines())
            if item is not None
        ]

    def _read_lines(self) -> List[str]:
        if self.state_store is not None:
            content = self.state_store.read_document(
                "todo", self.todo_path, "# chichu TODO\n\n## 待办\n"
            )
            return content.splitlines()
        try:
            return self.todo_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def _is_paused(self) -> bool:
        return self._read_state().get("status") == "paused"

    def _read_state(self) -> Dict[str, str]:
        if self.state_store is not None:
            content = self.state_store.read_document(
                "reminder_state", self.state_path, "status=active\n"
            )
        else:
            try:
                content = self.state_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {}

        state: Dict[str, str] = {}
        for line in content.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            state[key.strip()] = value.strip()
        return state

    def _write_state(self, state: Dict[str, str]) -> None:
        lines = ["%s=%s" % (key, state[key]) for key in sorted(state)]
        content = "\n".join(lines).rstrip() + "\n"
        if self.state_store is not None:
            self.state_store.write_document(
                "reminder_state", self.state_path, content
            )
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(content, encoding="utf-8")

    def _write_todo_content(self, content: str) -> None:
        if self.state_store is not None:
            self.state_store.write_document("todo", self.todo_path, content)
            return
        self.todo_path.parent.mkdir(parents=True, exist_ok=True)
        self.todo_path.write_text(content, encoding="utf-8")


def create_reminder_store(
    config: Config, state_store: Optional[object] = None
) -> Optional[ReminderStore]:
    if not config.enable_reminder_push:
        return None
    return ReminderStore(
        Path(config.housekeeper_todo_path),
        state_path=Path(config.reminder_state_path),
        quiet_hours_start=config.quiet_hours_start,
        quiet_hours_end=config.quiet_hours_end,
        proactive_user_ids=sorted(config.allowed_qq_users),
        enable_daily_briefing=config.enable_daily_briefing,
        daily_briefing_time=config.daily_briefing_time,
        enable_evening_review=config.enable_evening_review,
        evening_review_time=config.evening_review_time,
        state_store=state_store,
    )


def _ensure_timezone(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _parse_datetime(raw: Optional[str], timezone: ZoneInfo) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _ensure_timezone(parsed, timezone)


def _parse_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _clean_task_text(text: str) -> str:
    marker = "；事项："
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text


def _next_recurring_due(
    metadata: dict,
    now: datetime,
    timezone: ZoneInfo,
) -> Optional[datetime]:
    now = _ensure_timezone(now, timezone)
    repeat_time = _parse_repeat_time(metadata.get("repeat_time"))
    if repeat_time is None:
        return None

    repeat = metadata.get("repeat")
    if repeat == "daily":
        candidate = datetime.combine(now.date(), repeat_time, tzinfo=timezone)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if repeat == "weekly":
        weekday = _parse_int(metadata.get("repeat_weekday"))
        if weekday is None:
            return None
        days = (weekday - now.isoweekday()) % 7
        candidate = datetime.combine(
            now.date() + timedelta(days=days),
            repeat_time,
            tzinfo=timezone,
        )
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if repeat == "monthly":
        month_day = _parse_int(metadata.get("repeat_month_day"))
        if month_day is None:
            return None
        candidate = _monthly_candidate(now.year, now.month, month_day, repeat_time, timezone)
        if candidate <= now:
            year = now.year + 1 if now.month == 12 else now.year
            month = 1 if now.month == 12 else now.month + 1
            candidate = _monthly_candidate(year, month, month_day, repeat_time, timezone)
        return candidate

    return None


def _monthly_candidate(
    year: int,
    month: int,
    month_day: int,
    repeat_time: time,
    timezone: ZoneInfo,
) -> datetime:
    day = min(month_day, monthrange(year, month)[1])
    return datetime.combine(date(year, month, day), repeat_time, tzinfo=timezone)


def _parse_repeat_time(raw: Optional[str]) -> Optional[time]:
    if not raw:
        return None
    parts = raw.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _is_quiet_time(
    now: datetime,
    quiet_hours_start: str,
    quiet_hours_end: str,
) -> bool:
    start = _parse_clock_time(quiet_hours_start)
    end = _parse_clock_time(quiet_hours_end)
    if start is None or end is None or start == end:
        return False

    current = now.timetz().replace(tzinfo=None)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _parse_clock_time(raw: str) -> Optional[time]:
    parts = raw.strip().split(":", 1)
    if len(parts) != 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _time_reached(now: datetime, raw: str) -> bool:
    target = _parse_clock_time(raw)
    if target is None:
        return False
    current = now.timetz().replace(tzinfo=None)
    return current >= target
