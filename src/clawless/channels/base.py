"""Base types for messaging channels."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class InboundMessage:
    """A parsed inbound message from any channel."""

    sender_id: str  # e.g. "+1234567890"
    chat_id: str  # channel-specific, e.g. "whatsapp:+1234567890"
    content: str  # text body, may include [image: path] tags
    media_files: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"whatsapp:{self.sender_id}"


class Channel(Protocol):
    """Interface every channel must implement."""

    name: str

    async def send_text(self, chat_id: str, text: str) -> None: ...

    async def send_media(
        self, chat_id: str, file_path: str, caption: str = ""
    ) -> None: ...
