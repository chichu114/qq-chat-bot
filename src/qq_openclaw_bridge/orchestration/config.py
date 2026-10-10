from dataclasses import dataclass


@dataclass(frozen=True)
class OrchestrationConfig:
    mode: str
    version: int

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode != "direct":
            raise ValueError("runtime orchestration mode must be direct")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("orchestration config version must be positive")


__all__ = ["OrchestrationConfig"]
