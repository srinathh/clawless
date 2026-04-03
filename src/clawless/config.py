"""Configuration loaded from TOML file with env var overrides.

Config is loaded from a TOML file (default: config.toml, override via
CONFIG_FILE env var). Environment variables can override any value using
__ as the nesting separator (e.g. CLAUDE__API_KEY overrides claude.api_key).
"""

from __future__ import annotations

import os

from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    TomlConfigSettingsSource,
)


class AppConfig(BaseModel):
    workspace: str = "."  # Claude SDK cwd + media; the mounted project folder
    data_dir: str = "/home/clawless/datadir"  # framework state (session map, etc.)
    plugins: list[str] = []


class ClaudeConfig(BaseModel):
    api_key: str = ""  # can be empty if .credentials.json is mounted
    max_turns: int = 30
    max_budget_usd: float = 1.0
    max_concurrent_requests: int = 3


class TwilioWhatsAppConfig(BaseModel):
    account_sid: str = ""
    auth_token: str = ""
    whatsapp_from: str = ""
    webhook_path: str = "/twilio/whatsapp"
    public_url: str  # required — ngrok, Cloudflare tunnel, reverse proxy, etc.
    ack_message: str = "Thinking..."
    allowed_senders: list[str]  # required — no allow-all


class TestChannelConfig(BaseModel):
    sender: str = "test:user1"
    messages: list[str] = []


class ChannelsConfig(BaseModel):
    twilio_whatsapp: TwilioWhatsAppConfig | None = None
    test: TestChannelConfig | None = None

    def has_any(self) -> bool:
        """True if at least one channel is configured."""
        return any(v is not None for v in self.model_dump().values())


class Settings(BaseSettings):
    app: AppConfig = AppConfig()
    claude: ClaudeConfig = ClaudeConfig()
    channels: ChannelsConfig = ChannelsConfig()

    model_config = {"env_nested_delimiter": "__"}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_file = os.environ.get("CONFIG_FILE", "config.toml")
        return (
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=toml_file),
        )
