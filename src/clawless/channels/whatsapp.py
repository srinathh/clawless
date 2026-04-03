"""Twilio WhatsApp channel — webhook-based, async.

Ported from srinathh/nanobot (branch feature/twilio-whatsapp-nightly),
file nanobot/channels/twilio_whatsapp.py.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI, Request, Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from clawless.channels.base import Channel, InboundMessage
from clawless.config import Settings
from clawless.formatter import format_for_whatsapp, split_message

logger = logging.getLogger(__name__)

TWILIO_MAX_MESSAGE_LEN = 1600


class WhatsAppChannel:
    """WhatsApp channel via Twilio Business API.

    Receives Twilio webhooks, downloads media, and sends replies
    via the Twilio REST API.
    """

    name = "whatsapp"

    def __init__(self, settings: Settings, media_dir: Path) -> None:
        self._settings = settings
        self._twilio = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
        self._validator: RequestValidator | None = (
            RequestValidator(settings.twilio_auth_token)
            if settings.twilio_validate_signature
            else None
        )
        self._message_handler: Callable[[InboundMessage, Channel], Awaitable[None]] | None = None

        # Media directories
        self._inbound_media_dir = media_dir / "inbound"
        self._inbound_media_dir.mkdir(parents=True, exist_ok=True)
        self._outbound_media_dir = media_dir / "outbound"
        self._outbound_media_dir.mkdir(parents=True, exist_ok=True)

    def set_message_handler(
        self, handler: Callable[[InboundMessage, Channel], Awaitable[None]]
    ) -> None:
        self._message_handler = handler

    def register_routes(self, app: FastAPI) -> None:
        """Register webhook and media-serving routes on the FastAPI app."""
        app.post(self._settings.twilio_webhook_path)(self._handle_webhook)
        app.get("/twilio/whatsapp/media/{filename}")(self._serve_media)

    # ------------------------------------------------------------------
    # Inbound: Twilio webhook
    # ------------------------------------------------------------------

    async def _handle_webhook(self, request: Request) -> Response:
        """Handle an incoming Twilio WhatsApp webhook POST."""
        form = await request.form()

        # Signature validation
        if self._validator:
            signature = request.headers.get("X-Twilio-Signature", "")
            if self._settings.twilio_public_url:
                url = self._settings.twilio_public_url + request.url.path
            else:
                url = str(request.url)
            if not self._validator.validate(url, dict(form), signature):
                logger.warning("Invalid Twilio signature — rejecting request")
                return Response(status_code=403, content="Invalid signature")

        sender = str(form.get("From", ""))  # "whatsapp:+1234567890"
        body = str(form.get("Body", ""))
        message_sid = str(form.get("MessageSid", ""))
        profile_name = str(form.get("ProfileName", ""))

        # Check allowlist
        if self._settings.allowed_senders and sender not in self._settings.allowed_senders:
            logger.warning("Message from non-allowed sender %s — dropping", sender)
            return Response(content="<Response></Response>", media_type="application/xml")

        # Download media attachments
        num_media = int(form.get("NumMedia", "0") or "0")
        media_files: list[str] = []
        if num_media > 0:
            media_urls = [str(form.get(f"MediaUrl{i}", "")) for i in range(num_media)]
            media_urls = [u for u in media_urls if u]
            media_files = await self._download_media(media_urls)

        # Build content with media tags
        content = body or ""
        for fpath in media_files:
            mime, _ = mimetypes.guess_type(fpath)
            tag = "image" if mime and mime.startswith("image/") else "file"
            media_tag = f"[{tag}: {fpath}]"
            content = f"{content}\n{media_tag}" if content else media_tag

        if not content:
            content = "(empty message)"

        logger.info(
            "WhatsApp from %s (%s): %s (%d media)",
            sender, profile_name, body[:80], num_media,
        )

        message = InboundMessage(
            sender=sender,
            sender_name=profile_name,
            content=content,
            media_files=media_files,
            metadata={"message_sid": message_sid},
        )

        # Fire-and-forget: return 200 to Twilio immediately, process async
        if self._message_handler:
            asyncio.create_task(self._message_handler(message, self))

        return Response(content="<Response></Response>", media_type="application/xml")

    # ------------------------------------------------------------------
    # Outbound: send via Twilio REST API
    # ------------------------------------------------------------------

    async def send(self, to: str, text: str = "", media: list[str] | None = None) -> None:
        """Send text and/or media. Twilio requires separate API calls for each."""
        if text:
            formatted = format_for_whatsapp(text)
            for chunk in split_message(formatted, max_len=TWILIO_MAX_MESSAGE_LEN):
                await asyncio.to_thread(
                    self._twilio.messages.create,
                    from_=self._settings.twilio_whatsapp_from,
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
                from_=self._settings.twilio_whatsapp_from,
                to=to,
                media_url=[url],
            )

    # ------------------------------------------------------------------
    # Outbound media staging
    # ------------------------------------------------------------------

    def _stage_media(self, local_path: str) -> str | None:
        """Copy a local file to the outbound dir and return its public URL."""
        if not self._settings.twilio_public_url:
            logger.warning(
                "Cannot serve local media '%s': twilio_public_url not configured",
                local_path,
            )
            return None
        src = Path(local_path).expanduser()
        if not src.is_file():
            logger.warning("Media file not found: %s", local_path)
            return None
        filename = f"{uuid.uuid4().hex}{src.suffix}"
        shutil.copy2(src, self._outbound_media_dir / filename)
        return f"{self._settings.twilio_public_url}/twilio/whatsapp/media/{filename}"

    async def _serve_media(self, request: Request) -> Response:
        """Serve a staged outbound media file for Twilio to fetch."""
        from starlette.responses import FileResponse

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
            auth=(self._settings.twilio_account_sid, self._settings.twilio_auth_token),
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
