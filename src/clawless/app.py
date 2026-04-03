"""FastAPI application — wires agent, channels, and config together."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from clawless.agent import AgentManager
from clawless.channels.whatsapp import TwilioWhatsAppChannel
from clawless.config import ClawlessPaths, Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s.%(funcName)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths = ClawlessPaths()
    settings = Settings()  # type: ignore[call-arg]

    logger.info("Starting clawless — home: %s", paths.home)

    # media_dir is a runtime artifact, auto-created
    paths.media_dir.mkdir(parents=True, exist_ok=True)

    # Single plugin if plugin_dir has the plugin manifest
    plugins = [str(paths.plugin_dir)] if (paths.plugin_dir / ".claude-plugin").is_dir() else []
    if plugins:
        logger.info("Plugin loaded: %s", paths.plugin_dir)

    app.state.agent = AgentManager(settings.claude, plugins, paths.workspace, paths.data_dir)

    if settings.channels.twilio_whatsapp:
        app.state.twilio_whatsapp = TwilioWhatsAppChannel(
            settings.channels.twilio_whatsapp, paths.media_dir, app
        )
        logger.info("Twilio WhatsApp webhook at %s",
                     settings.channels.twilio_whatsapp.webhook_path)

    if settings.channels.test:
        from clawless.channels.test import TestChannel
        app.state.test = TestChannel(settings.channels.test, app)
        asyncio.create_task(app.state.test.run())
        logger.info("Test channel active — %d scripted messages",
                     len(settings.channels.test.messages))

    if not settings.channels.has_any():
        raise RuntimeError("No channels configured — add at least one channel to config.toml")

    logger.info("Clawless ready")
    yield

    logger.info("Shutting down clawless")
    await app.state.agent.close_all()


app = FastAPI(title="clawless", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    from clawless.config import Settings

    settings = Settings()  # type: ignore[call-arg]
    uvicorn.run("clawless.app:app", host="0.0.0.0", port=settings.port)
