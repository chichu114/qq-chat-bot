from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from ..onebot import extract_text


@dataclass(frozen=True)
class QuoteArtifact:
    text: str
    status: str
    source_message_id: Optional[str]

    @property
    def embedded_text(self) -> str:
        return self.text


class QuoteResolver:
    def __init__(self, rpc_client) -> None:
        self._rpc_client = rpc_client

    async def resolve(self, envelope, websocket) -> QuoteArtifact:
        quote = envelope.quote
        if quote is None:
            return QuoteArtifact("", "absent", None)
        source_id = str(quote.message_id)
        if isinstance(quote.embedded_text, str) and quote.embedded_text.strip():
            return QuoteArtifact(quote.embedded_text, "available", source_id)
        try:
            response = await self._rpc_client.request(
                websocket,
                "get_msg",
                {"message_id": quote.message_id},
                timeout=5,
            )
            text = self._response_text(response)
        except Exception:
            return QuoteArtifact("", "unavailable", source_id)
        return QuoteArtifact(text, "available", source_id)

    @staticmethod
    def _response_text(response) -> str:
        if not isinstance(response, Mapping) or response.get("status") != "ok":
            raise ValueError("quote unavailable")
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("quote unavailable")
        text = extract_text(data.get("message"))
        if text is None:
            raise ValueError("quote unavailable")
        return text


__all__ = ["QuoteArtifact", "QuoteResolver"]
