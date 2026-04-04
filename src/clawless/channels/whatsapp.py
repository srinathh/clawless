"""Twilio WhatsApp channel — webhook-based, async."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

from clawless.channels.base import Channel, InboundMessage
from clawless.config import TwilioWhatsAppConfig
from clawless.utils import split_text

logger = logging.getLogger(__name__)

TWILIO_MAX_MESSAGE_LEN = 1600
WEBHOOK_PATH = "/twilio/whatsapp"


class TwilioWhatsAppChannel(Channel):
    """WhatsApp channel via Twilio Business API.

    Receives Twilio webhooks, downloads media, and sends replies
    via the Twilio REST API.
    """

    name = "twilio-whatsapp"
    formatting_instructions = (
        "The user is on WhatsApp. Use WhatsApp formatting only: "
        "*bold*, _italic_, ~strikethrough~, ```monospace```. "
        "No markdown headers, links, or HTML. Use • for bullet points. "
        "Keep responses concise — messages over 1600 characters are split."
    )

    def __init__(self, config: TwilioWhatsAppConfig, media_dir: Path, app: FastAPI) -> None:
        self._config = config
        self._twilio = TwilioClient(config.account_sid, config.auth_token)
        self._validator: RequestValidator | None = (
            RequestValidator(config.auth_token)
            if config.public_url
            else None
        )

        # Media directories
        self._inbound_media_dir = media_dir / "inbound"
        self._inbound_media_dir.mkdir(parents=True, exist_ok=True)
        self._outbound_media_dir = media_dir / "outbound"
        self._outbound_media_dir.mkdir(parents=True, exist_ok=True)

        # Register routes
        app.post(WEBHOOK_PATH)(self._handle_webhook)
        app.get(f"{WEBHOOK_PATH}/media/{{filename}}")(self._serve_media)

    # ------------------------------------------------------------------
    # Inbound: Twilio webhook
    # ------------------------------------------------------------------

    async def _handle_webhook(self, request: Request) -> Response:
        """Handle an incoming Twilio WhatsApp webhook POST."""
        form = await request.form()

        # Signature validation — active whenever public_url is set
        if self._validator:
            signature = request.headers.get("X-Twilio-Signature", "")
            url = self._config.public_url + request.url.path
            if not self._validator.validate(url, dict(form), signature):
                logger.warning("Invalid Twilio signature — rejecting request")
                return Response(status_code=403, content="Invalid signature")

        sender = str(form.get("From", ""))  # "whatsapp:+1234567890"
        if not sender:
            return Response(status_code=400)

        body = str(form.get("Body", ""))
        message_sid = str(form.get("MessageSid", ""))
        profile_name = str(form.get("ProfileName", ""))

        # Check allowlist
        if sender not in self._config.allowed_senders:
            logger.warning("Message from non-allowed sender %s — dropping", sender)
            return Response(status_code=403)

        # Download media attachments
        try:
            num_media = int(str(form.get("NumMedia", "0")))
        except ValueError:
            num_media = 0

        media_files: list[str] = []

        if num_media > 0:
            media_urls = [u for i in range(num_media) if (u := str(form.get(f"MediaUrl{i}", "")))]
            media_files = await self._download_media(media_urls)

        # Build content with media tags
        content = body
        for fpath in media_files:
            mime, _ = mimetypes.guess_type(fpath)
            media_tag = f"[{mime or 'application/octet-stream'}: {fpath}]"
            content = f"{content}\n{media_tag}" if content else media_tag

        if not content:
            content = "(empty message)"

        logger.info(f"WhatsApp msg {message_sid}: {num_media} attachments")
        logger.debug(f"WhatsApp msg {message_sid} from {sender} ({profile_name}): {body[:80]}")

        message = InboundMessage(
            sender=sender,
            sender_name=profile_name,
            content=content,
            media_files=media_files,
            metadata={"message_sid": message_sid},
        )

        # Fire-and-forget: return ack to Twilio immediately, process async
        asyncio.create_task(request.app.state.agent.process_message(message, self))

        resp = MessagingResponse()
        resp.message(self._config.ack_message)
        return Response(content=resp.to_xml(), media_type="application/xml")

    # ------------------------------------------------------------------
    # Outbound: send via Twilio REST API
    # ------------------------------------------------------------------

    async def send(self, to: str, text: str = "", media: list[str] | None = None) -> None:
        """Send text and/or media. Twilio requires separate API calls for each."""
        if text:
            chunks = split_text(text, max_len=TWILIO_MAX_MESSAGE_LEN)
            for chunk in chunks:
                await asyncio.to_thread(
                    self._twilio.messages.create,
                    from_=self._config.whatsapp_from,
                    to=to,
                    body=chunk,
                )
        for path in media or []:
            if path.startswith(("http://", "https://")):
                url = path
            else:
                url = self._stage_media(path)
            if not url:
                logger.warning("Cannot send media '%s' — no public URL available", path)
                continue
            await asyncio.to_thread(
                self._twilio.messages.create,
                from_=self._config.whatsapp_from,
                to=to,
                media_url=[url],
            )

    # ------------------------------------------------------------------
    # Outbound media staging
    # ------------------------------------------------------------------

    def _stage_media(self, local_path: str) -> str | None:
        """Copy a local file to the outbound dir and return its public URL."""
        src = Path(local_path).expanduser()
        if not src.is_file():
            logger.warning("Media file not found: %s", local_path)
            return None
        filename = f"{uuid.uuid4().hex}{src.suffix}"
        shutil.copy2(src, self._outbound_media_dir / filename)
        return f"{self._config.public_url}{WEBHOOK_PATH}/media/{filename}"

    async def _serve_media(self, request: Request) -> Response:
        """Serve a staged outbound media file for Twilio to fetch."""
        filename = request.path_params["filename"]
        file_path = (self._outbound_media_dir / filename).resolve()
        if file_path.parent != self._outbound_media_dir.resolve():
            return Response(status_code=404)
        if file_path.is_file():
            return FileResponse(str(file_path))
        return Response(status_code=404)

    # ------------------------------------------------------------------
    # Inbound media download
    # ------------------------------------------------------------------

    async def _download_media(self, media_urls: list[str]) -> list[str]:
        """Download Twilio media attachments (requires Basic Auth)."""
        paths: list[str] = []
        async with httpx.AsyncClient(
            auth=(self._config.account_sid, self._config.auth_token),
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            for url in media_urls:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "application/octet-stream")
                    ext = mimetypes.guess_extension(content_type) or ".bin"
                    sid = url.rstrip("/").rsplit("/", 1)[-1]
                    file_path = self._inbound_media_dir / f"{sid}{ext}"
                    file_path.write_bytes(resp.content)
                    paths.append(str(file_path))
                    logger.debug("Downloaded media to %s (%s)", file_path, content_type)
                except Exception:
                    logger.exception("Failed to download media %s", url)
        return paths
