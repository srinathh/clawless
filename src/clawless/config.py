"""Configuration loaded from environment variables / .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Auth
    anthropic_api_key: str = ""

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""  # e.g. "whatsapp:+14155238886"
    twilio_webhook_path: str = "/twilio/whatsapp"
    twilio_public_url: str  # public URL (ngrok, Cloudflare tunnel, reverse proxy, etc.)
    twilio_ack_message: str = "Thinking..."  # immediate reply before agent processes

    # Plugins — comma-separated list of plugin directory paths (inside container)
    plugins: list[str] = []

    # Access control — must list at least one allowed sender
    allowed_senders: list[str]

    # Agent limits
    max_turns: int = 30
    max_budget_usd: float = 1.0
    max_concurrent_requests: int = 3

    model_config = {"env_file_encoding": "utf-8"}
