# Clawless — Architecture

## Overview

Clawless is a minimal, self-hosted personal AI assistant that connects messaging
channels to the Claude Agent SDK. A FastAPI app wires together configuration,
agent session management, and pluggable messaging channels — all running in Docker.

```
Messaging Platform ──webhook──▶ Channel ──fire-and-forget──▶ AgentManager ──SDK──▶ Claude
       ◀──reply──────────────── Channel ◀──channel.send()──── AgentManager ◀──result── Claude
```

## Directory Convention

Everything lives under `~` (home dir of the `clawless` user in Docker). The
`clawless-init` command scaffolds this structure at any path.

```
~/
├── workspace/              # Claude SDK cwd — agent operates here. rw
│   ├── media/              # Channel media files (auto-created at runtime)
│   └── .claude/            # Project-level settings + CLAUDE.md
├── .claude/                # User-level SDK settings + credentials
│   ├── settings.json       # Loaded via setting_sources=["user"]
│   └── .credentials.json   # API credentials (mountable from Docker host)
├── data/                   # Framework state. rw, NOT agent-accessible
│   ├── config.toml         # App config (channels, claude options)
│   └── claude_sessions.json # Session persistence (auto-created)
└── plugin/                 # Single plugin dir with prescribed structure
    ├── .claude-plugin/
    │   └── plugin.json
    ├── skills/
    ├── agents/
    ├── commands/
    └── hooks/
```

**Path management**: `ClawlessPaths` in `config.py` derives all paths from
`Path.home()` and validates required dirs exist on construction. No configurable
path fields — everything is conventional.

**Bootstrap**: The SDK reads `~/.claude` automatically. Our framework state lives
in `~/data/` (invisible to the agent since `cwd=~/workspace`). Config is loaded
from `~/data/config.toml`.

## Configuration

`Settings` (pydantic-settings) loads from `~/data/config.toml` with env var
overrides using `__` as nesting delimiter (e.g. `CLAUDE__MAX_TURNS=10`).

```
Settings
├── claude: ClaudeConfig
│   ├── max_turns: int = 30
│   ├── max_budget_usd: float = 1.0
│   └── max_concurrent_requests: int = 3
└── channels: ChannelsConfig
    ├── twilio_whatsapp: TwilioWhatsAppConfig | None
    └── test: TestChannelConfig | None
```

API key is NOT in config — the SDK reads `ANTHROPIC_API_KEY` env var or
`~/.claude/.credentials.json` directly.

## Agent Session Management

`AgentManager` in `agent.py` maintains one `ClaudeSDKClient` per sender with:

- **Per-sender locks**: Messages from the same sender are serialized
- **Global semaphore**: Caps total concurrent SDK calls (default: 3)
- **Session persistence**: `claude_sessions.json` maps sender → CLI session UUID,
  allowing conversation resumption across restarts

### ClaudeAgentOptions

Each SDK client is configured with:

| Option | Value |
|--------|-------|
| `cwd` | `~/workspace` |
| `allowed_tools` | Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch |
| `permission_mode` | `bypassPermissions` (requires non-root user) |
| `setting_sources` | `["user", "project"]` |
| `plugins` | `[{"type": "local", "path": "~/plugin"}]` if manifest exists |
| `resume` | Persisted session UUID if available |

### Message Processing Flow

1. Channel webhook receives message, creates `InboundMessage`
2. Fires `asyncio.create_task(agent.process_message(msg, channel))` — non-blocking
3. AgentManager acquires per-sender lock + semaphore slot
4. Builds prompt: `[{channel.formatting_instructions}]\n\n{content}`
5. Sends to Claude via SDK, streams response
6. Captures session ID from `SystemMessage.init`, persists mapping
7. Extracts final text from `ResultMessage.result`
8. Replies via `channel.send(sender, text)`

## Channel Architecture

### Protocol

Every channel implements `Channel` (a Python Protocol):

```python
class Channel(Protocol):
    name: str                       # e.g. "twilio-whatsapp", "test"
    formatting_instructions: str    # Prepended to every prompt

    async def send(self, to: str, text: str = "", media: list[str] | None = None) -> None: ...
```

`InboundMessage` carries the parsed incoming message:

```python
@dataclass
class InboundMessage:
    sender: str              # channel-namespaced, e.g. "whatsapp:+1234567890"
    sender_name: str = ""
    content: str = ""        # text body, may include [mime: path] tags
    media_files: list[str] = []
    metadata: dict = {}
```

**Sender namespacing**: Sender IDs are globally unique across channels
(e.g. `whatsapp:+1234567890`, `test:user1`) and double as session keys.

### WhatsApp Channel (`channels/whatsapp.py`)

- Registers `POST {webhook_path}` for Twilio webhooks
- Validates Twilio request signatures
- Enforces sender allowlist (no allow-all)
- Downloads media via Twilio API with Basic Auth
- Splits outgoing text at 1600 chars (Twilio limit)
- Stages outbound media via `GET {webhook_path}/media/{filename}`

### Test Channel (`channels/test.py`)

- Feeds scripted messages to the agent sequentially on startup
- Captures all responses in an ordered list
- Exposes `GET /test/responses` and `GET /test/status` for assertions
- Used for integration testing against the real Claude Agent SDK

## Plugin System

`~/plugin/` is a single Claude Code plugin with the prescribed structure:
`.claude-plugin/plugin.json`, `skills/`, `agents/`, `commands/`, `hooks/`.

If `~/plugin/.claude-plugin/` exists, it's passed to the SDK as:
```python
plugins=[{"type": "local", "path": str(plugin_dir)}]
```

Skills are auto-namespaced by the SDK (e.g. `private-plugin:my-skill`).

## Docker Deployment

**Dockerfile**: Python 3.13-slim, non-root `clawless` user, Node.js + Claude Code CLI.

**docker-compose.yml**: Single bind mount of host dir → `/home/clawless`:

```yaml
volumes:
  - ${CLAWLESS_HOST_DIR}:/home/clawless:rw
environment:
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
```

**Setup**:
```bash
clawless-init ~/my-clawless-data    # scaffold on host
# edit ~/my-clawless-data/data/config.toml
CLAWLESS_HOST_DIR=~/my-clawless-data ANTHROPIC_API_KEY=sk-... docker compose up
```

## Testing

### Unit tests (`tests/test_config.py`, `test_base.py`, `test_utils.py`)

Each test creates an isolated home dir under `./data/<uuid>/` using `init_home()`,
sets `HOME` to point there, and restores it after. This exercises the same setup
path as production.

### Integration tests (`tests/test_channel_integration.py`)

Runs the full pipeline against the real Claude Agent SDK:
- Creates isolated home dir with test channel config
- Starts app via `LifespanManager` + `httpx.AsyncClient(ASGITransport)`
- Session-scoped fixture with `loop_scope="session"` for shared event loop
- Polls `/test/status` until done, then asserts on `/test/responses`

## Key Design Decisions

1. **Home dir convention over configuration** — No configurable paths. Everything
   derives from `~`. `ClawlessPaths` validates on construction.
2. **Sender namespacing** — Channel-prefixed IDs (`whatsapp:+123`) are globally
   unique, serving as both reply address and session key.
3. **Fire-and-forget webhooks** — Return acknowledgment immediately, process async.
4. **Single plugin dir** — `~/plugin/` is one plugin, not a parent of many.
   Keeps the mount simple.
5. **SDK reads credentials** — `ANTHROPIC_API_KEY` and `~/.claude/.credentials.json`
   are handled by the SDK, not our config.
6. **setting_sources=["user", "project"]** — SDK loads both `~/.claude/settings.json`
   and `~/workspace/.claude/settings.json` + CLAUDE.md.
