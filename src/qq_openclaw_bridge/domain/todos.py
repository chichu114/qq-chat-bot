import re
from typing import List, Optional

from .models import TodoItem


TODO_RE = re.compile(r"^- \[([ x])\] (\d+)$")


def format_todo_text(text: str, due_hint: Optional[str]) -> str:
    if due_hint:
        return "提醒：%s；事项：%s" % (due_hint, text)
    return text


def clean_todo_task_text(text: str) -> str:
    marker = "；事项："
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text


def next_todo_id(content: str) -> int:
    item_ids = [item.item_id for item in parse_todos(content)]
    return max(item_ids, default=0) + 1


def parse_todos(content: str) -> List[TodoItem]:
    items = []
    for line in content.splitlines():
        item = parse_todo_line(line)
        if item:
            items.append(item)
    return items


def parse_todo_line(line: str) -> Optional[TodoItem]:
    parts = [part.strip() for part in line.strip().split(" | ")]
    if len(parts) < 3:
        return None
    match = TODO_RE.match(parts[0])
    if not match:
        return None
    metadata = {}
    for part in parts[3:]:
        if "=" in part:
            key, value = part.split("=", 1)
            metadata[key.strip()] = value.strip()
    return TodoItem(
        status=match.group(1),
        item_id=int(match.group(2)),
        created_at=parts[1],
        text=parts[2],
        metadata=metadata,
    )


def format_todo_line(item: TodoItem) -> str:
    line = "- [%s] %d | %s | %s" % (
        item.status,
        item.item_id,
        item.created_at,
        item.text,
    )
    for key in ("user", "due", "reminded"):
        if key in item.metadata:
            line += " | %s=%s" % (key, item.metadata[key])
    for key in sorted(item.metadata):
        if key not in ("user", "due", "reminded"):
            line += " | %s=%s" % (key, item.metadata[key])
    return line


def replace_todo_item(content: str, replacement: TodoItem) -> str:
    updated = []
    for line in content.splitlines():
        item = parse_todo_line(line)
        if item and item.item_id == replacement.item_id:
            updated.append(format_todo_line(replacement))
        else:
            updated.append(line)
    return "\n".join(updated).rstrip() + "\n"
