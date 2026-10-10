import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional
from zoneinfo import ZoneInfo


WEEKDAY_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
}


def parse_due_at(
    due_hint: Optional[str], now: datetime, timezone: ZoneInfo
) -> Optional[datetime]:
    if not due_hint:
        return None
    now = (
        now.replace(tzinfo=timezone)
        if now.tzinfo is None
        else now.astimezone(timezone)
    )
    hint = due_hint.strip().replace("：", ":")
    absolute = parse_absolute_due_at(hint, now, timezone)
    if absolute is not None:
        return absolute

    day_offset = 0
    if "后天" in hint:
        day_offset = 2
    elif "明天" in hint or "明晚" in hint or "明早" in hint:
        day_offset = 1
    due_date = now.date() + timedelta(days=day_offset)
    due_time = time_for_hint(hint)
    if due_time is None:
        return None
    due_at = datetime.combine(due_date, due_time, tzinfo=timezone)
    if due_at <= now and day_offset == 0:
        due_at += timedelta(days=1)
    return due_at if has_relative_due_marker(hint) else None


def parse_recurrence_metadata(due_hint: Optional[str]) -> Dict[str, str]:
    if not due_hint:
        return {}
    hint = due_hint.strip().replace("：", ":")
    daily = re.match(r"^每天\s*(?P<time>\d{1,2}(?:[:点]\d{0,2})?)?$", hint)
    if daily:
        return {
            "repeat": "daily",
            "repeat_time": normalize_time_text(daily.group("time")),
        }
    weekly = re.match(
        r"^每(?:周|星期)(?P<weekday>[一二三四五六日天])\s*(?P<time>\d{1,2}(?:[:点]\d{0,2})?)?$",
        hint,
    )
    if weekly:
        return {
            "repeat": "weekly",
            "repeat_weekday": str(WEEKDAY_NUMBERS[weekly.group("weekday")]),
            "repeat_time": normalize_time_text(weekly.group("time")),
        }
    monthly = re.match(
        r"^每月(?P<day>\d{1,2})(?:日|号)?\s*(?P<time>\d{1,2}(?:[:点]\d{0,2})?)?$",
        hint,
    )
    if monthly:
        return {
            "repeat": "monthly",
            "repeat_month_day": str(int(monthly.group("day"))),
            "repeat_time": normalize_time_text(monthly.group("time")),
        }
    return {}


def next_recurring_due(
    metadata: Dict[str, str], now: datetime, timezone: ZoneInfo
) -> Optional[datetime]:
    now = (
        now.replace(tzinfo=timezone)
        if now.tzinfo is None
        else now.astimezone(timezone)
    )
    repeat_time = parse_repeat_time(metadata.get("repeat_time"))
    if repeat_time is None:
        return None
    repeat = metadata.get("repeat")
    if repeat == "daily":
        candidate = datetime.combine(now.date(), repeat_time, tzinfo=timezone)
        return candidate + timedelta(days=1) if candidate <= now else candidate
    if repeat == "weekly":
        weekday = parse_int(metadata.get("repeat_weekday"))
        if weekday is None:
            return None
        days = (weekday - now.isoweekday()) % 7
        candidate = datetime.combine(
            now.date() + timedelta(days=days), repeat_time, tzinfo=timezone
        )
        return candidate + timedelta(days=7) if candidate <= now else candidate
    if repeat == "monthly":
        month_day = parse_int(metadata.get("repeat_month_day"))
        if month_day is None:
            return None
        candidate = monthly_candidate(
            now.year, now.month, month_day, repeat_time, timezone
        )
        if candidate <= now:
            year = now.year + 1 if now.month == 12 else now.year
            month = 1 if now.month == 12 else now.month + 1
            candidate = monthly_candidate(
                year, month, month_day, repeat_time, timezone
            )
        return candidate
    return None


def monthly_candidate(
    year: int,
    month: int,
    month_day: int,
    repeat_time: time,
    timezone: ZoneInfo,
) -> datetime:
    day = min(month_day, monthrange(year, month)[1])
    return datetime.combine(date(year, month, day), repeat_time, tzinfo=timezone)


def normalize_time_text(raw: Optional[str]) -> str:
    if not raw:
        return "09:00"
    value = raw.replace("点", ":").strip(":")
    if ":" in value:
        hour, minute = value.split(":", 1)
        minute = minute or "0"
    else:
        hour, minute = value, "0"
    return "%02d:%02d" % (int(hour), int(minute))


def parse_repeat_time(raw: Optional[str]) -> Optional[time]:
    if not raw:
        return None
    match = re.match(r"^(\d{1,2}):(\d{1,2})$", raw)
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def parse_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_absolute_due_at(
    hint: str, now: datetime, timezone: ZoneInfo
) -> Optional[datetime]:
    match = re.search(
        r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?:\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{1,2}))?)?",
        hint,
    )
    if match:
        try:
            return datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour") or 9),
                int(match.group("minute") or 0),
                tzinfo=timezone,
            )
        except ValueError:
            return None
    match = re.search(
        r"(?P<month>\d{1,2})月(?P<day>\d{1,2})(?:日|号)?(?:\s*(?P<hour>\d{1,2})(?:[:点](?P<minute>\d{1,2})?)?)?",
        hint,
    )
    if not match:
        return None
    try:
        due_at = datetime(
            now.year,
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour") or 9),
            int(match.group("minute") or 0),
            tzinfo=timezone,
        )
    except ValueError:
        return None
    return due_at.replace(year=now.year + 1) if due_at <= now else due_at


def parse_metadata_datetime(
    raw: Optional[str], timezone: ZoneInfo
) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=timezone)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone)
    )


def time_for_hint(hint: str) -> Optional[time]:
    match = re.search(
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{1,2})|点(?P<point_minute>\d{1,2})?分?)",
        hint,
    )
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or match.group("point_minute") or 0)
        if any(marker in hint for marker in ("下午", "晚上", "晚")) and hour < 12:
            hour += 12
        elif "中午" in hint and hour < 11:
            hour += 12
        elif any(marker in hint for marker in ("早上", "上午")) and hour == 12:
            hour = 0
        try:
            return time(hour, minute)
        except ValueError:
            return None
    if "早" in hint or "上午" in hint:
        return time(9, 0)
    if "中午" in hint:
        return time(12, 0)
    if "下午" in hint:
        return time(15, 0)
    if "晚" in hint or "晚上" in hint:
        return time(20, 0)
    return time(9, 0)


def has_relative_due_marker(hint: str) -> bool:
    return any(
        marker in hint
        for marker in (
            "今天",
            "今晚",
            "明天",
            "明晚",
            "明早",
            "后天",
            "早上",
            "上午",
            "中午",
            "下午",
            "晚上",
        )
    )
