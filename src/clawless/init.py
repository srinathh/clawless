"""Scaffold the clawless home directory structure.

Usage: clawless-init [path]
Default path: ~/clawless_home
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CONFIG_TEMPLATE = """\
# Clawless configuration — place at ~/data/config.toml
# In Docker: /home/clawless/data/config.toml

[claude]
max_turns = 30
max_budget_usd = 1.0
max_concurrent_requests = 3

# Channels — only configured channels get instantiated.
# Remove or comment out a section to disable that channel.

# [channels.twilio_whatsapp]
# account_sid = "AC..."
# auth_token = ""
# whatsapp_from = "whatsapp:+14155238886"
# webhook_path = "/twilio/whatsapp"
# public_url = "https://xxxx.ngrok-free.app"
# ack_message = "Thinking..."
# allowed_senders = ["whatsapp:+1234567890"]

# [channels.test]
# sender = "test:user1"
# messages = ["Hello", "What is 2+2?"]
"""


def init_home(path: Path) -> None:
    """Create the prescribed clawless directory structure."""
    for subdir in ["workspace", ".claude", "data"]:
        (path / subdir).mkdir(parents=True, exist_ok=True)

    # Plugin skeleton with prescribed structure
    plugin = path / "plugin"
    for plugin_subdir in [".claude-plugin", "skills", "agents", "commands", "hooks"]:
        (plugin / plugin_subdir).mkdir(parents=True, exist_ok=True)

    # Minimal plugin.json
    manifest = plugin / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        manifest.write_text(json.dumps({"name": "private-plugin"}, indent=2) + "\n")

    # Config template
    config_dest = path / "data" / "config.toml"
    if not config_dest.exists():
        config_dest.write_text(CONFIG_TEMPLATE)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="clawless-init",
        description="Initialize clawless home directory with prescribed structure",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=str(Path.home() / "clawless_home"),
        help="target directory (default: ~/clawless_home)",
    )
    args = parser.parse_args()

    path = Path(args.path).resolve()
    init_home(path)

    print(f"Initialized clawless home at {path}")
    print()
    print(f"  {path}/")
    print(f"  ├── workspace/       # Agent working directory (Claude SDK cwd)")
    print(f"  ├── .claude/         # Claude CLI credentials and state")
    print(f"  ├── data/            # Framework config and state")
    print(f"  │   └── config.toml  # Edit this to configure channels")
    print(f"  └── plugin/          # Plugin directory (skills, agents, hooks)")
    print()
    print(f"For Docker: CLAWLESS_HOST_DIR={path} docker compose up")
