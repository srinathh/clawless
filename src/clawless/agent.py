"""Agent session management via ClaudeSDKClient.

Maintains one persistent ClaudeSDKClient per sender, with per-sender
locking, session persistence, and a message loop that polls the store
for unprocessed inbound messages.

Host-controlled delivery: the agent's structured output (text + media)
is parsed by the host and sent via the channel. No agent-side send tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SdkPluginConfig,
    SystemMessage,
    TextBlock,
)

from clawless.channels.base import Channel, InboundMessage
from clawless.config import ClaudeConfig
from clawless.store import MessageStore
from clawless.tools import build_clawless_mcp_server

logger = logging.getLogger(__name__)

# JSON schema for structured output — agent returns text + optional media paths.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Message text to send to the user",
        },
        "media": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Local file paths to attach as media",
        },
    },
    "required": ["text"],
}

RESET_AGENT_CMD = "/reset_agent"
RESET_QUEUE_CMD = "/reset_queue"

FRAMEWORK_SYSTEM_PROMPT = """\
Your response will be delivered to the user automatically as a structured JSON \
object. Reply naturally in the "text" field. To attach files, include their \
local paths in the "media" array.

Your working directory is ~/workspace/. You have all Claude Code tools available \
with bypass permissions.

## Media handling
- Inbound media from users arrives as `[mime/type: /path/to/file]` tags in the \
message text. Files are stored under ~/workspace/media/inbound/. You can read \
image files directly since you are multimodal.
- To send media/files to the user: include local file paths in the "media" \
array of your response. The channel will stage and serve them automatically.

## Skills, agents, and plugins

IMPORTANT: When asked to create skills, agents, or MCP configs, ALWAYS write them \
to ~/workspace/plugin/. Do not ask for permission — you already have write access. \
Use the Write tool directly to create the files.

Two locations provide extensibility:

1. **~/workspace/plugin/** — YOUR writable plugin directory:
   - Skills: ~/workspace/plugin/skills/<skill-name>/SKILL.md (invoked as /workspace-plugin:<skill-name>)
   - Agents: ~/workspace/plugin/agents/<agent-name>.md

2. **~/plugin/** — Pre-configured plugin (READ-ONLY). Never write to this directory. \
Plugin skills are invoked as /private-plugin:<skill-name>.

Check both locations when looking for available skills and agents."""


@dataclass
class _SessionClient:
    """Wraps a persistent ClaudeSDKClient for one conversation."""

    client: ClaudeSDKClient
    session_id: str | None = None
    is_resuming: bool = False


class AgentManager:
    """Manages ClaudeSDKClient instances, one per sender.

    Messages from the same sender are serialized via per-sender locks.
    A global semaphore caps total concurrent SDK calls.
    """

    def __init__(
        self,
        config: ClaudeConfig,
        plugins: list[str],
        workspace: Path,
        data_dir: Path,
        store: MessageStore,
    ) -> None:
        self._config = config
        self._plugins = plugins
        self._workspace = workspace
        self._store = store
        self._clients: dict[str, _SessionClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._mcp_server = build_clawless_mcp_server()
        self._in_flight_msgs: set[str] = set()  # message IDs with tasks already dispatched

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _build_options(self, session_key: str) -> ClaudeAgentOptions:
        plugins: list[SdkPluginConfig] = [
            SdkPluginConfig(type="local", path=p) for p in self._plugins if p
        ]
        # Writable plugin inside workspace — agent creates skills/agents here
        ws_plugin = self._workspace / "plugin"
        if (ws_plugin / ".claude-plugin").is_dir():
            plugins.append(SdkPluginConfig(type="local", path=str(ws_plugin)))
        options = ClaudeAgentOptions(
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": FRAMEWORK_SYSTEM_PROMPT,
            },
            permission_mode="bypassPermissions",
            max_turns=self._config.max_turns,
            max_budget_usd=self._config.max_budget_usd,
            setting_sources=["user", "project"],
            cwd=str(self._workspace),
            plugins=plugins,
            mcp_servers={"clawless": self._mcp_server},
            output_format={"type": "json_schema", "schema": RESPONSE_SCHEMA},
            allowed_tools=[
                # File tools
                "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit",
                # Execution
                "Bash", "KillBash",
                # Agent / orchestration
                "Agent", "TodoWrite", "Skill",
                # Web
                "WebSearch", "WebFetch",
                # MCP
                "ListMcpResources", "ReadMcpResource",
                "mcp__clawless__*",
            ],
        )
        # Resume existing session if we have a persisted mapping
        cli_session_id = self._store.get_session(session_key)
        if cli_session_id:
            options.resume = cli_session_id
            logger.info("Resuming session %s for %s", cli_session_id, session_key)
        else:
            logger.info("Creating new session for %s", session_key)
        return options

    async def _get_or_create_client(self, session_key: str) -> _SessionClient:
        if session_key in self._clients:
            return self._clients[session_key]

        options = self._build_options(session_key)
        is_resuming = options.resume is not None
        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        try:
            sc = _SessionClient(
                client=client,
                session_id=self._store.get_session(session_key),
                is_resuming=is_resuming,
            )
            self._clients[session_key] = sc
        except Exception:
            await client.__aexit__(None, None, None)
            raise
        return sc

    async def _close_client(self, session_key: str) -> None:
        sc = self._clients.pop(session_key, None)
        if sc and sc.client:
            try:
                await sc.client.__aexit__(None, None, None)
            except Exception:
                logger.debug("Error closing client for %s", session_key)

    async def _reset_session(self, session_key: str) -> None:
        """Close the SDK client AND clear the persisted session so the next message starts fresh."""
        await self._close_client(session_key)
        self._store.delete_session(session_key)
        logger.info("Reset session for %s (client closed, persisted session cleared)", session_key)

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def process_message(self, message: InboundMessage, channel: Channel) -> None:
        """Process an inbound message and send the reply via the channel.

        Called by the message loop. Per-sender lock serializes messages
        from the same sender. Global semaphore caps concurrent SDK calls.
        Cursor is advanced optimistically and rolled back on error if no
        output was sent (nanoclaw pattern).
        """
        sender = message.sender
        lock = self._locks.setdefault(sender, asyncio.Lock())

        async with lock:
            # Advance cursor optimistically, save old for rollback
            previous_cursor = self._store.get_cursor(sender)
            self._store.set_cursor(sender, message.message_id)

            output_sent = False
            try:
                # --- Command interception (no SDK call needed) ---
                content = message.content.strip()

                if content == RESET_AGENT_CMD:
                    await self._reset_session(sender)
                    await channel.send(
                        sender,
                        text="Agent reset. Your next message starts a fresh conversation.",
                    )
                    output_sent = True
                    return

                if content == RESET_QUEUE_CMD:
                    skipped = self._store.skip_to_latest(sender)
                    reply = (
                        "Queue cleared."
                        if skipped == 0
                        else f"Queue cleared — {skipped} pending message(s) cancelled."
                    )
                    await channel.send(sender, text=reply)
                    output_sent = True
                    return
                # --- End command interception ---

                logger.debug("Processing message for %s: %r", sender, message.content[:200])
                sc = await self._get_or_create_client(sender)
                logger.debug("Client ready for %s, starting query", sender)

                async def _run_query() -> tuple[str, dict | None, list[str]]:
                    prompt = f"[{channel.formatting_instructions}]\n\n{message.content}"
                    await sc.client.query(prompt)
                    logger.debug("Query submitted for %s, receiving response", sender)
                    content = ""
                    structured = None
                    # Buffer TextBlocks — sent after deduplication against
                    # the final StructuredOutput to avoid double-sending.
                    text_blocks: list[str] = []
                    # When resuming a session, history replays first.
                    # Skip TextBlocks until we see a ResultMessage (end of
                    # replayed turn), then collect TextBlocks for the new response.
                    past_history = not sc.is_resuming
                    async for msg in sc.client.receive_response():
                        msg_type = type(msg).__name__
                        logger.debug("SDK message for %s: %s", sender, msg_type)
                        if isinstance(msg, SystemMessage) and msg.subtype == "init":
                            new_id = msg.data.get("session_id")
                            if new_id and new_id != sc.session_id:
                                sc.session_id = new_id
                                self._store.set_session(sender, new_id)
                        elif isinstance(msg, ResultMessage):
                            logger.debug(
                                "ResultMessage for %s: subtype=%s, result=%r, structured=%r",
                                sender, msg.subtype,
                                msg.result[:200] if msg.result else None,
                                msg.structured_output,
                            )
                            if msg.result:
                                content = msg.result
                            if msg.structured_output is not None:
                                structured = msg.structured_output
                            past_history = True
                        elif isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock) and block.text.strip():
                                    if past_history:
                                        text_blocks.append(block.text)
                                    else:
                                        logger.debug("Skipping replayed TextBlock for %s", sender)
                        else:
                            preview = ""
                            for attr in ("content", "text", "data", "result"):
                                val = getattr(msg, attr, None)
                                if val is not None:
                                    preview = repr(val)[:300]
                                    break
                            logger.info("Unhandled %s for %s: %s", msg_type, sender, preview or "(no content attr)")
                    # First query after resume is done — clear the flag
                    sc.is_resuming = False
                    return content, structured, text_blocks

                final_content, structured, text_blocks = await asyncio.wait_for(
                    _run_query(), timeout=self._config.request_timeout
                )

                # Host-controlled delivery: parse structured output, send via channel
                text = ""
                media: list[str] | None = None
                if structured and isinstance(structured, dict):
                    text = structured.get("text", "")
                    media = structured.get("media") or None
                elif final_content:
                    # Fallback: plain text if structured output wasn't produced
                    text = final_content

                # Send intermediate TextBlocks that differ from the final
                # structured output (avoids duplicating the final response).
                for tb in text_blocks:
                    if tb.strip() != text.strip():
                        logger.debug("Sending intermediate TextBlock for %s: %r", sender, tb[:200])
                        await channel.send(sender, text=tb)
                        output_sent = True

                logger.debug(
                    "Query complete for %s: text=%r, media=%r",
                    sender, text[:200] if text else None, media,
                )

                if text and text.strip():
                    await channel.send(sender, text=text, media=media)
                    output_sent = True
                elif media:
                    await channel.send(sender, media=media)
                    output_sent = True
                else:
                    logger.warning("No response produced for %s, resetting session", sender)
                    await self._reset_session(sender)
                    await channel.send(
                        sender,
                        text="Sorry, I wasn't able to generate a response. "
                        "I have reset the agent, please try again.",
                    )
                    output_sent = True

            except asyncio.TimeoutError:
                logger.error("SDK call timed out for %s, resetting session", sender)
                await self._reset_session(sender)
                try:
                    await channel.send(
                        sender, text="Sorry, the request timed out. Please try again."
                    )
                    output_sent = True
                except Exception:
                    logger.exception("Failed to send timeout message to %s", sender)

            except Exception:
                logger.exception("Error processing message for %s, resetting session", sender)
                await self._reset_session(sender)
                try:
                    await channel.send(
                        sender,
                        text="Sorry, I encountered an error processing your message.",
                    )
                    output_sent = True
                except Exception:
                    logger.exception("Failed to send error message to %s", sender)

            finally:
                # Cursor rollback: if nothing was sent, roll back so the message
                # can be reprocessed on restart (nanoclaw pattern).
                if not output_sent and previous_cursor is not None:
                    try:
                        self._store.set_cursor(sender, previous_cursor)
                        logger.info("Rolled back cursor for %s (no output sent)", sender)
                    except Exception:
                        logger.debug("Could not roll back cursor for %s (store may be closed)", sender)

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def start_message_loop(
        self,
        channels: dict[str, Channel],
        poll_interval: float = 1.0,
    ) -> None:
        """Poll the store for unprocessed messages and route to the agent.

        Runs as a long-lived async task started from the app lifespan.
        Routes outbound replies to the correct channel by sender prefix.
        """
        logger.info("Message loop started (poll_interval=%.1fs)", poll_interval)
        while True:
            try:
                senders = self._store.get_all_senders_with_unprocessed()
                for sender in senders:
                    messages = self._store.get_unprocessed(sender)
                    channel = self._resolve_channel(sender, channels)
                    if not channel:
                        logger.warning("No channel for sender %s", sender)
                        continue
                    for msg in messages:
                        msg_id = msg["id"]
                        if msg_id in self._in_flight_msgs:
                            continue
                        self._in_flight_msgs.add(msg_id)
                        media_files = (
                            json.loads(msg["media_files"])
                            if msg["media_files"]
                            else []
                        )
                        inbound = InboundMessage(
                            sender=msg["sender"],
                            content=msg["content"],
                            message_id=msg_id,
                            sender_name=msg["sender_name"],
                            media_files=media_files,
                        )

                        async def _dispatch(inbound=inbound, _msg_id=msg_id):
                            try:
                                await self.process_message(inbound, channel)
                            finally:
                                self._in_flight_msgs.discard(_msg_id)

                        asyncio.create_task(_dispatch())
            except Exception:
                logger.exception("Error in message loop")
            await asyncio.sleep(poll_interval)

    @staticmethod
    def _resolve_channel(
        sender: str, channels: dict[str, Channel]
    ) -> Channel | None:
        """Route sender to channel by prefix match."""
        for prefix, channel in channels.items():
            if sender.startswith(prefix):
                return channel
        return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close_all(self) -> None:
        """Close all active clients and the session store."""
        for key in list(self._clients):
            await self._close_client(key)
        self._store.close()
