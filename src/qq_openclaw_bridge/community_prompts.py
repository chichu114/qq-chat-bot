"""Shared, versioned prompt contracts for community-specific models."""


KANAGAWAWAVE_V2_PROFILE_KEY = "kanagawawave-v2-model-style"
KANAGAWAWAVE_V2_INSTRUCTION = (
    "[COMMUNITY=KANAGAWAVE] "
    "以该社区的常见语气自然回复下面的内容。只输出回复正文。"
)


__all__ = (
    "KANAGAWAWAVE_V2_INSTRUCTION",
    "KANAGAWAWAVE_V2_PROFILE_KEY",
)
