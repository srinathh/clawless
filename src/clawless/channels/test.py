"""Test channel for integration testing.

Feeds scripted messages into the message store and captures responses via
HTTP endpoints, allowing end-to-end testing without external services.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import FastAPI

from clawless.channels.base import Channel, InboundMessage
from clawless.config import TestChannelConfig

logger = logging.getLogger(__name__)


class TestChannel(Channel):
    name = "test"
    formatting_instructions = "Plain text only. No markdown or special formatting."

    def __init__(self, config: TestChannelConfig, app: FastAPI) -> None:
        self._config = config
        self._app = app
        self._responses: list[dict] = []
        self._done = asyncio.Event()
        self._error: str | None = None

        app.get("/test/responses")(self._get_responses)
        app.get("/test/status")(self._get_status)

    async def send(self, to: str, text: str = "", media: list[str] | None = None) -> None:
        self._responses.append({"to": to, "text": text, "media": media or []})

    async def run(self) -> None:
        """Write scripted messages to the store, then signal done when all are processed."""
        try:
            store = self._app.state.store
            for content in self._config.messages:
                msg_id = f"test_{uuid.uuid4().hex}"
                store.store_message(
                    id=msg_id,
                    sender=self._config.sender,
                    content=content,
                    inbound=True,
                )
            # Wait for the message loop to process all messages.
            # Poll until we have at least as many responses as scripted messages.
            for _ in range(300):  # up to 5 minutes
                if len(self._responses) >= len(self._config.messages):
                    break
                await asyncio.sleep(1)
        except Exception as e:
            self._error = str(e)
            logger.exception("Test channel run failed")
        finally:
            self._done.set()

    async def _get_responses(self):
        return {"responses": self._responses}

    async def _get_status(self):
        return {
            "done": self._done.is_set(),
            "total_messages": len(self._config.messages),
            "total_responses": len(self._responses),
            "error": self._error,
        }
