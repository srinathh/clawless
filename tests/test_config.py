"""Tests for clawless.config."""

import os
import uuid
from pathlib import Path

from clawless.config import ClawlessPaths, Settings
from clawless.init import init_home

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _setup_home(toml_content: str) -> tuple[Path, str | None]:
    """Create an isolated home dir under ./data/<uuid>/ and set HOME."""
    run_dir = (PROJECT_ROOT / "data" / str(uuid.uuid4())).resolve()
    init_home(run_dir)
    (run_dir / "data" / "config.toml").write_text(toml_content)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(run_dir)
    return run_dir, old_home


def _teardown_home(old_home: str | None) -> None:
    """Restore HOME."""
    if old_home:
        os.environ["HOME"] = old_home
    else:
        os.environ.pop("HOME", None)


def test_clawless_paths_validates():
    """ClawlessPaths validates required dirs exist."""
    run_dir, old_home = _setup_home("[claude]\n")
    try:
        paths = ClawlessPaths()
        assert paths.workspace == run_dir / "workspace"
        assert paths.data_dir == run_dir / "data"
        assert paths.plugin_dir == run_dir / "plugin"
        assert paths.config_file == run_dir / "data" / "config.toml"
        assert paths.media_dir == run_dir / "workspace" / "media"
    finally:
        _teardown_home(old_home)


def test_clawless_paths_raises_on_missing_dirs():
    """ClawlessPaths raises if dirs are missing."""
    run_dir = (PROJECT_ROOT / "data" / str(uuid.uuid4())).resolve()
    run_dir.mkdir(parents=True)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(run_dir)
    try:
        try:
            ClawlessPaths()
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "Missing directories" in str(e)
    finally:
        _teardown_home(old_home)


def test_load_from_toml():
    """Settings loads from ~/data/config.toml."""
    toml_content = """
[claude]
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
    _, old_home = _setup_home(toml_content)
    try:
        settings = Settings()
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
        _teardown_home(old_home)


def test_no_channel_configured():
    """Settings loads without any channel section."""
    _, old_home = _setup_home("[claude]\n")
    try:
        settings = Settings()
        assert settings.channels.twilio_whatsapp is None
    finally:
        _teardown_home(old_home)


def test_env_var_overrides_toml():
    """Environment variables override TOML values."""
    _, old_home = _setup_home("[claude]\nmax_turns = 10\n")
    os.environ["CLAUDE__MAX_TURNS"] = "99"
    try:
        settings = Settings()
        assert settings.claude.max_turns == 99
    finally:
        del os.environ["CLAUDE__MAX_TURNS"]
        _teardown_home(old_home)


def test_defaults():
    """Default values are applied when not specified."""
    _, old_home = _setup_home("[claude]\n")
    try:
        settings = Settings()
        assert settings.claude.max_turns == 30
        assert settings.claude.max_budget_usd == 1.0
        assert settings.claude.max_concurrent_requests == 3
    finally:
        _teardown_home(old_home)
