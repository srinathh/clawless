"""Tests for clawless.config."""

import os
import tempfile
from pathlib import Path

from clawless.config import Settings


def test_load_from_toml():
    """Settings loads from a TOML file."""
    toml_content = """
[app]
plugins = ["/path/to/plugin"]

[claude]
api_key = "test-key"
max_turns = 10
max_budget_usd = 0.5
max_concurrent_requests = 2

[channels.twilio_whatsapp]
account_sid = "AC123"
auth_token = "token123"
whatsapp_from = "whatsapp:+14155238886"
webhook_path = "/twilio/whatsapp"
public_url = "https://example.ngrok-free.app"
ack_message = "Working..."
allowed_senders = ["whatsapp:+1234567890"]
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        os.environ["CONFIG_FILE"] = f.name
        try:
            settings = Settings()
            assert settings.app.plugins == ["/path/to/plugin"]
            assert settings.claude.api_key == "test-key"
            assert settings.claude.max_turns == 10
            assert settings.claude.max_budget_usd == 0.5
            assert settings.claude.max_concurrent_requests == 2
            assert settings.channels.twilio_whatsapp is not None
            assert settings.channels.twilio_whatsapp.account_sid == "AC123"
            assert settings.channels.twilio_whatsapp.whatsapp_from == "whatsapp:+14155238886"
            assert settings.channels.twilio_whatsapp.public_url == "https://example.ngrok-free.app"
            assert settings.channels.twilio_whatsapp.ack_message == "Working..."
            assert settings.channels.twilio_whatsapp.allowed_senders == ["whatsapp:+1234567890"]
        finally:
            del os.environ["CONFIG_FILE"]
            Path(f.name).unlink()


def test_no_channel_configured():
    """Settings loads without any channel section."""
    toml_content = """
[claude]
api_key = "test-key"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        os.environ["CONFIG_FILE"] = f.name
        try:
            settings = Settings()
            assert settings.channels.twilio_whatsapp is None
            assert settings.claude.api_key == "test-key"
        finally:
            del os.environ["CONFIG_FILE"]
            Path(f.name).unlink()


def test_env_var_overrides_toml():
    """Environment variables override TOML values."""
    toml_content = """
[claude]
api_key = "from-toml"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        os.environ["CONFIG_FILE"] = f.name
        os.environ["CLAUDE__API_KEY"] = "from-env"
        try:
            settings = Settings()
            assert settings.claude.api_key == "from-env"
        finally:
            del os.environ["CONFIG_FILE"]
            del os.environ["CLAUDE__API_KEY"]
            Path(f.name).unlink()


def test_defaults():
    """Default values are applied when not specified."""
    toml_content = """
[claude]
api_key = ""
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        os.environ["CONFIG_FILE"] = f.name
        try:
            settings = Settings()
            assert settings.claude.max_turns == 30
            assert settings.claude.max_budget_usd == 1.0
            assert settings.claude.max_concurrent_requests == 3
            assert settings.app.plugins == []
        finally:
            del os.environ["CONFIG_FILE"]
            Path(f.name).unlink()
