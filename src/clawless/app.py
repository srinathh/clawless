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

    agent_mgr = AgentManager(settings, workspace)

    media_dir = workspace / "media"
    whatsapp = WhatsAppChannel(settings, media_dir)
    whatsapp.set_message_handler(agent_mgr.process_message)
    whatsapp.register_routes(app)

    app.state.agent = agent_mgr
    app.state.whatsapp = whatsapp

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
