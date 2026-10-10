from typing import Optional, Protocol


class HousekeeperHandler(Protocol):
    def handle(self, text: str, user_id: Optional[int] = None) -> Optional[str]:
        ...


class HousekeeperService:
    """Stable command-service port around the current housekeeper behavior."""

    def __init__(self, handler: HousekeeperHandler) -> None:
        self._handler = handler

    def handle(self, text: str, user_id: Optional[int] = None) -> Optional[str]:
        return self._handler.handle(text, user_id=user_id)
