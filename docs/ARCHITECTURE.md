# Clawless — Architecture

## Overview

Clawless is a minimal, self-hosted personal AI assistant that connects messaging
channels to the Claude Agent SDK. A FastAPI app wires together configuration,
agent session management, and pluggable messaging channels — all running in Docker.

```
Messaging Platform ──webhook──> Channel ──fire-and-forget──> AgentManager ──SDK──> Claude
       <──reply──────────────── Channel <──channel.send()──── AgentManager <──result── Claude
```

## Directory Convention

Everything lives under `~` (home dir of the `clawless` user in Docker). The
`clawless-init` command scaffolds this structure at any path.

```
~/
├── workspace/              # Claude SDK cwd — agent operates here. rw
│   ├── media/              # Channel media files (auto-created at runtime)
│   │   ├── inbound/        # Downloaded from messaging platforms
│   │   └── outbound/       # Staged for sending via messaging platforms
│   └── .claude/
│       ├── CLAUDE.md       # Agent instructions (identity, workspace, extensibility)
│       ├── skills/         # Bot-created skills (standalone format, writable)
│       └── agents/         # Bot-created agents (standalone format, writable)
├── data/                   # Runtime state. rw, NOT agent-accessible
│   └── sessions.db         # Session persistence via sqlitedict (auto-created)
├── clawless.toml           # App config (channels, claude options). ro in Docker
└── plugin/                 # Single plugin dir with prescribed structure. ro in Docker
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

**Bootstrap**: Framework state lives in `~/data/` (invisible to the agent since
`cwd=~/workspace`). Config is loaded from `~/clawless.toml`. SDK runtime state
(sessions) is redirected to `~/workspace/.claude/` via `CLAUDE_CONFIG_DIR`.

**CLAUDE.md**: `clawless-init` scaffolds a single CLAUDE.md at
`~/workspace/.claude/CLAUDE.md`, loaded by the SDK via `setting_sources=["project"]`.
It defines the agent's identity, communication style, and workspace context. Framework
internals (send_message usage, media handling, plugin info) are in the `system_prompt`
parameter instead. Written only if it doesn't already exist, so user customizations
survive re-runs of init.

## Configuration

`Settings` (pydantic-settings) loads config from three sources (highest priority wins):
1. Environment variables (with `__` as nesting delimiter, e.g. `CLAUDE__MAX_TURNS=10`)
2. `.env` file in CWD (gracefully ignored if missing)
3. `~/clawless.toml` (gracefully ignored if missing)

```
Settings
├── anthropic_api_key: str          # required
├── port: int = 18265
├── claude: ClaudeConfig
│   ├── max_turns: int = 30
│   ├── max_budget_usd: float = 1.0
│   ├── max_concurrent_requests: int = 3
│   └── request_timeout: float = 300.0
└── channels: ChannelsConfig        # at least one required (model_validator)
    ├── twilio_whatsapp: TwilioWhatsAppConfig | None
    └── test: TestChannelConfig | None
```

`anthropic_api_key` is required — pydantic raises `ValidationError` if missing.
A `model_validator` also enforces that at least one channel is configured.

## Agent Session Management

`AgentManager` in `agent.py` maintains one `ClaudeSDKClient` per sender with:

- **Per-sender locks**: Messages from the same sender are serialized
- **Global semaphore**: Caps total concurrent SDK calls (default: 3)
- **Session persistence**: `sessions.db` (sqlitedict) maps sender -> CLI session UUID,
  allowing conversation resumption across restarts
- **Request timeout**: Configurable timeout (default 300s) prevents hung SDK calls
  from blocking the system

### ClaudeAgentOptions

Each SDK client is configured with:

| Option | Value |
|--------|-------|
| `system_prompt` | `{"type": "preset", "preset": "claude_code", "append": FRAMEWORK_SYSTEM_PROMPT}` |
| `cwd` | `~/workspace` |
| `permission_mode` | `bypassPermissions` (requires non-root user) |
| `setting_sources` | `["project"]` |
| `env` | `{"CLAUDE_CONFIG_DIR": "~/workspace/.claude"}` |
| `plugins` | `[{"type": "local", "path": "~/plugin"}]` if manifest exists |
| `resume` | Persisted session UUID if available |
| `mcp_servers` | `{"clawless": <in-process MCP server>}` with `send_message` tool |
| `allowed_tools` | Built-in SDK tools + `mcp__clawless__*` (MCP tools require explicit allowlisting) |

### Two-Layer Prompt Design

Framework instructions are split into two layers:

- **`system_prompt`** (in code, not user-editable): Framework internals — send_message
  tool usage, workspace paths, media handling, plugin info. Uses `SystemPromptPreset`
  with `preset="claude_code"` to preserve built-in Claude Code tool instructions,
  appending framework-specific instructions via the `append` field.
- **CLAUDE.md** (user-editable): Identity, personality, communication style, and
  workspace context. Single file at `~/workspace/.claude/CLAUDE.md`, loaded via
  `setting_sources=["project"]`.

### Message Processing Flow

1. Channel webhook receives message, creates `InboundMessage`
2. Fires `asyncio.create_task(agent.process_message(msg, channel))` — non-blocking
3. AgentManager acquires per-sender lock + semaphore slot
4. Sets tool context (`set_context(channel, sender)`) for the `send_message` MCP tool
5. Builds prompt with channel formatting instructions + user content
6. Sends to Claude via SDK, streams response (with `request_timeout` guard)
7. Captures session ID from `SystemMessage.init`, persists to sqlitedict
8. Agent uses `send_message` tool to deliver replies via `channel.send()`
9. If agent didn't use the tool: logs warning, delivers `ResultMessage.result` as fallback
10. On timeout: closes the client, notifies the user

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
- Downloads inbound media via Twilio API with Basic Auth to `workspace/media/inbound/`
- Tags media in message content as `[mime/type: /path/to/file]`
- Splits outgoing text at 1600 chars (Twilio limit) using `split_text()`
- Stages outbound media to `workspace/media/outbound/` and serves via `GET {webhook_path}/media/{filename}` with path traversal protection
- Returns immediate TwiML acknowledgment ("Thinking...") to meet Twilio's 15-second timeout
- Sends actual response asynchronously via Twilio REST API

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

## Custom MCP Tools

`tools.py` defines an in-process MCP server with custom tools available to the agent.
Tools are defined at module level with `@tool` and registered in `build_clawless_mcp_server()`.
To add a new tool: define it with `@tool` in `tools.py`, then add it to the `tools=[]` list.

### send_message

The **only way** the agent communicates with the user. The `system_prompt` parameter
instructs the agent to always use this tool for replies. The tool handler calls `channel.send()` as
a side effect. Per-request context (channel, sender) is set via `set_context()` before
each query. If the agent fails to use the tool, a warning is logged and the SDK's final
result text is sent as a fallback.

## Docker Deployment

**Dockerfile**: Python 3.13-slim, non-root `clawless` user, Node.js + Claude Code CLI.
Default port: 18265.

**docker-compose.yml**:

```yaml
volumes:
  - ${CLAWLESS_HOST_DIR}/workspace:/home/clawless/workspace:rw
  - ${CLAWLESS_HOST_DIR}/data:/home/clawless/data:rw
  - ${CLAWLESS_HOST_DIR}/clawless.toml:/home/clawless/clawless.toml:ro
  - ${CLAWLESS_HOST_DIR}/plugin:/home/clawless/plugin:ro
environment:
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
  PORT: ${PORT:-18265}
```

`ANTHROPIC_API_KEY` is required. Set it in `.env` or pass via environment.

**Setup**:
```
clawless-init ~/my-data
# edit ~/my-data/clawless.toml
# set ANTHROPIC_API_KEY in .env or environment
CLAWLESS_HOST_DIR=~/my-data docker compose up
```

## Testing

Three levels of tests, all creating isolated home dirs under `./data/<timestamp>/`:

### Unit tests (`test_config.py`, `test_base.py`, `test_utils.py`)

Fast, no API key needed. Test configuration loading, path validation, InboundMessage
construction, and text splitting. Each test creates an isolated home dir via `init_home()`,
sets `HOME` to point there, and restores it after.

### Host integration test (`test_channel_integration.py`)

Runs the full pipeline in-process against the real Claude Agent SDK:
- Creates isolated home dir with test channel config
- Starts app via `LifespanManager` + `httpx.AsyncClient(ASGITransport)`
- Session-scoped fixture with `loop_scope="session"` for shared event loop
- Polls `/test/status` until done, then asserts on `/test/responses`
- Prints agent responses to stdout
- Asserts responses don't contain "not logged in" (auth failure detection)

### Docker integration test (`test_docker_integration.py`)

Builds and runs the full Docker container, skipped by default (`@pytest.mark.docker`):
- Creates isolated home dir, scaffolds config with test channel
- Requires `ANTHROPIC_API_KEY` env var (skips if not set)
- Runs `docker compose up -d --build` with `PORT=18266`
- Waits for `/health` (up to 5 min, progress printed every 10s)
- Streams docker compose stdout/stderr to terminal
- Polls `/test/status`, asserts on `/test/responses`
- Tears down with `docker compose down -v`

Run with: `uv run pytest -m docker -v -s`

## Key Design Decisions

1. **Home dir convention over configuration** — No configurable paths. Everything
   derives from `~`. `ClawlessPaths` validates on construction.
2. **Sender namespacing** — Channel-prefixed IDs (`whatsapp:+123`) are globally
   unique, serving as both reply address and session key.
3. **Fire-and-forget webhooks** — Return acknowledgment immediately, process async.
4. **Single plugin dir** — `~/plugin/` is one plugin, not a parent of many.
   Keeps the mount simple.
5. **API key in Settings** — `ANTHROPIC_API_KEY` is a required field in `Settings`,
   validated at startup by pydantic.
6. **setting_sources=["project"] + CLAUDE_CONFIG_DIR** — SDK reads config only from
   `~/workspace/.claude/`. `CLAUDE_CONFIG_DIR` redirects session storage there too,
   eliminating `~/.claude/` entirely.
7. **Formatting via prompt injection** — Each channel's `formatting_instructions` are
   prepended to the user's message, letting Claude format natively rather than
   post-processing output.
8. **sqlitedict for sessions** — Simple key-value persistence with autocommit,
   crash-safe, no migration overhead.
