from typing import Optional


IDENTITY_REPLY = (
    "我是 chichu，你本地 QQ 私聊管家。消息、代码、项目、提醒，"
    "还有那些你拖到快炸的小麻烦，都归我盯着。"
    "你负责别太离谱，我负责把事收拾干净。"
)

IDENTITY_QUESTIONS = (
    "你是谁",
    "你是谁？",
    "介绍一下你自己",
    "自我介绍",
    "chichu是谁",
    "chichu 是谁",
    "chichu是什么",
    "chichu 是什么",
)


def identity_reply(text: str) -> Optional[str]:
    normalized = _normalize(text)
    if not normalized:
        return None
    if normalized in {_normalize(question) for question in IDENTITY_QUESTIONS}:
        return IDENTITY_REPLY
    return None


def _normalize(text: str) -> str:
    return "".join(text.strip().lower().split())
