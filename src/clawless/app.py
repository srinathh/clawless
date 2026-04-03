"""FastAPI application — wires agent, channels, and config together."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from clawless.agent import AgentManager
from clawless.channels.whatsapp import WhatsAppChannel
from clawless.config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s.%(funcName)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()  # type: ignore[call-arg]
    workspace = Path(settings.app.workspace).resolve()
    data_dir = Path(settings.app.data_dir).resolve()

    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Starting clawless — workspace: %s, data_dir: %s", workspace, data_dir)

    app.state.agent = AgentManager(settings.claude, settings.app.plugins, workspace, data_dir)

    media_dir = workspace / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    if settings.channels.twilio_whatsapp:
        app.state.whatsapp = WhatsAppChannel(
            settings.channels.twilio_whatsapp, workspace / "media", app
        )
        logger.info("WhatsApp channel active — webhook at %s",
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

    uvicorn.run("clawless.app:app", host="0.0.0.0", port=8080)
