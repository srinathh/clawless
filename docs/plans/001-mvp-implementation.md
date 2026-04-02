# Clawless MVP Implementation Plan

## Context

Clawless is a self-hosted personal AI assistant built on the Claude Agent SDK. A FastAPI app receives WhatsApp messages via Twilio webhooks, routes them to `ClaudeSDKClient` for processing, and sends replies back. See [SPEC.md](../SPEC.md) for the full architecture.

**MVP goal:** Send a WhatsApp message, get a Claude response back, running in Docker with persistent sessions.

**Key reference:** The Twilio WhatsApp bridge in [srinathh/nanobot](https://github.com/srinathh/nanobot/tree/feature/twilio-whatsapp-nightly) (branch `feature/twilio-whatsapp-nightly`, file `nanobot/channels/twilio_whatsapp.py`, 334 lines) already handles webhook parsing, signature validation, media download, media staging/serving, and message splitting. Much of this can be ported directly.

---

## Phase 0: Research Spikes

These must be completed first — results may change the implementation.

### Spike 1: Agent SDK in Docker

**Goal:** Verify `ClaudeSDKClient` works inside Docker with `ANTHROPIC_API_KEY`.

**What to build:**
- Minimal Dockerfile: Python 3.13 + Node.js + Claude Code CLI + `claude-agent-sdk`
- A 10-line Python script that calls `query(prompt="Say hello", options=ClaudeAgentOptions(max_turns=1))`
- docker-compose.yml mounting a workspace and passing `ANTHROPIC_API_KEY`

**What to verify:**
- SDK installs cleanly in Docker
- Claude Code CLI installs via npm (SDK depends on it)
- `query()` returns a successful `ResultMessage`
- Session `.jsonl` files appear in `~/.claude/projects/`
- Non-root user (`appuser`) works with `bypassPermissions`

**What could go wrong:**
- Claude Code CLI may need additional system dependencies
- The SDK may try to write to paths we haven't mounted
- `bypassPermissions` may behave differently in Docker

### Spike 2: ClaudeSDKClient Lifecycle & Concurrency

**Goal:** Understand how to manage `ClaudeSDKClient` instances for multiple WhatsApp senders.

**What to test (can run locally, doesn't need Docker):**
- Create a `ClaudeSDKClient`, call `query()` twice — does it maintain session?
- Create two `ClaudeSDKClient` instances with different `cwd` or options — do they get separate sessions?
- Call `query()` on one client while another is still processing — does it work or block?
- Capture `session_id` from `ResultMessage`, destroy the client, create a new one with `resume=session_id` — does it restore context?

**What we learn:**
- One client per sender, or one shared client?
- Can we use `resume` to reconnect after container restart?
- What happens if a sender messages while their previous query is still running?

### Spike 3: Multimodal Input

**Goal:** Confirm how to pass images to `ClaudeSDKClient.query()`.

**What to test:**
- Read the SDK source or docs for the `prompt` parameter type
- Try passing a base64-encoded image as a content block
- Verify Claude can see and describe the image

**What we learn:**
- Exact format for image + text prompts
- Whether we need to base64-encode or can pass file paths

---

## Phase 1: Implementation Steps

### Step 1: Repository Structure

Create the app package structure. Per the global CLAUDE.md rules: no logic in `__init__.py`, one file per functional concern, `app.py` has `main()`.

```
src/clawless/
├── __init__.py              # minimal, re-export main if needed
├── app.py                   # FastAPI app creation, lifespan, startup/shutdown
├── agent.py                 # ClaudeSDKClient wrapper, session registry
├── channels/
│   ├── __init__.py
│   ├── base.py              # Abstract channel interface (Protocol or ABC)
│   └── whatsapp.py          # Twilio WhatsApp: webhook handler, media, formatter
├── formatter.py             # Markdown → WhatsApp formatting
└── config.py                # Environment variable loading, pydantic settings
```

Update `pyproject.toml`:
- Add dependencies: `claude-agent-sdk`, `fastapi`, `uvicorn`, `httpx`, `twilio`
- Script entry point: `clawless = "clawless.app:main"`

**No Dockerfile yet** — get the app working locally first.

### Step 2: Config Module

**File: `src/clawless/config.py`**

Pydantic `BaseSettings` that reads from environment:

```python
class Settings(BaseSettings):
    anthropic_api_key: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_from: str  # e.g. "whatsapp:+14155238886"
    twilio_webhook_path: str = "/twilio/whatsapp"
    twilio_public_url: str = ""  # for media serving, e.g. ngrok URL
    twilio_validate_signature: bool = False
    allowed_senders: list[str] = []  # phone numbers, e.g. ["+1234567890"]
    workspace_dir: str = "."
    max_turns: int = 30
    max_budget_usd: float = 1.0
```

Reads from `.env` file or environment variables.

### Step 3: Channel Interface

**File: `src/clawless/channels/base.py`**

Define the abstract interface every channel must implement:

```python
from typing import Protocol

class InboundMessage:
    """Parsed inbound message from a channel."""
    sender_id: str       # unique sender identifier (e.g. "+1234567890")
    chat_id: str         # channel-specific chat ID (e.g. "whatsapp:+1234567890")
    content: str         # text content (may include media tags)
    media_files: list[str]  # local paths to downloaded media files
    metadata: dict       # channel-specific metadata

class Channel(Protocol):
    """Interface that every channel must implement."""
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: str, text: str) -> None: ...
    async def send_media(self, chat_id: str, file_path: str, caption: str = "") -> None: ...
```

The channel registers its webhook routes with FastAPI during `start()`.

### Step 4: WhatsApp Channel — Port from Nanobot

**File: `src/clawless/channels/whatsapp.py`**

Port from `nanobot/channels/twilio_whatsapp.py` (334 lines). The nanobot code handles:

**Keep as-is (adapt imports):**
- `_handle_webhook()` — Twilio POST parsing, form fields (`From`, `Body`, `NumMedia`, `MediaUrl0`...), signature validation via `RequestValidator`
- `_download_media()` — authenticated httpx download from Twilio media URLs, content-type-based file extension guessing
- `send()` — Twilio REST API `messages.create()`, message splitting at 1600 chars
- `_stage_media()` / `_serve_media()` — copy outbound files to a staging dir, serve via HTTP for Twilio to fetch
- Signature validation logic with `public_url` support for proxies/ngrok

**Replace:**
- `BaseChannel` / `MessageBus` → our `Channel` protocol + FastAPI integration
- `_handle_message()` callback → enqueue to `asyncio.Queue` for agent processing
- aiohttp web server → FastAPI routes (webhook handler becomes a FastAPI endpoint)
- nanobot config → our `Settings` from config.py
- `get_media_dir()` → workspace-relative paths

**Key constants from nanobot:**
- `TWILIO_MAX_MESSAGE_LEN = 1600`
- Webhook path: `/twilio/whatsapp`
- Media download uses `httpx.AsyncClient` with Basic Auth (`account_sid:auth_token`)

### Step 5: Agent Module — Session Registry

**File: `src/clawless/agent.py`**

Manages `ClaudeSDKClient` instances, one per sender. This is the bridge between channels and the SDK.

```python
class AgentManager:
    """Manages ClaudeSDKClient instances per sender."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}  # per-sender lock
        self._session_map: dict[str, str] = {}  # sender_id → session_id
        self._queue: asyncio.Queue = asyncio.Queue()

    async def process_message(self, message: InboundMessage, channel: Channel) -> None:
        """Process an inbound message and send the reply via the channel."""
        sender_id = message.sender_id
        
        # Per-sender lock to serialize messages from same sender
        lock = self._locks.setdefault(sender_id, asyncio.Lock())
        async with lock:
            client = await self._get_or_create_client(sender_id)
            
            # Build prompt (text + media references)
            prompt = self._build_prompt(message)
            
            # Query the SDK
            async for msg in client.query(prompt):
                if isinstance(msg, ResultMessage):
                    if msg.subtype == "success":
                        formatted = format_for_whatsapp(msg.result)
                        await channel.send_text(message.chat_id, formatted)
                    else:
                        await channel.send_text(message.chat_id, 
                            f"Sorry, I couldn't process that ({msg.subtype})")
                    # Save session_id for resume
                    self._session_map[sender_id] = msg.session_id

    async def _get_or_create_client(self, sender_id: str) -> ClaudeSDKClient:
        """Get existing client or create a new one. Resume session if available."""
        if sender_id not in self._clients:
            options = ClaudeAgentOptions(
                allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep",
                               "WebSearch", "WebFetch"],
                permission_mode="bypassPermissions",
                max_turns=self._settings.max_turns,
                max_budget_usd=self._settings.max_budget_usd,
                setting_sources=["project"],
            )
            # Resume previous session if we have one
            if sender_id in self._session_map:
                options.resume = self._session_map[sender_id]
            
            client = ClaudeSDKClient(options=options)
            self._clients[sender_id] = client
        return self._clients[sender_id]
```

**Open questions (from spikes):**
- Does `ClaudeSDKClient` need to be used as `async with` context manager per query, or can it stay open?
- Is `resume` set on `ClaudeAgentOptions` or passed differently?
- What happens to the client if the query errors out — is it reusable?

### Step 6: FastAPI App

**File: `src/clawless/app.py`**

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create agent manager, start channels
    settings = Settings()
    agent_manager = AgentManager(settings)
    whatsapp = WhatsAppChannel(settings, agent_manager)
    
    app.state.agent_manager = agent_manager
    app.state.whatsapp = whatsapp
    
    await whatsapp.start(app)  # registers routes
    yield
    await whatsapp.stop()

app = FastAPI(lifespan=lifespan)

def main():
    import uvicorn
    uvicorn.run("clawless.app:app", host="0.0.0.0", port=8080)
```

The WhatsApp channel registers its webhook route during `start()`:

```python
# Inside WhatsAppChannel.start():
app.post(self._settings.twilio_webhook_path)(self.handle_webhook)
app.get("/twilio/whatsapp/media/{filename}")(self.serve_media)
app.get("/health")(self.health)
```

### Step 7: Response Formatter

**File: `src/clawless/formatter.py`**

Convert Claude's markdown to WhatsApp format:

```python
def format_for_whatsapp(text: str, max_len: int = 1600) -> list[str]:
    """Convert markdown to WhatsApp format and split into chunks."""
    # 1. Convert ## headers → *bold* lines
    # 2. Convert **bold** → *bold*
    # 3. Convert - bullets → • bullets
    # 4. Strip HTML
    # 5. Preserve code blocks
    # 6. Split at max_len on paragraph/sentence boundaries
    ...
```

Port `split_message()` from nanobot's `utils/helpers.py` and add formatting conversion.

### Step 8: Dockerfile & Docker Compose

**Dockerfile** (per spec):
```dockerfile
FROM python:3.13-slim
RUN useradd -m -s /bin/bash appuser
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
USER appuser
WORKDIR /home/appuser/workspace
COPY --chown=appuser:appuser . /app
RUN pip install --no-cache-dir /app
CMD ["clawless"]
```

**docker-compose.yml** (per spec — 4 mounts: workspace rw, user-claude-dir rw, plugins ro, app ro + optional credentials ro overlay).

**Entrypoint:** The `CMD` runs the `clawless` script entry point which calls `uvicorn`.

### Step 9: Integration Test

1. Set up Twilio WhatsApp Sandbox (free tier)
2. Run `ngrok http 8080` to expose the webhook port
3. Configure Twilio webhook URL: `https://<ngrok>.ngrok-free.app/twilio/whatsapp`
4. Create `.env` with `ANTHROPIC_API_KEY`, Twilio creds, `TWILIO_PUBLIC_URL`
5. Create `data/user-claude/CLAUDE.md` with basic persona
6. Run `docker compose up --build`
7. Send a WhatsApp message to the Twilio sandbox number
8. Verify Claude responds on WhatsApp
9. `docker compose down && docker compose up` — send another message, verify session resumes

**Success criteria:**
- Text message: received → processed → reply on WhatsApp
- Session persistence: reply references prior conversation after restart
- Error handling: invalid sender gets no response (allowlist)
- Timeout: Twilio gets HTTP 200 within 15s, reply comes async via REST API

---

## File Dependency Graph

```
config.py          ← reads .env, no internal deps
    ↓
channels/base.py   ← defines Channel protocol, InboundMessage
    ↓
formatter.py       ← pure function, no deps beyond stdlib
    ↓
channels/whatsapp.py ← implements Channel, uses config, formatter
    ↓
agent.py           ← uses ClaudeSDKClient, config, formatter, Channel
    ↓
app.py             ← wires everything together, FastAPI lifespan
```

Build order: config → base → formatter → whatsapp → agent → app

---

## What's Ported from Nanobot vs. New

| Component | Source | Notes |
|-----------|--------|-------|
| Twilio webhook parsing | Port from nanobot | Form fields, signature validation |
| Media download | Port from nanobot | httpx + Basic Auth, content-type guessing |
| Media staging/serving | Port from nanobot | UUID filenames, FileResponse |
| Message splitting | Port from nanobot | `split_message()` at 1600 chars |
| Twilio send | Port from nanobot | `messages.create()`, media_url support |
| Channel interface | **New** | Protocol class for pluggable channels |
| Agent session management | **New** | Per-sender ClaudeSDKClient, session resume |
| FastAPI integration | **New** | Webhook routes, lifespan, async processing |
| Response formatter | **New** | Markdown → WhatsApp conversion |
| Docker setup | **New** | Dockerfile, compose, mount strategy |
| Config/settings | **New** | Pydantic BaseSettings |

---

## Out of Scope (Phase 2+)

- Media handling (images, voice, documents) — Phase 2
- Plugin system — Phase 2
- Additional channels (Telegram, etc.) — Phase 2
- Scheduled tasks / proactive messages — Phase 3
- Permission relay — deferred (was channels-specific)
- Health checks and monitoring — Phase 4
- Cost tracking — Phase 4
