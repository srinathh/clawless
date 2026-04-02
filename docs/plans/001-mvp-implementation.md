# Clawless MVP Implementation Plan

## Context

Clawless is a self-hosted personal AI assistant built on the Claude Agent SDK. A FastAPI app receives WhatsApp messages via Twilio webhooks, routes them to `ClaudeSDKClient` for processing, and sends replies back. See [SPEC.md](../SPEC.md) for the full architecture.

**MVP goal:** Send a WhatsApp message, get a Claude response back, running in Docker with persistent sessions.

**Key references from [srinathh/nanobot](https://github.com/srinathh/nanobot):**

| Branch | File | Lines | What to port |
|--------|------|-------|-------------|
| `feature/twilio-whatsapp-nightly` | `nanobot/channels/twilio_whatsapp.py` | 334 | Twilio webhook handling, signature validation, media download/upload, message splitting |
| `feature/claude-agent-engine` | `nanobot/agent/claude_agent.py` | 806 | ClaudeSDKClient lifecycle, per-sender session management, session persistence, concurrency |

The nanobot `claude_agent.py` implementation answers all research spike questions — no spikes needed.

---

## Research Spikes: Answered by Nanobot

### Client Lifecycle — ANSWERED

From `claude_agent.py`:
- **One `ClaudeSDKClient` per sender** (keyed by `channel:chat_id`), stored in `self._clients: dict[str, _SessionClient]`
- Client is created via `ClaudeSDKClient(options=options)` then `await client.__aenter__()`
- Client stays alive across messages — reused for subsequent queries from the same sender
- Closed via `await client.__aexit__(None, None, None)`

### Session Persistence — ANSWERED

From `claude_agent.py`:
- Session IDs are captured from `SystemMessage.data.get("session_id")` during `receive_response()`
- Mapped to sender key and persisted to `workspace/claude_sessions.json`
- On restart, `options.resume = cli_session_id` reconnects to the previous session
- `/new` slash command closes client with `forget=True` to start fresh

### Concurrency — ANSWERED

From `claude_agent.py`:
- **Per-sender `asyncio.Lock`** in `self._session_locks` — serializes messages from the same sender
- **Global `asyncio.Semaphore`** (`NANOBOT_MAX_CONCURRENT_REQUESTS=3`) — caps total concurrent SDK calls
- Messages dispatched via `asyncio.create_task(self._dispatch(msg))` with task tracking

### Query Pattern — ANSWERED

From `claude_agent.py`:
```python
await sc.client.query(msg.content)
async for message in sc.client.receive_response():
    if isinstance(message, SystemMessage) and message.subtype == "init":
        # capture session_id
    elif isinstance(message, AssistantMessage):
        # stream text blocks
    elif isinstance(message, ResultMessage):
        # final response in message.result
```

### MCP Servers — ANSWERED

From `claude_agent.py`:
- MCP servers passed as dict to `ClaudeAgentOptions(mcp_servers={...})`
- In-process MCP server created via `create_sdk_mcp_server()` for custom tools (message, cron)
- External MCP servers from config merged with in-process server

### Allowed Tools — ANSWERED

From `claude_agent.py`:
```python
allowed_tools = [
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "WebSearch", "WebFetch",
    "mcp__nanobot__*",  # in-process tools
]
# Plus wildcards for external MCP servers
```

### Permission Mode — ANSWERED

Uses `permission_mode="bypassPermissions"` — runs all tools without prompting. Safe because the container runs as non-root `appuser` (required by the SDK for this mode).

---

## Implementation Steps

### Step 1: Repository Structure

Create the app package. Per global CLAUDE.md rules: no logic in `__init__.py`, one file per concern, `app.py` has `main()`.

```
src/clawless/
├── __init__.py              # minimal
├── app.py                   # FastAPI app, lifespan, startup/shutdown, main()
├── agent.py                 # ClaudeSDKClient wrapper, per-sender session registry
├── channels/
│   ├── __init__.py
│   ├── base.py              # Abstract channel interface
│   └── whatsapp.py          # Twilio WhatsApp: webhook, media, formatter, send
├── formatter.py             # Markdown → WhatsApp formatting
└── config.py                # Pydantic BaseSettings from .env
```

Update `pyproject.toml`:
- Dependencies: `claude-agent-sdk`, `fastapi`, `uvicorn[standard]`, `httpx`, `twilio`
- Script entry point: `clawless = "clawless.app:main"`

### Step 2: Config Module

**File: `src/clawless/config.py`**

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Auth
    anthropic_api_key: str

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_from: str          # "whatsapp:+14155238886"
    twilio_webhook_path: str = "/twilio/whatsapp"
    twilio_public_url: str = ""        # for media serving (ngrok URL)
    twilio_validate_signature: bool = False

    # Access control
    allowed_senders: list[str] = []    # ["+1234567890"], empty = allow all

    # Agent limits
    max_turns: int = 30
    max_budget_usd: float = 1.0
    max_concurrent_requests: int = 3

    # Paths (set by Docker, defaults for local dev)
    workspace_dir: str = "."
```

### Step 3: Channel Interface

**File: `src/clawless/channels/base.py`**

```python
from dataclasses import dataclass, field

@dataclass
class InboundMessage:
    sender_id: str            # "+1234567890"
    chat_id: str              # "whatsapp:+1234567890"
    content: str              # text + media tags
    media_files: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"whatsapp:{self.sender_id}"
```

Channel interface as a Protocol — `start(app)`, `stop()`, `send_text(chat_id, text)`, `send_media(chat_id, file_path, caption)`.

### Step 4: WhatsApp Channel — Port from Nanobot

**File: `src/clawless/channels/whatsapp.py`**

Port from `nanobot/channels/twilio_whatsapp.py`. Key pieces:

**Keep (adapt imports):**
- `_handle_webhook()` — POST parsing, `From`, `Body`, `NumMedia`, `MediaUrl0`, signature validation
- `_download_media()` — httpx + Basic Auth from Twilio media URLs
- `send()` — `twilio.rest.Client.messages.create()`, split at 1600 chars
- `_stage_media()` / `_serve_media()` — outbound file staging + HTTP serving
- Signature validation with `public_url` for ngrok

**Replace:**
- nanobot `BaseChannel` / `MessageBus` → our channel Protocol + callback to agent
- aiohttp → FastAPI routes registered during `start(app)`
- nanobot config → our `Settings`
- `get_media_dir()` → `{workspace}/media/`

**Webhook handler pattern (from nanobot):**
1. Parse Twilio form data
2. Validate signature (if enabled)
3. Check sender against allowlist
4. Download media attachments (if any)
5. Build `InboundMessage` with text + media tags
6. Return TwiML `<Response></Response>` immediately (HTTP 200)
7. Enqueue message for async agent processing

### Step 5: Agent Module — Port from Nanobot

**File: `src/clawless/agent.py`**

Port the core pattern from `nanobot/agent/claude_agent.py`. Simplified since we don't need nanobot's bus, cron, streaming, or slash commands in MVP.

**Key structures to port:**

```python
@dataclass
class _SessionClient:
    client: ClaudeSDKClient
    session_id: str | None = None

class AgentManager:
    _clients: dict[str, _SessionClient]       # session_key → client
    _session_locks: dict[str, asyncio.Lock]    # per-sender serialization
    _concurrency_gate: asyncio.Semaphore       # global cap
    _session_map: dict[str, str]               # session_key → CLI session UUID
```

**Pattern from nanobot `_process_message()`:**
```python
sc = await self._get_or_create_client(session_key)
await sc.client.query(msg.content)
async for message in sc.client.receive_response():
    if isinstance(message, SystemMessage) and message.subtype == "init":
        new_id = message.data.get("session_id")
        if new_id and new_id != sc.session_id:
            sc.session_id = new_id
            self._record_session(key, new_id)
    elif isinstance(message, ResultMessage):
        if message.result:
            final_content = message.result
```

**Session persistence from nanobot:**
- `_load_session_map()` — reads `workspace/claude_sessions.json`
- `_save_session_map()` — writes on every new session
- `_record_session()` — maps session_key → CLI session UUID
- On `_get_or_create_client()`: if mapping exists, set `options.resume = cli_session_id`

**Options from nanobot:**
```python
ClaudeAgentOptions(
    system_prompt=system_prompt,
    model=model,
    max_turns=settings.max_turns,
    mcp_servers=mcp_servers,
    allowed_tools=[...],
    permission_mode="bypassPermissions",
    cwd=str(workspace),
)
```

### Step 6: Response Formatter

**File: `src/clawless/formatter.py`**

```python
def format_for_whatsapp(text: str) -> str:
    """Convert Claude's markdown to WhatsApp format."""
    # ## Header → *Header*
    # **bold** → *bold*
    # - bullet → • bullet
    # Strip HTML
    # Preserve code blocks
    ...

def split_message(text: str, max_len: int = 1600) -> list[str]:
    """Split at paragraph/sentence boundaries."""
    # Port from nanobot utils/helpers.py
    ...
```

### Step 7: FastAPI App

**File: `src/clawless/app.py`**

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    agent_mgr = AgentManager(settings)
    whatsapp = WhatsAppChannel(settings)

    app.state.agent = agent_mgr
    app.state.whatsapp = whatsapp

    whatsapp.register_routes(app)
    whatsapp.set_message_handler(agent_mgr.process_message)

    yield

    await agent_mgr.close_all()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ok"}

def main():
    import uvicorn
    uvicorn.run("clawless.app:app", host="0.0.0.0", port=8080)
```

**Async processing pattern:**

The WhatsApp channel webhook returns 200 immediately and enqueues. The agent processes async:

```python
# In whatsapp.py webhook handler:
async def handle_webhook(self, request: Request):
    message = self._parse_twilio_request(request)
    # Fire-and-forget background task
    asyncio.create_task(self._message_handler(message, self))
    return Response(content="<Response></Response>", media_type="application/xml")
```

### Step 8: Dockerfile & Docker Compose

**Dockerfile:**
```dockerfile
FROM python:3.13-slim
RUN useradd -m -s /bin/bash appuser
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
COPY --chown=appuser:appuser . /app
RUN pip install --no-cache-dir /app
USER appuser
WORKDIR /home/appuser/workspace
CMD ["clawless"]
```

**docker-compose.yml:**
```yaml
services:
  agent:
    build: .
    ports:
      - "8080:8080"
    env_file: .env
    volumes:
      - ${WORKSPACE_DIR}:/home/appuser/workspace:rw
      - ${USER_CLAUDE_DIR}:/home/appuser/.claude:rw
      - ./src:/app:ro
```

**.env.example:**
```
ANTHROPIC_API_KEY=sk-ant-...
WORKSPACE_DIR=./data/workspace
USER_CLAUDE_DIR=./data/user-claude
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_PUBLIC_URL=https://xxxx.ngrok-free.app
TWILIO_VALIDATE_SIGNATURE=false
ALLOWED_SENDERS=["+1234567890"]
MAX_TURNS=30
MAX_BUDGET_USD=1.0
```

### Step 9: Integration Test

1. Set up Twilio WhatsApp Sandbox (free tier)
2. `ngrok http 8080` to expose webhook port
3. Configure Twilio webhook: `https://<ngrok>.ngrok-free.app/twilio/whatsapp`
4. Create `data/user-claude/CLAUDE.md` with persona
5. Create `.env` from `.env.example`
6. `docker compose up --build`
7. Send WhatsApp message → verify Claude responds
8. `docker compose down && up` → verify session resumes (references prior conversation)

---

## File Dependency Graph

```
config.py             ← no internal deps
    ↓
channels/base.py      ← InboundMessage dataclass
    ↓
formatter.py          ← pure functions
    ↓
channels/whatsapp.py  ← uses config, base, formatter
    ↓
agent.py              ← uses config, base, formatter, ClaudeSDKClient
    ↓
app.py                ← wires everything: FastAPI + agent + whatsapp
```

Build order: config → base → formatter → whatsapp → agent → app

---

## What's Ported vs. New

| Component | Source | Notes |
|-----------|--------|-------|
| Twilio webhook parsing | Port from nanobot `twilio_whatsapp.py` | Form fields, sig validation, media download |
| Media staging/serving | Port from nanobot `twilio_whatsapp.py` | UUID filenames, FileResponse |
| Message splitting | Port from nanobot `utils/helpers.py` | Split at 1600 chars |
| Twilio send | Port from nanobot `twilio_whatsapp.py` | `messages.create()`, media_url |
| Agent session management | Port from nanobot `claude_agent.py` | Per-sender client, session map, locks, semaphore |
| ClaudeSDKClient usage | Port from nanobot `claude_agent.py` | query/receive_response pattern, session resume |
| Channel interface | **New** | Simpler than nanobot's BaseChannel |
| FastAPI integration | **New** | Replace aiohttp + MessageBus |
| Response formatter | **New** | Markdown → WhatsApp conversion |
| Docker setup | **New** | Dockerfile, compose, mount strategy |
| Config/settings | **New** | Pydantic BaseSettings (simpler than nanobot's YAML) |

---

## Out of Scope (Phase 2+)

- Plugin system — Phase 2
- Additional channels (Telegram, etc.) — Phase 2
- Media in prompts (multimodal input to SDK) — Phase 2
- Custom MCP tools (message tool, cron tool) — Phase 2
- Streaming responses — Phase 2
- Slash commands (/new, /stop, /status) — Phase 2
- Scheduled tasks / proactive messages — Phase 3
- Health checks and monitoring — Phase 4
- Cost tracking — Phase 4
