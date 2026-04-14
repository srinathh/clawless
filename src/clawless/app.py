"""FastAPI application — wires agent, channels, store, and config together."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from clawless.agent import AgentManager
from clawless.channels.base import Channel
from clawless.channels.whatsapp import TwilioWhatsAppChannel
from clawless.config import ClawlessPaths, Settings
from clawless.store import MessageStore
from clawless.wiki import make_wiki_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths = ClawlessPaths()
    settings = Settings()  # type: ignore[call-arg]
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s.%(funcName)s %(message)s")

    # Ensure SDK can read the API key from environment
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

    logger.info("Starting clawless — home: %s", paths.home)

    # Wiki endpoint — serves ~/workspace/wiki as rendered HTML
    app.include_router(make_wiki_router(paths.workspace))

    # media_dir is a runtime artifact, auto-created
    paths.media_dir.mkdir(parents=True, exist_ok=True)

    # Message store — SQLite bus for inbound messages, sessions, cursors
    store = MessageStore(paths.data_dir / "clawless.db")
    app.state.store = store

    # Single plugin if plugin_dir has the plugin manifest
    plugins = [str(paths.plugin_dir)] if (paths.plugin_dir / ".claude-plugin").is_dir() else []
    if plugins:
        logger.info("Plugin loaded: %s", paths.plugin_dir)

    # Agent manager
    agent = AgentManager(settings.claude, plugins, paths.workspace, paths.data_dir, store)
    app.state.agent = agent

    # Build channel map for routing (sender prefix → channel instance)
    channels: dict[str, Channel] = {}

    if settings.channels.twilio_whatsapp:
        wa = TwilioWhatsAppChannel(
            settings.channels.twilio_whatsapp, paths.media_dir, app
        )
        channels["whatsapp:"] = wa
        app.state.twilio_whatsapp = wa
        from clawless.channels.whatsapp import WEBHOOK_PATH
        logger.info("Twilio WhatsApp webhook at %s", WEBHOOK_PATH)

    if settings.channels.test:
        from clawless.channels.test import TestChannel
        tc = TestChannel(settings.channels.test, app)
        channels["test:"] = tc
        app.state.test = tc
        asyncio.create_task(tc.run())
        logger.info("Test channel active — %d scripted messages",
                     len(settings.channels.test.messages))

    # Start the message loop — polls store for unprocessed messages
    asyncio.create_task(agent.start_message_loop(channels))

    logger.info("Clawless ready")
    yield

    logger.info("Shutting down clawless")
    await agent.close_all()


app = FastAPI(title="clawless", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    from clawless.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    uvicorn.run("clawless.app:app", host="0.0.0.0", port=settings.port)
