# Style Management B4 Design

## Goal

Give chichu local QQ commands for inspecting and managing `STYLE.md` feedback entries without calling OpenClaw or consuming API quota.

## Commands

- `查看风格` and `风格沉淀`: list numbered entries from the `## 用户反馈沉淀` section.
- `风格自检`: summarize how many feedback entries exist and show the latest three.
- `删除风格 1`: remove the numbered feedback entry from `## 用户反馈沉淀`.
- `清空风格反馈`: remove all feedback entries from `## 用户反馈沉淀` while preserving B1 persona/style sections.

## Architecture

B4 extends `StyleMemory` with a local command handler, `handle_command(text: str) -> Optional[str]`. The bridge will call this handler after identity and housekeeper commands, and before passive feedback recording or OpenClaw.

`StyleMemory` remains responsible for all `STYLE.md` parsing and writing so bridge logic stays thin. The command handler reads only the `## 用户反馈沉淀` section and rewrites only list entries in that section.

## Error Handling

If `STYLE.md` does not exist or contains no feedback entries, chichu replies with a short empty-state message. Invalid indexes such as `删除风格 99` return a local error message and do not modify the file.

## Testing

Unit tests cover listing, self-check, deleting one entry, clearing entries, empty-state behavior, invalid delete indexes, and bridge routing that avoids calling the agent for B4 commands.
