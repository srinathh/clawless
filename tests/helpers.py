"""Shared test helpers and constants."""

from datetime import datetime, timezone
from pathlib import Path

from clawless.init import init_home

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOML_CONFIG = """
[claude]
max_turns = 5
max_budget_usd = 0.50

[channels.test]
sender = "test:user1"
messages = ["Hello, who are you?", "What is 2+2?", "Use the send_message tool to send me a message saying exactly 'tool-test-ok'", "Create a file called test.txt in your working directory with the contents 'test'. Confirm when done."]
"""


def create_test_home(prefix: str = "") -> Path:
    """Create an isolated home dir under ./data/ with test config, return the path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    name = f"{prefix}_{ts}" if prefix else ts
    run_dir = (PROJECT_ROOT / "data" / name).resolve()
    init_home(run_dir)
    (run_dir / "data" / "config.toml").write_text(TOML_CONFIG)
    return run_dir
