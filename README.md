# Clawless

Minimal self-hosted personal AI assistant connecting messaging channels to the [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk).

## Quick start

```bash
pip install .                     # or: uv pip install .
clawless-init ~/my-data           # scaffold home directory structure
# edit ~/my-data/data/config.toml — configure at least one channel
```

### Run locally

```bash
ANTHROPIC_API_KEY=sk-... clawless
```

### Run with Docker

Two auth modes — set one or the other:

```bash
# Option 1: API key
CLAWLESS_HOST_DIR=~/my-data ANTHROPIC_API_KEY=sk-... docker compose up

# Option 2: Claude credentials file (subscription auth)
CLAWLESS_HOST_DIR=~/my-data CLAUDE_CREDENTIALS_FILE=~/.claude/.credentials.json docker compose up
```

## Running tests

Tests create isolated home dirs under `./data/<timestamp>/`. Integration tests require `ANTHROPIC_API_KEY` or `~/.claude/.credentials.json`.

```bash
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

Use `-s` to see agent responses printed during integration tests.

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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed architecture and design decisions.
