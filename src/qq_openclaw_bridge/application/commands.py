from typing import Callable, Optional, Protocol, Sequence

from ..models import AgentRequest


class CommandHandler(Protocol):
    def handle(self, request: AgentRequest) -> Optional[str]:
        ...


class CallableCommandHandler:
    def __init__(
        self, callback: Callable[[AgentRequest], Optional[str]]
    ) -> None:
        self._callback = callback

    def handle(self, request: AgentRequest) -> Optional[str]:
        return self._callback(request)


class CommandChain:
    def __init__(self, handlers: Sequence[CommandHandler]) -> None:
        self._handlers = tuple(handlers)

    def handle(self, request: AgentRequest) -> Optional[str]:
        for handler in self._handlers:
            reply = handler.handle(request)
            if reply is not None:
                return reply
        return None
