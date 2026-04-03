"""Base types for messaging channels.

Each messaging platform (WhatsApp, Telegram, etc.) implements the Channel
protocol. The sender identity is always channel-namespaced — Twilio uses
"whatsapp:+1234567890", Telegram uses "telegram:123456", etc. This means
the sender string is globally unique across channels without needing a
separate channel prefix, and doubles as the session key in AgentManager
and the reply address in Channel.send(). If there is any scope of 
prefix confusion based on the sender id, the channel implementation must
implement a name-spacing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class InboundMessage:
    """A parsed inbound message from any channel.

    The sender field is the channel-namespaced identity as provided by the
    platform (e.g. "whatsapp:+1234567890"). It serves as both the unique
    user identifier and the reply address — no separate chat_id needed.
    """

    sender: str  # channel-namespaced, e.g. "whatsapp:+1234567890"
    sender_name: str = ""  # display name if available (e.g. WhatsApp ProfileName)
    content: str = ""  # text body, may include [image: path] tags
    media_files: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class Channel(Protocol):
    """Interface every channel must implement.

    A single send() method handles both text and media. Channels that
    support sending text + media together (e.g. Telegram) can do so in
    one API call. Channels that don't (e.g. Twilio WhatsApp, which ignores
    the body for video/audio/docs) send them as separate messages.

    formatting_instructions is included in the prompt sent to Claude so it
    outputs text compatible with the channel's formatting rules natively,
    avoiding the need for post-processing.
    """

    name: str
    formatting_instructions: str

    async def send(
        self, to: str, text: str = "", media: list[str] | None = None
    ) -> None: ...
