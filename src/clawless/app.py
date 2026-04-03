"""FastAPI application — wires agent, channels, and config together."""

from __future__ import annotations

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
    workspace = Path.cwd()

    logger.info("Starting clawless — workspace: %s", workspace)

    app.state.agent = AgentManager(settings.claude, settings.app.plugins, workspace)

    if settings.channels.twilio_whatsapp:
        app.state.whatsapp = WhatsAppChannel(
            settings.channels.twilio_whatsapp, workspace / "media", app
        )
        logger.info("WhatsApp channel active — webhook at %s",
                     settings.channels.twilio_whatsapp.webhook_path)
    else:
        logger.info("No WhatsApp channel configured")

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
