"""Agent session management via ClaudeSDKClient.

Maintains one persistent ClaudeSDKClient per sender, with per-sender
locking and session persistence across restarts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SdkPluginConfig,
    SystemMessage,
)
from sqlitedict import SqliteDict

from clawless.channels.base import Channel, InboundMessage
from clawless.config import ClaudeConfig

logger = logging.getLogger(__name__)


@dataclass
class _SessionClient:
    """Wraps a persistent ClaudeSDKClient for one conversation."""

    client: ClaudeSDKClient
    session_id: str | None = None


class AgentManager:
    """Manages ClaudeSDKClient instances, one per sender.

    Messages from the same sender are serialized via per-sender locks.
    A global semaphore caps total concurrent SDK calls.
    """

    def __init__(self, config: ClaudeConfig, plugins: list[str], workspace: Path, data_dir: Path) -> None:
        self._config = config
        self._plugins = plugins
        self._workspace = workspace
        self._clients: dict[str, _SessionClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._concurrency_gate = asyncio.Semaphore(config.max_concurrent_requests)
        self._session_map = SqliteDict(str(data_dir / "sessions.db"), autocommit=True)

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _build_options(self, session_key: str) -> ClaudeAgentOptions:
        plugins: list[SdkPluginConfig] = [
            SdkPluginConfig(type="local", path=p) for p in self._plugins if p
        ]
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            max_turns=self._config.max_turns,
            max_budget_usd=self._config.max_budget_usd,
            setting_sources=["user", "project"],
            cwd=str(self._workspace),
            plugins=plugins,
        )
        # Resume existing session if we have a persisted mapping
        cli_session_id = self._session_map.get(session_key)
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
        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        try:
            sc = _SessionClient(client=client, session_id=self._session_map.get(session_key))
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

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    async def process_message(self, message: InboundMessage, channel: Channel) -> None:
        """Process an inbound message and send the reply via the channel.

        Called as a fire-and-forget task from the webhook handler.
        Per-sender lock serializes messages from the same sender.
        Global semaphore caps concurrent SDK calls.
        """
        sender = message.sender
        lock = self._locks.setdefault(sender, asyncio.Lock())

        async with lock, self._concurrency_gate:
            try:
                sc = await self._get_or_create_client(sender)

                async def _run_query() -> str:
                    prompt = f"[{channel.formatting_instructions}]\n\n{message.content}"
                    await sc.client.query(prompt)
                    content = ""
                    async for msg in sc.client.receive_response():
                        if isinstance(msg, SystemMessage) and msg.subtype == "init":
                            new_id = msg.data.get("session_id")
                            if new_id and new_id != sc.session_id:
                                sc.session_id = new_id
                                self._session_map[sender] = new_id
                        elif isinstance(msg, ResultMessage):
                            if msg.result:
                                content = msg.result
                        else:
                            logger.debug("Unhandled message: %s", type(msg).__name__)
                    return content

                final_content = await asyncio.wait_for(
                    _run_query(), timeout=self._config.request_timeout
                )

                if not final_content:
                    final_content = "Done — no text response."

                await channel.send(sender, text=final_content)

            except asyncio.TimeoutError:
                logger.error("SDK call timed out for %s", sender)
                await self._close_client(sender)
                try:
                    await channel.send(sender, text="Sorry, the request timed out.")
                except Exception:
                    logger.exception("Failed to send timeout message to %s", sender)

            except Exception:
                logger.exception("Error processing message for %s", sender)
                try:
                    await channel.send(
                        sender, text="Sorry, I encountered an error processing your message."
                    )
                except Exception:
                    logger.exception("Failed to send error message to %s", sender)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close_all(self) -> None:
        """Close all active clients and the session store."""
        for key in list(self._clients):
            await self._close_client(key)
        self._session_map.close()
