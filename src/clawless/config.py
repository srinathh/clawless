"""Configuration from env vars, .env file, and optional TOML.

All paths are derived from Path.home() — see ClawlessPaths.
Config sources (highest priority wins): env vars > .env file > ~/clawless.toml.
Environment variables use __ as the nesting separator (e.g. CLAUDE__MAX_TURNS=10).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    TomlConfigSettingsSource,
)


class ClawlessPaths:
    """All paths derived from the user's home directory.

    Validates that required directories exist on construction.
    Use clawless-init to create the expected structure.
    """

    def __init__(self) -> None:
        self._home = Path.home()
        self._validate()

    def _validate(self) -> None:
        missing = [
            name for name, path in [
                ("workspace", self.workspace),
                ("data", self.data_dir),
                ("plugin", self.plugin_dir),
            ]
            if not path.is_dir()
        ]
        if missing:
            raise RuntimeError(
                f"Missing directories under {self._home}: {', '.join(missing)}. "
                f"Run 'clawless-init {self._home}' to create the expected structure."
            )

    @property
    def home(self) -> Path:
        return self._home

    @property
    def workspace(self) -> Path:
        return self._home / "workspace"

    @property
    def data_dir(self) -> Path:
        return self._home / "data"

    @property
    def plugin_dir(self) -> Path:
        return self._home / "plugin"

    @property
    def media_dir(self) -> Path:
        return self.workspace / "media"


class ClaudeConfig(BaseModel):
    max_turns: int = 30
    max_budget_usd: float = 1.0
    max_concurrent_requests: int = 3
    request_timeout: float = 300.0
    bot_name: str = "Clawless"


class TwilioWhatsAppConfig(BaseModel):
    account_sid: str = ""
    auth_token: str = ""
    whatsapp_from: str = ""
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
    anthropic_api_key: str
    port: int = 18265
    log_level: str = "DEBUG"
    claude: ClaudeConfig = ClaudeConfig()
    channels: ChannelsConfig = ChannelsConfig()

    model_config = {"env_nested_delimiter": "__", "env_file": ".env"}

    @model_validator(mode="after")
    def at_least_one_channel(self):
        if not self.channels.has_any():
            raise ValueError("At least one channel must be configured")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_file = str(Path.home() / "clawless.toml")
        return (
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=toml_file),
        )
