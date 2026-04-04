"""Scaffold the clawless home directory structure.

Usage: clawless-init [path]
Default path: ~/clawless_home
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

USER_CLAUDE_MD_TEMPLATE = """\
# Clawless Personal Assistant

You are a personal AI assistant running on the Clawless framework. Users reach you through messaging channels (currently WhatsApp).

## Communication style

- Be concise and conversational — this is a chat, not a document
- Skip preamble. Answer directly
- Channel-specific formatting rules are provided in each message — follow them
- When a task is done, say so briefly. Don't recap what you did unless asked
"""

PROJECT_CLAUDE_MD_TEMPLATE = """\
# Workspace

Your working directory is ~/workspace/. You have all Claude Code tools available with unrestricted permissions.

## Media

Inbound media from users arrives as `[mime/type: /path/to/file]` tags in the message text. The files are stored under ~/workspace/media/inbound/. You can read image files directly since you are multimodal.

Outbound media: save files to ~/workspace/media/outbound/ and include the local path in your response. The channel will stage and serve them automatically.

## Plugin

A plugin at ~/plugin/ may provide additional skills, agents, commands, and hooks. Check ~/plugin/skills/ for available skills if relevant to a task.

## Sending messages

Use the send_message tool for ALL replies to the user. Your final text response
is NOT delivered directly — only send_message calls reach the user.

- send_message(text="Here's your answer...") — reply to the user
- send_message(text="Here's the file", media=["/path/to/file.png"]) — with attachment
- send_message(text="Working on it...") then send_message(text="Done!") — multiple messages
"""

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

    # Workspace .claude directory for project-level SDK settings
    (path / "workspace" / ".claude").mkdir(parents=True, exist_ok=True)

    # CLAUDE.md templates — agent identity and workspace context
    user_claude_md = path / ".claude" / "CLAUDE.md"
    if not user_claude_md.exists():
        user_claude_md.write_text(USER_CLAUDE_MD_TEMPLATE)

    project_claude_md = path / "workspace" / ".claude" / "CLAUDE.md"
    if not project_claude_md.exists():
        project_claude_md.write_text(PROJECT_CLAUDE_MD_TEMPLATE)

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
    print(f"  ├── workspace/              # Agent working directory (Claude SDK cwd)")
    print(f"  │   └── .claude/CLAUDE.md   # Project-level agent instructions")
    print(f"  ├── .claude/                # Claude CLI credentials and state")
    print(f"  │   └── CLAUDE.md           # User-level agent instructions")
    print(f"  ├── data/                   # Framework config and state")
    print(f"  │   └── config.toml         # Edit this to configure channels")
    print(f"  └── plugin/                 # Plugin directory (skills, agents, hooks)")
    print()
    print(f"For Docker: CLAWLESS_HOST_DIR={path} docker compose up")
