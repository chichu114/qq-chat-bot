import re
from typing import List, Optional, Tuple

from .models import MemoryItem


MEMORY_SECTION = "## 长期记忆"


def append_to_section(content: str, section: str, line: str) -> str:
    if not content.strip():
        content = "# chichu\n"
    if section not in content:
        content = content.rstrip() + "\n\n" + section + "\n"
    return content.rstrip() + "\n" + line + "\n"


def parse_memories(content: str) -> List[str]:
    return [item.text for item in extract_memory_items(content)]


def extract_memory_items(content: str) -> List[MemoryItem]:
    lines = content.splitlines()
    section = find_markdown_section(lines, MEMORY_SECTION)
    if section is None:
        return []

    start, end = section
    memories = []
    for raw_line in lines[start + 1 : end]:
        match = re.match(r"^- (\d{4}-\d{2}-\d{2}): (.+)$", raw_line.strip())
        if match:
            memories.append(
                MemoryItem(created_at=match.group(1), text=match.group(2).strip())
            )
    return memories


def replace_memory_items(content: str, memories: List[MemoryItem]) -> str:
    if not content.strip():
        content = "# chichu MEMORY\n"

    lines = content.splitlines()
    section = find_markdown_section(lines, MEMORY_SECTION)
    if section is None:
        lines.extend(["", MEMORY_SECTION])
        section = (len(lines) - 1, len(lines))

    start, end = section
    preserved = [
        line
        for line in lines[start + 1 : end]
        if not re.match(r"^- \d{4}-\d{2}-\d{2}: .+$", line.strip())
    ]
    memory_lines = ["- %s: %s" % (item.created_at, item.text) for item in memories]
    updated = lines[: start + 1] + memory_lines + preserved + lines[end:]
    return "\n".join(updated).rstrip() + "\n"


def find_markdown_section(
    lines: List[str], heading: str
) -> Optional[Tuple[int, int]]:
    for index, line in enumerate(lines):
        if line.strip() != heading:
            continue
        end = len(lines)
        for next_index in range(index + 1, len(lines)):
            if lines[next_index].startswith("## "):
                end = next_index
                break
        return index, end
    return None
