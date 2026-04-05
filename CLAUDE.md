# Clawless

Minimal self-hosted personal AI assistant connecting messaging channels to the Claude Agent SDK.

## Before you start

Read through the documentation and plans to understand the architecture:

- `docs/ARCHITECTURE.md` — Current architecture, directory convention, data flow, design decisions
- `docs/CODE_WALKTHROUGH.md` — File-by-file code walkthrough
- `docs/SPEC.md` — Original design spec with implementation status annotations
- `docs/plans/` — Implementation plans (check which are already implemented vs. pending)

## Project structure

```
src/clawless/
├── config.py          # ClawlessPaths, Settings, all config models
├── app.py             # FastAPI lifespan, channel wiring, entry point
├── agent.py           # AgentManager — SDK client lifecycle, session persistence
├── init.py            # clawless-init command — scaffolds home directory
├── utils.py           # Text splitting utility
└── channels/
    ├── base.py        # Channel protocol, InboundMessage dataclass
    ├── whatsapp.py    # Twilio WhatsApp implementation
    └── test.py        # Test channel for integration testing
```

## Key conventions

- All paths derive from `Path.home()` via `ClawlessPaths` — no configurable path fields
- Config sources (highest priority wins): env vars > `.env` file > `~/data/config.toml` (all optional)
- `ANTHROPIC_API_KEY` is required — validated by pydantic-settings at startup
- Sender IDs are channel-namespaced (e.g. `whatsapp:+1234567890`, `test:user1`)
- The `~/plugin/` directory is a single Claude Code plugin, not a container of multiple plugins

## Running tests

Tests create isolated home dirs under `./data/<timestamp>/` and set `HOME` to point there.
Requires `ANTHROPIC_API_KEY` env var for integration tests.

```
# Unit tests (fast, no API key needed)
uv run pytest tests/test_config.py -v

# Host integration test (runs app in-process, ~2 min)
uv run pytest tests/test_channel_integration.py -v -s

# Docker integration test (builds image, runs via docker compose, ~2-3 min)
# Skipped by default — must be explicitly requested with -m docker
uv run pytest -m docker tests/test_docker_integration.py -v -s

# All tests except Docker
uv run pytest tests/ -v -s

# Everything including Docker
uv run pytest -m '' tests/ -v -s
```

Always use `-s` to see agent responses printed during integration tests.

After running integration tests, review the printed agent responses carefully for unexpected behavior:
- Extra or duplicate responses beyond the scripted messages
- Empty-message replies ("your message came through empty", etc.)
- Agent not using the send_message tool
- Responses that don't match the scripted input

Show the full agent responses to the user so they can review them.

## Docker

```
clawless-init ~/my-data          # scaffold home structure on host
# edit ~/my-data/data/config.toml
# set ANTHROPIC_API_KEY in .env or environment

CLAWLESS_HOST_DIR=~/my-data docker compose up
```
