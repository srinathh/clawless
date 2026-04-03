"""FastAPI application — wires agent, channels, and config together."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from clawless.agent import AgentManager
from clawless.channels.whatsapp import WhatsAppChannel
from clawless.config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    workspace = Path.cwd()

    logger.info("Starting clawless — workspace: %s", workspace)

    app.state.agent = AgentManager(settings, workspace)
    app.state.whatsapp = WhatsAppChannel(settings, workspace / "media", app)

    logger.info("Clawless ready — webhook at %s", settings.twilio_webhook_path)
    yield

    logger.info("Shutting down clawless")
    await agent_mgr.close_all()


app = FastAPI(title="clawless", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run("clawless.app:app", host="0.0.0.0", port=8080)
