"""Custom MCP tools for the clawless agent."""

from __future__ import annotations

import logging

from claude_agent_sdk import create_sdk_mcp_server, tool

from clawless.channels.base import Channel

logger = logging.getLogger(__name__)

# Per-request context — set before each query by AgentManager.process_message().
# Safe with concurrent senders: per-sender lock serializes processing, and
# the tool handler runs within the same async task as the query.
_ctx: dict = {"channel": None, "sender": "", "sent_in_turn": False, "send_count": 0}

# Max send_message calls allowed per process_message turn.
# Prevents runaway loops where the agent keeps calling send_message.
MAX_SENDS_PER_TURN = 10


def set_context(channel: Channel, sender: str) -> None:
    """Bind channel and sender for the current turn."""
    _ctx["channel"] = channel
    _ctx["sender"] = sender
    _ctx["sent_in_turn"] = False
    _ctx["send_count"] = 0


def was_sent_in_turn() -> bool:
    """Return True if send_message was called during the current turn."""
    return _ctx["sent_in_turn"]


@tool(
    "send_message",
    "Send a message or reply to the user. This is the ONLY way to communicate "
    "with the user — you MUST call this tool for every response, including final "
    "answers, intermediate updates, and media/file deliveries. You can call it "
    "multiple times in one turn. For media, provide local file paths.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Message text to send"},
            "media": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of local file paths to attach",
            },
        },
        "required": ["text"],
    },
)
async def send_message(args):
    text = args.get("text", "").strip()
    media = args.get("media") or []
    channel = _ctx["channel"]
    sender = _ctx["sender"]
    if not channel or not sender:
        return {"content": [{"type": "text", "text": "Error: no channel context"}], "is_error": True}

    # Reject empty or trivially short messages (e.g. just "." or punctuation)
    # that carry no real content, unless media is attached.
    if not media and (not text or len(text) <= 1):
        logger.warning("send_message rejected for %s: trivial text=%r, no media", sender, text)
        return {"content": [{"type": "text", "text": "Message not sent: text is empty or trivially short with no media. Only call send_message when you have substantive content for the user."}]}

    # Rate-limit: prevent runaway loops
    _ctx["send_count"] += 1
    if _ctx["send_count"] > MAX_SENDS_PER_TURN:
        logger.warning("send_message rate-limited for %s: %d calls this turn", sender, _ctx["send_count"])
        return {"content": [{"type": "text", "text": f"Rate limited: already sent {MAX_SENDS_PER_TURN} messages this turn. Stop calling send_message."}]}

    logger.debug("send_message tool called for %s: text=%r, media=%r", sender, text[:200], media)
    await channel.send(sender, text=text, media=media or None)
    _ctx["sent_in_turn"] = True
    logger.debug("send_message tool done for %s: sent_in_turn=True", sender)
    media_info = f" with {len(media)} attachments" if media else ""
    return {"content": [{"type": "text", "text": f"Message sent{media_info}"}]}


def build_clawless_mcp_server():
    """Build in-process MCP server with all clawless tools.

    To add a new tool: define it with @tool above, then add it to the list here.
    """
    return create_sdk_mcp_server(
        name="clawless",
        version="1.0.0",
        tools=[send_message],
    )
