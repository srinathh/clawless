"""Scaffold the clawless home directory structure.

Usage: clawless-init [path]
Default path: ~/clawless_home
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROJECT_CLAUDE_MD_TEMPLATE = """\
# Clawless Personal Assistant

You are a personal AI assistant running on the Clawless framework. Users reach you \
through messaging channels (currently WhatsApp).

## Communication style

- Be concise and conversational — this is a chat, not a document
- Skip preamble. Answer directly
- Channel-specific formatting rules are provided in each message — follow them
- When a task is done, say so briefly. Don't recap what you did unless asked

## Workspace

Your working directory is ~/workspace/.

## Extensibility
- To CREATE new skills or agents: use ~/workspace/plugin/ (writable plugin)
  - Skills: ~/workspace/plugin/skills/<name>/SKILL.md
  - Agents: ~/workspace/plugin/agents/<name>.md
- Pre-configured extensions from the user are in ~/plugin/ (read-only, do not modify)
"""

CONFIG_TEMPLATE = """\
# Clawless configuration — place at ~/clawless.toml
# In Docker: /home/clawless/clawless.toml

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
    for subdir in [".claude", "workspace", "data", "logs"]:
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

    # CLAUDE.md — agent identity and workspace context (project-level only)
    project_claude_md = path / "workspace" / ".claude" / "CLAUDE.md"
    if not project_claude_md.exists():
        project_claude_md.write_text(PROJECT_CLAUDE_MD_TEMPLATE)

    # Writable plugin inside workspace — agent creates skills/agents here
    ws_plugin = path / "workspace" / "plugin"
    for ws_plugin_subdir in [".claude-plugin", "skills", "agents", "commands", "hooks"]:
        (ws_plugin / ws_plugin_subdir).mkdir(parents=True, exist_ok=True)
    ws_manifest = ws_plugin / ".claude-plugin" / "plugin.json"
    if not ws_manifest.exists():
        ws_manifest.write_text(json.dumps({"name": "workspace-plugin"}, indent=2) + "\n")

    # Config template
    config_dest = path / "clawless.toml"
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
    print(f"  ├── .claude/                # SDK runtime state (sessions, memory)")
    print(f"  ├── workspace/              # Agent working directory (Claude SDK cwd)")
    print(f"  │   ├── .claude/            # Project-level SDK settings")
    print(f"  │   │   └── CLAUDE.md       # Agent instructions")
    print(f"  │   └── plugin/             # Writable plugin (bot-created skills/agents)")
    print(f"  │       ├── skills/         # Bot-created skills")
    print(f"  │       └── agents/         # Bot-created agents")
    print(f"  ├── data/                   # App runtime state (session map)")
    print(f"  ├── logs/                   # Application logs")
    print(f"  ├── clawless.toml           # Edit this to configure channels")
    print(f"  └── plugin/                 # Pre-configured plugin (read-only in Docker)")
    print()
    print(f"For Docker: CLAWLESS_HOST_DIR={path} docker compose up")
