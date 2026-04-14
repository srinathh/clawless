# Clawless — Architecture

## Overview

Clawless is a minimal, self-hosted personal AI assistant that connects messaging
channels to the Claude Agent SDK. A FastAPI app wires together configuration,
a message store (SQLite bus), agent session management, and pluggable messaging
channels — all running in Docker.

```
Messaging Platform ──webhook──> Channel ──store.store_message()──> SQLite
                                                                     │
Message Loop (polls SQLite) ────────────────────────────────────────>│
  ├─ Routes to channel by sender prefix                              │
  └─ AgentManager.process_message()                                  │
       ├─ SDK query with structured output schema                    │
       └─ ResultMessage.structured_output → {"text": "...", "media": [...]}
            └─ HOST sends via channel.send()
```

## Directory Convention

Everything lives under `~` (home dir of the `clawless` user in Docker). The
`clawless-init` command scaffolds this structure at any path.

```
~/
├── .claude/                # SDK runtime state (sessions, memory). rw
├── workspace/              # Claude SDK cwd — agent operates here. rw
│   ├── media/              # Channel media files (auto-created at runtime)
│   │   ├── inbound/        # Downloaded from messaging platforms
│   │   └── outbound/       # Staged for sending via messaging platforms
│   ├── .claude/
│   │   └── CLAUDE.md       # Agent instructions (identity, workspace, extensibility)
│   ├── wiki/               # Markdown wiki, served at /wiki (agent-writable)
│   └── plugin/             # Writable plugin — bot-created skills/agents
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── skills/         # Bot-created skills (writable)
│       └── agents/         # Bot-created agents (writable)
├── data/                   # App runtime state. rw, NOT agent-accessible
│   └── clawless.db         # SQLite message store (sessions, messages, cursors)
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
(sessions, memory) lives in `~/.claude/` (the default SDK location).

**CLAUDE.md**: `clawless-init` scaffolds a single CLAUDE.md at
`~/workspace/.claude/CLAUDE.md`, loaded by the SDK via `setting_sources=["user", "project"]`.
It defines the agent's identity, communication style, and workspace context. Framework
internals (structured output, media handling, plugin info) are in the `system_prompt`
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
│   ├── request_timeout: float = 300.0
│   └── bot_name: str = "Clawless"
└── channels: ChannelsConfig        # at least one required (model_validator)
    ├── twilio_whatsapp: TwilioWhatsAppConfig | None
    └── test: TestChannelConfig | None
```

`anthropic_api_key` is required — pydantic raises `ValidationError` if missing.
A `model_validator` also enforces that at least one channel is configured.

## Message Store

`MessageStore` in `store.py` is an SQLite database (WAL mode) with three tables:

| Table | Purpose |
|-------|---------|
| `sessions` | Sender → agent session UUID (conversation continuity) |
| `messages` | Inbound message bus + dedup (PK = channel-provided ID) |
| `cursors` | Per-sender watermark for crash recovery |

The store serves as an **inbound message bus**: channels write messages to the
store, and the message loop polls for unprocessed messages. This decouples
message receipt from processing and supports both webhook and polling channels.

**No outbound storage** — the agent SDK session maintains conversation history
internally. Outbound storage can be added later for observability.

## Agent Session Management

`AgentManager` in `agent.py` maintains one `ClaudeSDKClient` per sender with:

- **Per-sender locks**: Messages from the same sender are serialized
- **Global semaphore**: Caps total concurrent SDK calls (default: 3)
- **Session persistence**: `sessions` table maps sender -> CLI session UUID,
  allowing conversation resumption across restarts
- **Request timeout**: Configurable timeout (default 300s) prevents hung SDK calls
- **Message loop**: Polls the store for unprocessed messages, routes to channels

### ClaudeAgentOptions

Each SDK client is configured with:

| Option | Value |
|--------|-------|
| `system_prompt` | `{"type": "preset", "preset": "claude_code", "append": FRAMEWORK_SYSTEM_PROMPT}` |
| `cwd` | `~/workspace` |
| `permission_mode` | `bypassPermissions` (requires non-root user) |
| `setting_sources` | `["user", "project"]` |
| `plugins` | `[{"type": "local", "path": "~/plugin"}]` if manifest exists |
| `resume` | Persisted session UUID if available |
| `output_format` | `{"type": "json_schema", "schema": RESPONSE_SCHEMA}` |
| `mcp_servers` | `{"clawless": <in-process MCP server>}` (empty, for future tools) |
| `allowed_tools` | Built-in SDK tools + `mcp__clawless__*` |

### Structured Output

The agent's final response is constrained to a JSON schema via the SDK's
`output_format` option:

```json
{"text": "Message text for the user", "media": ["/path/to/file.png"]}
```

The host reads `ResultMessage.structured_output` and calls `channel.send()`
with the text and media. No custom MCP tools are needed for message delivery —
this eliminates the class of loop bugs where the agent's send_message tool
caused infinite loops.

### Two-Layer Prompt Design

Framework instructions are split into two layers:

- **`system_prompt`** (in code, not user-editable): Framework internals —
  structured output format, workspace paths, media handling, plugin info. Uses
  `SystemPromptPreset` with `preset="claude_code"` to preserve built-in Claude
  Code tool instructions, appending framework-specific instructions via `append`.
- **CLAUDE.md** (user-editable): Identity, personality, communication style, and
  workspace context. Single file at `~/workspace/.claude/CLAUDE.md`, loaded via
  `setting_sources=["project"]`.

### Message Processing Flow

1. Channel webhook receives message, writes to store (`store.store_message()`)
2. Returns acknowledgment immediately (e.g. TwiML "Thinking...")
3. Message loop polls store, finds unprocessed messages
4. Routes to channel by sender prefix (e.g. `whatsapp:` → WhatsApp channel)
5. Creates `asyncio.create_task(agent.process_message(msg, channel))`
6. AgentManager acquires per-sender lock + semaphore slot
7. Advances cursor optimistically, saves old cursor for rollback
8. Builds prompt with channel formatting instructions + user content
9. Sends to Claude via SDK with structured output schema
10. Captures session ID from `SystemMessage.init`, persists to store
11. Reads `ResultMessage.structured_output` → `{"text": "...", "media": [...]}`
12. Host sends text + media via `channel.send()`
13. On error before output: rolls back cursor (message reprocessed on restart)
14. On error after output: keeps cursor (prevents duplicate sends)
15. On no output: sends failure message to user

### Cursor-Based Crash Recovery (Nanoclaw Pattern)

Per-sender cursors track which messages have been processed:

- **Success**: Cursor advanced past processed message
- **Error after output sent**: Cursor stays advanced (prevents duplicate sends)
- **Error before output**: Cursor rolled back (allows retry on restart)

This follows the same pattern as nanoclaw's `lastAgentTimestamp`.

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
    message_id: str = ""     # platform-provided or channel-generated UUID
    media_files: list[str] = []
    metadata: dict = {}
```

**Sender namespacing**: Sender IDs are globally unique across channels
(e.g. `whatsapp:+1234567890`, `test:user1`) and double as session keys.
The message loop uses sender prefixes to route replies to the correct channel.

### WhatsApp Channel (`channels/whatsapp.py`)

- Registers `POST {webhook_path}` for Twilio webhooks
- Validates Twilio request signatures
- Enforces sender allowlist (no allow-all)
- Downloads inbound media via Twilio API with Basic Auth to `workspace/media/inbound/`
- Tags media in message content as `[mime/type: /path/to/file]`
- Writes inbound messages to the store (dedup via PK on MessageSid)
- Returns immediate TwiML acknowledgment ("Thinking...")
- Splits outgoing text at 1600 chars (Twilio limit) using `split_text()`
- Stages outbound media to `workspace/media/outbound/` and serves via GET endpoint

### Test Channel (`channels/test.py`)

- Writes scripted messages to the store with UUID message IDs on startup
- Waits for the message loop to process all messages
- Captures all responses in an ordered list via `send()`
- Exposes `GET /test/responses` and `GET /test/status` for assertions

## Plugin System

`~/plugin/` is a single Claude Code plugin with the prescribed structure:
`.claude-plugin/plugin.json`, `skills/`, `agents/`, `commands/`, `hooks/`.

If `~/plugin/.claude-plugin/` exists, it's passed to the SDK as:
```python
plugins=[{"type": "local", "path": str(plugin_dir)}]
```

Skills are auto-namespaced by the SDK (e.g. `private-plugin:my-skill`).

## Wiki Endpoint

`wiki.py` exposes a read-only web view of the agent's `~/workspace/wiki/`
directory.

| Route | Behaviour |
|---|---|
| `GET /wiki` | HTML index listing all `.md` files in `workspace/wiki` |
| `GET /wiki/{path}` | Renders the named markdown file as HTML (`.md` extension optional) |

The router is registered during lifespan via `app.include_router(make_wiki_router(paths.workspace))`.

`workspace/wiki/` is agent-accessible (inside the agent's cwd), so the agent
can create and edit wiki pages directly. The endpoint returns 404 if the wiki
directory or requested page doesn't exist, and 403 for any path traversal
attempt outside the wiki root.

Markdown is rendered with Python-Markdown using the `fenced_code`, `tables`,
and `toc` extensions. Output is wrapped in a minimal self-contained HTML page
with light responsive styling.

## Custom MCP Tools

`tools.py` defines an in-process MCP server registered with the agent. Currently
empty — tools can be added here for non-contextual side effects. To add a new tool:
define it with `@tool` in `tools.py`, then add it to the `tools=[]` list in
`build_clawless_mcp_server()`.

## Docker Deployment

**Dockerfile**: Python 3.13-slim, non-root `clawless` user, Node.js + Claude Code CLI.
Dependencies installed via `uv sync --frozen` from committed `uv.lock` for reproducible
builds. Default port: 18265.

**docker-compose.yml**:

```yaml
volumes:
  - ${CLAWLESS_HOST_DIR}/.claude:/home/clawless/.claude:rw
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

Four levels of tests, all creating isolated home dirs under `./data/<timestamp>/`:

### Unit tests (`test_config.py`, `test_base.py`, `test_utils.py`, `test_store.py`)

Fast, no API key needed. Test configuration loading, path validation, InboundMessage
construction, text splitting, and MessageStore operations (sessions, messages, cursors,
dedup, unprocessed queries).

### Host integration test (`test_channel_integration.py`)

Runs the full pipeline in-process against the real Claude Agent SDK:
- Creates isolated home dir with test channel config
- Starts app via `LifespanManager` + `httpx.AsyncClient(ASGITransport)`
- Session-scoped fixture with `loop_scope="session"` for shared event loop
- Polls `/test/status` until done, then asserts on `/test/responses`
- Verifies `clawless.db` exists (store was created)
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
3. **Message bus architecture** — Channels write to SQLite, message loop polls.
   Decouples receive from processing, supports future non-webhook channels.
4. **Host-controlled delivery via structured output** — Agent returns JSON
   `{"text": "...", "media": [...]}` via SDK's `output_format`. Host sends it.
   Eliminates the dot-spam loop bug class entirely.
5. **Cursor-based crash recovery** — Per-sender cursors with optimistic advance
   and rollback on error (nanoclaw pattern).
6. **Single plugin dir** — `~/plugin/` is one plugin, not a parent of many.
   Keeps the mount simple.
7. **API key in Settings** — `ANTHROPIC_API_KEY` is a required field in `Settings`,
   validated at startup by pydantic.
8. **Two `.claude/` directories** — `~/.claude/` holds SDK runtime state (sessions,
   memory); `~/workspace/.claude/` holds project-level config (CLAUDE.md, skills,
   agents). Both are mounted as Docker volumes for persistence.
9. **Formatting via prompt injection** — Each channel's `formatting_instructions` are
   prepended to the user's message, letting Claude format natively rather than
   post-processing output.
