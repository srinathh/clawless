# Clawless

Minimal self-hosted personal AI assistant connecting messaging channels to the [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk).

## Why?

The Claude Agent SDK now ships with sessions, memory, skills, hooks, MCP servers, and context compaction built in. The middleware frameworks that existed before these features landed are no longer necessary. Clawless is the minimal glue — a few hundred lines of Python, a Dockerfile, and your private config — to turn the SDK into a personal assistant reachable via messaging.

The app is fully open source. All personalization — identity, skills, credentials, conversation history — lives outside the repo, injected at runtime via a single bind mount. Clone the repo, mount your own config, and you have your own assistant to runwith docker.

## What you get

Clawless gives you a personal Claude-powered assistant reachable via WhatsApp (and other channels). It runs as a single Docker container, persists conversation sessions across restarts, and supports the full Claude Code toolset — file operations, web search, code execution — all driven by natural-language chat.

## Features

*   **Messaging channel integration** — WhatsApp via Twilio, with a pluggable channel architecture for adding more
*   **Full Claude Code capabilities** — the agent has access to the complete Claude Code toolset, not just chat
*   **Session persistence** — conversations resume across restarts via SQLite-backed session storage
*   **Concurrency controls** — per-sender message serialization and a global semaphore to cap parallel SDK calls
*   **Plugin system** — extend the agent with custom skills, commands, and hooks using Claude Code's plugin format
*   **Customizable personality** — edit CLAUDE.md files to shape the agent's identity and communication style
*   **Media support** — send and receive images, documents, and other files through messaging channels
*   **Docker-first deployment** — single container with two auth modes (API key or Claude credentials file)

## How it works

```
Messaging Platform ──webhook──> Channel ──fire-and-forget──> AgentManager ──SDK──> Claude
       <──reply──────────────── Channel <──channel.send()──── AgentManager <──result── Claude
```

1.  A message arrives at a channel webhook (e.g. Twilio WhatsApp)
2.  The channel returns an immediate acknowledgment and fires off the message to the AgentManager
3.  The AgentManager acquires a per-sender lock (serializing messages from the same user) and a concurrency slot
4.  It sends the message to Claude via the Agent SDK, streaming the response
5.  Claude processes the request using its full toolset and replies via a `send_message` MCP tool
6.  The channel delivers the reply back through the messaging platform

## Quick start

### Prerequisites

*   Docker and Docker Compose
*   An [Anthropic API key](https://console.anthropic.com/) or Claude subscription credentials
*   For WhatsApp: a [Twilio](https://www.twilio.com/) account with WhatsApp sandbox or number

### Install and scaffold

```
pip install .                     # or: uv pip install .
clawless-init ~/my-data           # scaffold the home directory structure
# edit ~/my-data/data/config.toml — configure at least one channel
```

This creates the following structure:

```
~/my-data/
├── workspace/              # Agent's working directory
│   └── .claude/CLAUDE.md   # Project-level instructions (editable)
├── .claude/
│   └── CLAUDE.md           # Agent identity & communication style (editable)
├── data/
│   └── config.toml         # Channel and agent configuration
└── plugin/                 # Custom skills, hooks, commands
    ├── .claude-plugin/plugin.json
    ├── skills/
    ├── agents/
    ├── commands/
    └── hooks/
```

### Configure

Edit `~/my-data/data/config.toml`:

```
[claude]
max_turns = 30
max_budget_usd = 1.0
max_concurrent_requests = 3

[channels.twilio_whatsapp]
account_sid = "AC..."
auth_token = "your-auth-token"
whatsapp_from = "whatsapp:+14155238886"
public_url = "https://your-domain.ngrok-free.app"
ack_message = "Thinking..."
allowed_senders = ["whatsapp:+1234567890"]
```

### Run with Docker

```
# Option 1: API key
CLAWLESS_HOST_DIR=~/my-data ANTHROPIC_API_KEY=sk-... docker compose up

# Option 2: Claude credentials file (subscription auth)
CLAWLESS_HOST_DIR=~/my-data CLAUDE_CREDENTIALS_FILE=~/.claude/.credentials.json docker compose up
```

## Configuration

Configuration is loaded from `~/data/config.toml` with environment variable overrides using `__` as the nesting delimiter (e.g. `CLAUDE__MAX_TURNS=10`).

| Setting | Default | Description |
| --- | --- | --- |
| `port` | `18265` | HTTP server port |
| `claude.max_turns` | `30` | Maximum agent turns per request |
| `claude.max_budget_usd` | `1.0` | Budget cap per request |
| `claude.max_concurrent_requests` | `3` | Max parallel SDK calls |
| `claude.request_timeout` | `300.0` | Timeout per request (seconds) |

The Anthropic API key is **not** part of the config — the SDK reads it from the `ANTHROPIC_API_KEY` environment variable or `~/.claude/.credentials.json`.

## Channels

### WhatsApp (Twilio)

The WhatsApp channel handles the full Twilio webhook lifecycle:

*   Validates request signatures for security
*   Enforces a sender allowlist (no open access)
*   Downloads inbound media (images, documents) to `workspace/media/inbound/`
*   Splits outgoing messages at 1600 characters (Twilio limit)
*   Serves outbound media files for Twilio to fetch
*   Returns an immediate acknowledgment ("Thinking...") to avoid Twilio's 15-second webhook timeout

Set your Twilio webhook URL to `https://your-domain/whatsapp/webhook`.

### Adding channels

Channels implement a simple Protocol:

```python
class Channel(Protocol):
    name: str
    formatting_instructions: str

    async def send(self, to: str, text: str = "", media: list[str] | None = None) -> None: ...
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full channel contract.

## Customization

### Agent personality

Edit `~/.claude/CLAUDE.md` (user-level) to change the agent's identity and communication style. Edit `~/workspace/.claude/CLAUDE.md` (project-level) for workspace-specific instructions.

Both files are created by `clawless-init` with sensible defaults and are never overwritten on re-runs.

### Plugins

The `~/plugin/` directory is a single [Claude Code plugin](https://docs.anthropic.com/en/docs/claude-code/plugins). Add custom skills, hooks, or commands following the plugin format. If `~/plugin/.claude-plugin/` exists, it's automatically loaded by the SDK.

## Project structure

```
src/clawless/
├── config.py          # ClawlessPaths, Settings, all config models
├── app.py             # FastAPI lifespan, channel wiring, entry point
├── agent.py           # AgentManager — SDK client lifecycle, session persistence
├── tools.py           # MCP server with send_message tool
├── init.py            # clawless-init command — scaffolds home directory
├── utils.py           # Text splitting utility
└── channels/
    ├── base.py        # Channel protocol, InboundMessage dataclass
    ├── whatsapp.py    # Twilio WhatsApp implementation
    └── test.py        # Test channel for integration testing
```

## Running tests

Tests create isolated home dirs under `./data/<timestamp>/` and set `HOME` to point there.

```
# Unit tests (fast, no API key needed)
uv run pytest tests/test_config.py -v

# Host integration test (runs app in-process, ~2 min)
uv run pytest tests/test_channel_integration.py -v -s

# Docker integration test (builds image, runs via docker compose, ~2-3 min)
uv run pytest -m docker tests/test_docker_integration.py -v -s

# All tests except Docker
uv run pytest tests/ -v -s

# Everything including Docker
uv run pytest -m '' tests/ -v -s
```

Use `-s` to see agent responses printed during integration tests. Integration tests require `ANTHROPIC_API_KEY` or `~/.claude/.credentials.json`.

## Status and roadmap

Phase 1 (MVP) is complete — core loop, WhatsApp channel, session persistence, Docker deployment, and three tiers of tests are all working.

| Phase | Status | Scope |
| --- | --- | --- |
| 1\. Core loop (MVP) | **Complete** | FastAPI + Agent SDK, WhatsApp channel, session persistence, Docker, test suite |
| 2\. Media & channels | Planned | Multimodal input, additional channels (Telegram, etc.), MCP server integrations |
| 3\. Scheduling | Planned | Proactive messages, daily briefings, reminders |
| 4\. Hardening | Planned | Health checks, cost tracking, backup strategy |

See [docs/specs/SPEC.md](docs/specs/SPEC.md) for the full design spec and research notes.

## Documentation

*   [Architecture](docs/ARCHITECTURE.md) — system design, data flow, and design decisions
*   [Code walkthrough](docs/CODE_WALKTHROUGH.md) — file-by-file implementation guide
*   [Design spec](docs/specs/SPEC.md) — original spec with goals, non-goals, research questions, and implementation status