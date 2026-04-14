# Clawless — Code Walkthrough

File-by-file walkthrough of the implementation. Read `ARCHITECTURE.md` first for
the high-level design.

## `src/clawless/config.py` — Configuration

All paths and settings originate here.

**ClawlessPaths** derives every path from `Path.home()`:
- `workspace` — `~/workspace` (agent's cwd)
- `data_dir` — `~/data` (clawless.db)
- `plugin_dir` — `~/plugin` (Claude Code plugin)
- `media_dir` — `~/workspace/media` (runtime artifact, auto-created)

Constructor calls `_validate()` which checks that `workspace`, `data_dir`, and
`plugin_dir` exist as directories. Raises `RuntimeError` with a helpful
"run clawless-init" message if anything is missing.

**Settings** (pydantic-settings `BaseSettings`) loads config from three sources in
priority order:
1. Environment variables (with `__` as nesting delimiter, e.g. `CLAUDE__MAX_TURNS=10`)
2. `.env` file in CWD (gracefully ignored if missing)
3. TOML file at `~/clawless.toml` (gracefully ignored if missing)

`anthropic_api_key` is a required field — pydantic raises `ValidationError` if
missing from all sources. A `model_validator` also enforces that at least one
channel is configured.

## `src/clawless/store.py` — Message Store

SQLite-backed store with WAL mode, three tables:

- **`sessions`**: sender (PK) → session_id. Replaces the old SqliteDict.
- **`messages`**: Inbound message bus. PK is the channel-provided message ID
  (Twilio MessageSid, test UUID). `INSERT OR IGNORE` provides dedup.
- **`cursors`**: Per-sender watermark for crash recovery.

**Key methods**:
- `store_message(id, sender, content, inbound, ...)` → bool (True if inserted)
- `get_unprocessed(sender)` → messages after cursor, ordered by rowid
- `get_all_senders_with_unprocessed()` → senders needing processing
- `get/set_cursor(sender, msg_id)` → per-sender watermark
- `get/set_session(sender, session_id)` → agent session persistence

Uses `rowid` ordering (not timestamps) to avoid same-second collisions.

## `src/clawless/app.py` — Application Wiring

The FastAPI lifespan function is the central wiring point:

1. Constructs `ClawlessPaths` and `Settings`
2. Creates `media_dir` if missing
3. Creates `MessageStore` at `data_dir/clawless.db`
4. Detects plugin at `~/plugin/.claude-plugin/`
5. Creates `AgentManager` with config, plugin paths, workspace, data_dir, and store
6. Builds channel map (sender prefix → channel instance):
   - `"whatsapp:"` → `TwilioWhatsAppChannel`
   - `"test:"` → `TestChannel`
7. Starts the message loop via `asyncio.create_task(agent.start_message_loop(channels))`
8. Test channel starts immediately via `asyncio.create_task(tc.run())`

`main()` is the CLI entry point (`clawless` command). Reads `Settings` for the port
and runs uvicorn.

## `src/clawless/agent.py` — Agent Session Management

The core of the system. `AgentManager` manages one `ClaudeSDKClient` per sender.

**Concurrency model**:
- `_locks: dict[str, asyncio.Lock]` — per-sender serialization
- `_concurrency_gate: asyncio.Semaphore` — global cap on concurrent SDK calls
- Both are acquired in `process_message()` via `async with lock, self._concurrency_gate`

**Structured output**: `RESPONSE_SCHEMA` defines the JSON schema for agent responses:
`{"text": "...", "media": [...]}`. Set via `output_format` on `ClaudeAgentOptions`.
The SDK constrains the agent's final response to match this schema.

**Session persistence**:
- `_store.get_session()` / `_store.set_session()` maps sender → CLI session UUID
- On client creation, `_build_options()` checks for a persisted session to set
  `ClaudeAgentOptions.resume`
- New session IDs arrive via `SystemMessage` with `subtype == "init"` during
  response streaming, and are persisted immediately

**Message processing** (`process_message()`):
1. Acquires per-sender lock + semaphore
2. Advances cursor optimistically, saves old cursor for rollback
3. Gets or creates a `_SessionClient`
4. Builds prompt with channel formatting instructions prepended
5. Calls `client.query(prompt)` then iterates `client.receive_response()`
6. Captures session ID from `SystemMessage.init`
7. Reads `ResultMessage.structured_output` → `{"text": "...", "media": [...]}`
8. Falls back to `ResultMessage.result` as plain text if structured output failed
9. Host sends response via `channel.send()`
10. On no output: sends failure message ("Sorry, I wasn't able to generate a response.")
11. On timeout: closes client, sends timeout message
12. On error: sends error message, rolls back cursor if no output was sent

**Message loop** (`start_message_loop()`):
- Polls `store.get_all_senders_with_unprocessed()` every ~1 second
- For each sender with unprocessed messages, fetches them and routes to the
  correct channel via `_resolve_channel()` (prefix matching)
- Creates `asyncio.create_task()` for each message

## `src/clawless/channels/base.py` — Channel Interface

Defines the contract between channels and the rest of the system.

**Channel** is a `Protocol` (structural typing, no inheritance required):
- `name: str` — identifier for logging
- `formatting_instructions: str` — injected into every prompt
- `send(to, text, media)` — async method to reply

**InboundMessage** is a dataclass carrying:
- `sender` — channel-namespaced identity (e.g. `whatsapp:+1234567890`)
- `sender_name` — display name if available
- `content` — text body, may include `[mime/type: /path/to/file]` tags
- `message_id` — platform-provided (Twilio MessageSid) or channel-generated UUID
- `media_files` — list of local file paths
- `metadata` — platform-specific extras

## `src/clawless/channels/whatsapp.py` — Twilio WhatsApp

The most complex channel. Handles the full Twilio webhook lifecycle.

**Constructor** sets up:
- Twilio `Client` for outbound API calls
- `RequestValidator` for webhook signature verification
- Media directories (`workspace/media/inbound/` and `outbound/`)
- Two FastAPI routes: webhook POST and media GET

**Inbound flow** (`_handle_webhook()`):
1. Validates Twilio signature using `RequestValidator`
2. Checks sender against `allowed_senders` allowlist
3. Downloads any media attachments via `_download_media()`
4. Writes to store via `store.store_message(id=message_sid, ...)` — dedup via PK
5. Returns TwiML with `ack_message` ("Thinking...") immediately
6. Message loop picks up the message asynchronously

**Outbound flow** (`send()`):
- Splits text using `split_text()` at 1600 chars (Twilio limit)
- Sends each text chunk via `twilio.messages.create()`
- For media: stages local files via `_stage_media()` to get a public URL,
  then sends via separate `messages.create()` calls

## `src/clawless/channels/test.py` — Test Channel

Minimal channel for integration testing without external services.

**`run()`** writes scripted messages to the store with UUID message IDs
(`test_<uuid>`), then waits for the message loop to process them by polling
the response count.

**`send()`** just appends to `_responses` list — no external API calls.

**HTTP endpoints** for test assertions:
- `GET /test/responses` — returns all captured responses
- `GET /test/status` — returns done flag, message count, response count, error

## `src/clawless/wiki.py` — Wiki Endpoint

Exposes `~/workspace/wiki/` as rendered HTML pages via two routes.

**`make_wiki_router(workspace)`** takes the workspace `Path` and returns an
`APIRouter` with prefix `/wiki`. The wiki root is `workspace / "wiki"`.

**`GET /wiki`** — lists all `.md` files found via `rglob("*.md")`, sorted and
linked by relative path. Returns 404 if the wiki directory doesn't exist yet.

**`GET /wiki/{page_path}`** — resolves the path against the wiki root, accepting
requests with or without `.md` extension. Reads the file, calls
`markdown.Markdown.convert()` (reset between requests), and wraps the result in
a minimal HTML page with breadcrumb navigation. Blocks traversal outside the
wiki root with a `resolve()` / `relative_to()` check (403 on violation).

The `_MD` instance is module-level and re-used; `.reset()` is called before each
render to clear state left from `toc` extension processing.

## `src/clawless/tools.py` — MCP Tool Harness

Defines an in-process MCP server registered with the agent. Currently empty —
tools can be added here for non-contextual side effects. `build_clawless_mcp_server()`
returns a server with an empty tool list.

## `src/clawless/utils.py` — Text Splitting

Single function: `split_text(text, max_len) -> list[str]`

Splitting strategy (in priority order):
1. Prefer newline breaks (preserves paragraph structure)
2. Fall back to space breaks (preserves words)
3. Hard-cut at `max_len` if no break point found

Used by WhatsApp channel for Twilio's 1600-character limit.

## `src/clawless/init.py` — Home Directory Scaffolding

`clawless-init [path]` creates the prescribed directory structure.

**Templates** (written only if file doesn't exist):
- `PROJECT_CLAUDE_MD_TEMPLATE` — agent identity and workspace context
- `CONFIG_TEMPLATE` — skeleton clawless.toml

**`init_home(path)`** creates:
- `.claude/`, `workspace/`, `data/` directories
- Plugin skeleton: `.claude-plugin/plugin.json`, `skills/`, `agents/`, `commands/`, `hooks/`
- `workspace/.claude/` for project-level SDK settings (CLAUDE.md)
- `workspace/plugin/` writable plugin scaffold (bot-created skills/agents)
- CLAUDE.md template at project level
- Config template at top level

## Tests

### `tests/test_store.py` — Store Unit Tests

Tests MessageStore operations: session roundtrip, message insertion/dedup, cursor
get/set/rollback, unprocessed message queries (by rowid ordering), WAL mode.

### `tests/test_config.py` — Config Unit Tests

Tests `ClawlessPaths` validation and `Settings` loading (TOML, env vars, defaults).

### `tests/test_base.py` — InboundMessage Tests

Tests InboundMessage dataclass construction and sender namespacing.

### `tests/test_utils.py` — Text Splitting Tests

Tests `split_text()` edge cases.

### `tests/test_channel_integration.py` — Host Integration Test

Full pipeline test running in-process. Session-scoped async fixture creates isolated
home, starts app via ASGI transport. Requires `ANTHROPIC_API_KEY` env var. Polls test
channel endpoints, asserts on agent responses, verifies `clawless.db` exists.

### `tests/test_docker_integration.py` — Docker Integration Test

Marked `@pytest.mark.docker`, skipped by default. Builds Docker image, starts
container, polls health and test channel endpoints over real HTTP.
