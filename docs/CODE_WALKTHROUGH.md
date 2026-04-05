# Clawless — Code Walkthrough

File-by-file walkthrough of the implementation. Read `ARCHITECTURE.md` first for
the high-level design.

## `src/clawless/config.py` — Configuration

All paths and settings originate here.

**ClawlessPaths** derives every path from `Path.home()`:
- `workspace` — `~/workspace` (agent's cwd)
- `data_dir` — `~/data` (sessions.db)
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

This is configured in `settings_customise_sources()` which returns
`(env_settings, dotenv_settings, toml_source)`.

`anthropic_api_key` is a required field — pydantic raises `ValidationError` if
missing from all sources. A `model_validator` also enforces that at least one
channel is configured.

## `src/clawless/app.py` — Application Wiring

The FastAPI lifespan function is the central wiring point:

1. Constructs `ClawlessPaths` and `Settings`
2. Creates `media_dir` if missing
3. Detects plugin at `~/plugin/.claude-plugin/`
4. Creates `AgentManager` with claude config, plugin paths, workspace, and data_dir
5. Conditionally creates `TwilioWhatsAppChannel` and/or `TestChannel` based on config
6. Channel validation is handled by `Settings` model_validator at construction time

The test channel is special — it starts immediately via `asyncio.create_task(test.run())`
since it feeds scripted messages rather than waiting for webhooks.

`main()` is the CLI entry point (`clawless` command). Reads `Settings` for the port
and runs uvicorn. The app module path `clawless.app:app` is passed as a string so
uvicorn imports it fresh.

## `src/clawless/agent.py` — Agent Session Management

The core of the system. `AgentManager` manages one `ClaudeSDKClient` per sender.

**Concurrency model**:
- `_locks: dict[str, asyncio.Lock]` — per-sender serialization
- `_concurrency_gate: asyncio.Semaphore` — global cap on concurrent SDK calls
- Both are acquired in `process_message()` via `async with lock, self._concurrency_gate`

**Session persistence**:
- `_session_map: SqliteDict` at `data_dir/sessions.db`, autocommit enabled
- Maps sender string -> CLI session UUID
- On client creation, `_build_options()` sets `system_prompt` using a `claude_code`
  preset with framework-specific instructions appended, and checks for a persisted
  session to set `ClaudeAgentOptions.resume`
- New session IDs arrive via `SystemMessage` with `subtype == "init"` during
  response streaming, and are persisted immediately

**Message processing** (`process_message()`):
1. Acquires per-sender lock + semaphore
2. Gets or creates a `_SessionClient` (wrapper around `ClaudeSDKClient`)
3. Builds prompt with channel formatting instructions prepended
4. Calls `client.query(prompt)` then iterates `client.receive_response()`
5. Captures session ID from `SystemMessage.init` and final text from `ResultMessage`
6. Sends response via `channel.send()`
7. On timeout: closes the client, sends error message
8. On exception: logs, sends generic error message

**Client lifecycle**: Clients are created lazily via `_get_or_create_client()` and
use the async context manager protocol (`__aenter__`/`__aexit__`). `close_all()` is
called during app shutdown to clean up all clients and close the session store.

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
- `media_files` — list of local file paths
- `metadata` — platform-specific extras

The sender string is globally unique across channels and doubles as the session key
in AgentManager — no separate channel prefix needed.

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
4. Builds `InboundMessage` with media tagged as `[mime/type: path]` in content
5. Fires `asyncio.create_task(agent.process_message(...))` — fire-and-forget
6. Returns TwiML with `ack_message` ("Thinking...") immediately

**Media download** (`_download_media()`):
- Fetches from Twilio media URLs using Basic Auth (account_sid:auth_token)
- Determines MIME type from Content-Type header
- Saves to `inbound/` with UUID filename + appropriate extension
- Handles errors gracefully (logs and skips failed downloads)

**Outbound flow** (`send()`):
- Splits text using `split_text()` at 1600 chars (Twilio limit)
- Sends each text chunk via `twilio.messages.create()`
- For media: stages local files via `_stage_media()` to get a public URL,
  then sends via separate `messages.create()` calls (Twilio ignores body for media)

**Media serving** (`_serve_media()`):
- Serves staged outbound media at `GET {webhook_path}/media/{filename}`
- Path traversal protection via `resolve()` + `is_relative_to()` check

## `src/clawless/channels/test.py` — Test Channel

Minimal channel for integration testing without external services.

**`run()`** iterates through `config.messages` sequentially, calling
`agent.process_message()` for each. Sets `_done` event when complete, captures
any exception in `_error`.

**`send()`** just appends to `_responses` list — no external API calls.

**HTTP endpoints** for test assertions:
- `GET /test/responses` — returns all captured responses
- `GET /test/status` — returns done flag, message count, response count, error

## `src/clawless/utils.py` — Text Splitting

Single function: `split_text(text, max_len) -> list[str]`

Splitting strategy (in priority order):
1. Prefer newline breaks (preserves paragraph structure)
2. Fall back to space breaks (preserves words)
3. Hard-cut at `max_len` if no break point found (for long unbreakable strings)

Each chunk is `lstrip()`ed to remove leading whitespace from continuation chunks.
Used by WhatsApp channel for Twilio's 1600-character limit.

## `src/clawless/init.py` — Home Directory Scaffolding

`clawless-init [path]` creates the prescribed directory structure.

**Templates** (written only if file doesn't exist):
- `PROJECT_CLAUDE_MD_TEMPLATE` — agent identity, communication style, and workspace context (`~/workspace/.claude/CLAUDE.md`)
- `CONFIG_TEMPLATE` — skeleton clawless.toml with all channel options commented out

**`init_home(path)`** creates:
- `workspace/`, `data/` directories
- Plugin skeleton: `.claude-plugin/plugin.json`, `skills/`, `agents/`, `commands/`, `hooks/`
- `workspace/.claude/` for project-level SDK settings and runtime state
- CLAUDE.md template at project level
- Config template in `data/`

Used by both the CLI command and test fixtures (which call `init_home()` directly).

## Tests

### `tests/test_config.py` — Unit Tests

Tests `ClawlessPaths` validation (finds correct dirs, raises on missing dirs) and
`Settings` loading (TOML parsing, env var overrides, defaults, empty channels).
Each test creates an isolated home via `init_home()` and sets `HOME`.

### `tests/test_base.py` — InboundMessage Tests

Tests InboundMessage dataclass construction (minimal fields, full fields, sender
namespacing across different channels).

### `tests/test_utils.py` — Text Splitting Tests

Tests `split_text()` edge cases: empty string, short string, exact length, newline
splits, space splits, hard cuts, multiple chunks.

### `tests/test_channel_integration.py` — Host Integration Test

Full pipeline test running in-process. Session-scoped async fixture creates isolated
home, starts app via ASGI transport. Requires `ANTHROPIC_API_KEY` env var. Polls test
channel endpoints, asserts on agent responses, checks for auth failures.

### `tests/test_docker_integration.py` — Docker Integration Test

Marked `@pytest.mark.docker`, skipped by default. Session-scoped fixture builds
Docker image, starts container via `docker compose up`, requires `ANTHROPIC_API_KEY`
(skips if not set), polls health and test
channel endpoints over real HTTP. Streams docker compose output to terminal.
Port 18266 (vs 18265 prod default).
