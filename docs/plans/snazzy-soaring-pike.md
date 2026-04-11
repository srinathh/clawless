# Plan: Host-Controlled Message Delivery with Structured Output

## Context

Clawless currently delegates message delivery to the agent via a `send_message` MCP tool. This caused the dot-spam bug (commit 3456459): the agent couldn't distinguish tool results from new user messages and entered an infinite `send_message(".")` loop. Current fixes (trivial rejection, rate limiting, prompt warnings) are band-aids.

Nanoclaw avoids this entirely: the host reads `result.result` from the agent output and calls `channel.sendMessage()` directly. The agent has no message-sending tool.

The Claude Agent SDK's **structured output** feature solves outbound media cleanly: the agent uses tools normally during its loop, then the final response is constrained to a JSON schema with `text` + `media` fields. The host reads `ResultMessage.structured_output` and calls `channel.send()`. Zero custom MCP tools needed.

**References:**
- [Structured outputs docs](https://code.claude.com/docs/en/agent-sdk/structured-outputs)
- Plan in `docs/plans/polished-scribbling-hammock.md` (branch `origin/feature/message-architecture-plan`)
- Nanoclaw at `~/src/nanoclaw` (host-controlled delivery, cursor-based message bus)

## Key Design Decisions

### Message bus architecture (inbound)

Nanoclaw uses a half-bus: channels write inbound messages to SQLite, a message loop polls with a cursor, and outbound is direct. This decouples channel receive from processing and supports both webhook and polling channels.

Clawless adopts the same pattern:

```
INBOUND:  Channel webhook → store.store_inbound() → return ack immediately
PROCESS:  Message loop polls store for unprocessed messages (cursor-based)
          → routes to correct channel by sender prefix
          → runs agent → gets structured output
OUTBOUND: Host calls channel.send() directly (no outbound storage needed)
```

The webhook handler no longer calls `asyncio.create_task(agent.process_message())`. It just writes to the store and returns. A separate loop (like nanoclaw's `startMessageLoop`) picks up new messages by advancing the cursor.

**Why this over direct invocation:**
- Supports future non-webhook channels (polling-based like Baileys)
- Crash recovery: unprocessed messages survive restarts (cursor hasn't advanced)
- Decouples receive from processing
- Matches the proven nanoclaw architecture

**Channel routing:** Sender IDs are channel-namespaced (`whatsapp:+1234...`, `test:user1`). The message loop looks up the channel instance by sender prefix to route outbound replies.

### Structured output replaces send_message entirely

The SDK's `output_format` option constrains the agent's final response to a JSON schema. The agent still uses all Claude Code tools during its loop — structured output only affects the final response.

Response schema:
```python
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Message text to send to the user"},
        "media": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Local file paths to attach as media"
        }
    },
    "required": ["text"]
}
```

### Keep MCP tools harness (empty for now)

Keep `tools.py` as an empty tool harness with `build_clawless_mcp_server()` returning an empty tool list. Future non-contextual side-effect tools can be added here. Remove `send_message`, `_ctx`, `set_context()`, `was_sent_in_turn()`, rate limiting.

### No outbound message storage

The agent SDK session maintains conversation history internally — no need to replay messages from our store. Outbound storage is only useful for observability, which we can add later. The store tracks: inbound messages (bus + dedup), sessions (agent context), cursors (crash recovery).

### Single `id` field (no separate platform_id)

Use one `id` field per message: use the platform-provided ID when available (Twilio MessageSid for inbound), generate a UUID otherwise. Channels provide the ID. Dedup is a simple PK check. Future platforms provide their own IDs in the same field.

### Agent sessions preserved

The `sessions` table (sender → session_id) stays. The agent resumes sessions across messages so it has full conversation context. Without sessions, every message would be a fresh conversation.

### Failure message to user

When the agent produces no output (empty result, timeout, error), send a failure message to the user rather than leaving them with just the "Thinking..." ack and silence.

## Changes

### 1. New file: `src/clawless/store.py`

SQLite message store. Uses stdlib `sqlite3` with WAL mode.

**Schema:**
```sql
CREATE TABLE sessions (
    sender      TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE messages (
    id          TEXT PRIMARY KEY,       -- channel-provided (Twilio MessageSid) or UUID
    sender      TEXT NOT NULL,
    inbound     INTEGER NOT NULL,       -- 1 = from user, 0 = from bot
    content     TEXT NOT NULL DEFAULT '',
    media_files TEXT,                   -- JSON array of file paths, or NULL
    sender_name TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_messages_sender ON messages(sender, created_at);

CREATE TABLE cursors (
    sender      TEXT PRIMARY KEY,
    last_msg_id TEXT NOT NULL,          -- last processed message id
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Design notes:**
- `id` is provided by the channel (Twilio MessageSid, `test_<uuid>`, etc.). Dedup is just a PK conflict check.
- `inbound` is a simple boolean integer: 1 = user message, 0 = from bot.
- No outbound storage — sessions handle agent context. Can add later for observability.

**Class `MessageStore`:**
- `__init__(self, db_path: Path)` — open connection, WAL mode, create tables
- `get_session(sender) -> str | None` / `set_session(sender, session_id)`
- `store_message(id, sender, content, inbound, sender_name="", media_files=None) -> bool` — `INSERT OR IGNORE`, returns True if inserted (False = duplicate)
- `get_unprocessed(sender) -> list[dict]` — messages after cursor for a given sender
- `get_all_senders_with_unprocessed() -> list[str]` — senders with messages past their cursor
- `get_cursor(sender) -> str | None` / `set_cursor(sender, msg_id)`
- `close()`

### 2. Modify `src/clawless/channels/base.py`

Add `message_id` field to `InboundMessage` (between `content` and `media_files`):
```python
message_id: str = ""  # message ID — platform-provided (Twilio MessageSid) or channel-generated UUID
```

### 3. Modify `src/clawless/channels/whatsapp.py`

**3a.** Pass `message_id` in InboundMessage constructor (line 117-123):
```python
message_id=message_sid,
```

**3b.** Replace `asyncio.create_task(agent.process_message())` with store write:
```python
store = request.app.state.store
stored = store.store_message(
    id=message.message_id, sender=sender, content=content,
    inbound=True, sender_name=profile_name, media_files=media_files or None,
)
if not stored:
    logger.info("Duplicate message %s from %s — dropping", message.message_id, sender)
# Return TwiML ack either way
```

**3c.** Capture outbound MessageSid from Twilio `messages.create()` response for logging (currently discarded at line ~145).

### 4. Modify `src/clawless/channels/test.py`

Generate a randomized message ID for each scripted message: `f"test_{uuid.uuid4().hex}"`.

Instead of calling `agent.process_message()` directly, write to the store. The message loop will pick them up.

### 5. Gut `src/clawless/tools.py` — keep as empty harness

Remove `send_message` tool, `_ctx` dict, `set_context()`, `was_sent_in_turn()`, rate limiting. Keep `build_clawless_mcp_server()` returning an empty tool list for future use.

### 6. Modify `src/clawless/agent.py` — Core change

**6a. Constructor** (line 84-92): Accept `store: MessageStore`, remove SqliteDict:
- Replace `self._session_map = SqliteDict(...)` with `self._store = store`
- Remove `from sqlitedict import SqliteDict`
- Remove `set_context` / `was_sent_in_turn` imports

**6b. Define response schema** as module constant:
```python
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Message text to send to the user"},
        "media": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Local file paths to attach as media"
        }
    },
    "required": ["text"]
}
```

**6c. `_build_options()`**: Add `output_format={"type": "json_schema", "schema": RESPONSE_SCHEMA}`. Keep MCP server registration (empty harness).

**6d. Session methods:** Replace `self._session_map` with `self._store` equivalents.

**6e. `FRAMEWORK_SYSTEM_PROMPT`** — simplify:
```
Your response will be delivered to the user automatically as a structured JSON object.
Reply naturally in the "text" field. To attach files, include their local paths in the
"media" array.

Your working directory is ~/workspace/. You have all Claude Code tools available
with bypass permissions.
```

Keep inbound media handling, skills/plugins, workspace sections. Remove all loop-prevention instructions and send_message references.

**6f. `process_message()`** — rewrite for structured output. No longer called from webhook directly. Called by the message loop with the inbound message + channel:

```python
async def process_message(self, message: InboundMessage, channel: Channel) -> None:
    sender = message.sender
    lock = self._locks.setdefault(sender, asyncio.Lock())

    async with lock, self._concurrency_gate:
        # Cursor: advance optimistically, save old for rollback
        previous_cursor = self._store.get_cursor(sender)
        self._store.set_cursor(sender, message.message_id)

        output_sent = False
        try:
            sc = await self._get_or_create_client(sender)
            # ... run query ...
            # On ResultMessage: structured = msg.structured_output, final_content = msg.result

            # Host-controlled delivery
            text, media = "", None
            if structured and isinstance(structured, dict):
                text = structured.get("text", "")
                media = structured.get("media") or None
            elif final_content:
                text = final_content  # fallback if structured output failed

            if text and text.strip():
                await channel.send(sender, text=text, media=media)
                output_sent = True
            elif media:
                await channel.send(sender, media=media)
                output_sent = True
            else:
                logger.warning("No response produced for %s", sender)
                await channel.send(sender, text="Sorry, I wasn't able to generate a response.")
                output_sent = True

        except asyncio.TimeoutError:
            logger.error("SDK call timed out for %s", sender)
            await self._close_client(sender)
            await channel.send(sender, text="Sorry, the request timed out. Please try again.")
            output_sent = True

        except Exception:
            logger.exception("Error processing message for %s", sender)
            try:
                await channel.send(sender, text="Sorry, I encountered an error processing your message.")
                output_sent = True
            except Exception:
                logger.exception("Failed to send error message to %s", sender)

            if not output_sent and previous_cursor is not None:
                self._store.set_cursor(sender, previous_cursor)
```

**6g. New: message loop** — polls store for unprocessed messages. Runs as an async task started in lifespan:

```python
async def start_message_loop(self, channels: dict[str, Channel], poll_interval: float = 1.0):
    """Poll store for unprocessed messages and route to agent."""
    while True:
        try:
            senders = self._store.get_all_senders_with_unprocessed()
            for sender in senders:
                messages = self._store.get_unprocessed(sender)
                # Route to channel by sender prefix
                channel = self._resolve_channel(sender, channels)
                if not channel:
                    logger.warning("No channel for sender %s", sender)
                    continue
                for msg in messages:
                    inbound = InboundMessage(
                        sender=msg["sender"], content=msg["content"],
                        message_id=msg["id"], sender_name=msg["sender_name"],
                        media_files=json.loads(msg["media_files"]) if msg["media_files"] else [],
                    )
                    asyncio.create_task(self.process_message(inbound, channel))
        except Exception:
            logger.exception("Error in message loop")
        await asyncio.sleep(poll_interval)

def _resolve_channel(self, sender: str, channels: dict[str, Channel]) -> Channel | None:
    """Route sender to channel by prefix match."""
    for prefix, channel in channels.items():
        if sender.startswith(prefix):
            return channel
    return None
```

**6h. `close_all()`**: Replace `self._session_map.close()` with `self._store.close()`.

### 7. Modify `src/clawless/app.py`

```python
# In lifespan:
store = MessageStore(paths.data_dir / "clawless.db")
app.state.store = store

agent = AgentManager(settings.claude, plugins, paths.workspace, paths.data_dir, store)
app.state.agent = agent

# Build channel map for routing (prefix → channel instance)
channels: dict[str, Channel] = {}
if settings.channels.twilio_whatsapp:
    wa = TwilioWhatsAppChannel(settings.channels.twilio_whatsapp, paths.media_dir, app)
    channels["whatsapp:"] = wa
    app.state.twilio_whatsapp = wa
if settings.channels.test:
    tc = TestChannel(settings.channels.test, app)
    channels["test:"] = tc
    app.state.test = tc

# Start message loop
asyncio.create_task(agent.start_message_loop(channels))

# On shutdown:
store.close()
await agent.close_all()
```

### 8. Modify `pyproject.toml`

Remove `"sqlitedict"` from dependencies.

### 9. Modify `src/clawless/config.py`

Add `bot_name: str = "Clawless"` to settings.

## New Message Flow

```
Channel webhook / test runner
  └─ store.store_message(id, sender, content, ...) → return ack

Message loop (polls every ~1s)
  ├─ store.get_all_senders_with_unprocessed()
  ├─ For each sender: store.get_unprocessed(sender)
  ├─ Route to channel by sender prefix
  └─ create_task → AgentManager.process_message()
       ├─ Advance cursor (save old for rollback)
       ├─ SDK client.query(prompt) with output_format schema
       │   ├─ Agent uses Claude Code tools as needed
       │   └─ ResultMessage.structured_output → {"text": "...", "media": [...]}
       ├─ HOST parses structured output
       ├─ HOST sends text + media via channel.send()
       ├─ On error before output: roll back cursor
       │   On error after output: keep cursor
       └─ On no output: send failure message to user
```

## Implementation Order

1. `store.py` (new) + `channels/base.py` (add field) + `channels/test.py` (uuid ids) — independent
2. `channels/whatsapp.py` — store write instead of create_task (depends on 1)
3. `tools.py` (gut) + `agent.py` (rewrite: structured output, message loop, cursors) + `app.py` (wire it all)
4. `pyproject.toml` — remove sqlitedict
5. `config.py` — add bot_name
6. Tests
7. Docs update

## Tests

**New `tests/test_store.py`** (unit, no API key):
- Session get/set roundtrip
- store_message returns True on first insert, False on duplicate
- get_unprocessed returns messages after cursor
- get_all_senders_with_unprocessed
- Cursor get/set/rollback
- WAL mode enabled

**Update `tests/helpers.py`:**
- Remove third scripted message that tested `send_message` tool (tool gone)
- Update to write messages to store instead of calling agent directly
- Add assertion: no single-char responses (validates dot-spam fix)
- Add assertion: structured output has text field

**Update `tests/test_channel_integration.py`:**
- Verify `clawless.db` exists in data dir after test run
- Verify messages table has inbound entries
- Verify agent responses arrive (host-controlled delivery works)

## Edge Cases

- **Agent produces structured output with text + media:** Host sends both. Normal case.
- **Agent produces structured output with text only:** Host sends text. Normal case.
- **Structured output validation fails (`error_max_structured_output_retries`):** Fall back to `ResultMessage.result` as plain text.
- **Agent produces no output:** Send failure message: "Sorry, I wasn't able to generate a response."
- **Timeout:** Send timeout message, close client.
- **Exception:** Send error message, roll back cursor if no output was sent.
- **Duplicate webhooks:** `store_message` uses `INSERT OR IGNORE` on PK — duplicate returns False, webhook acks normally.
- **Crash after output sent:** Cursor stays advanced, prevents duplicate sends on restart.
- **Crash before output:** Cursor not advanced past that message, message loop picks it up on restart.

## Files Modified

| File | Change |
|------|--------|
| `src/clawless/store.py` | **NEW** — SQLite message store with sessions, messages, cursors |
| `src/clawless/channels/base.py` | Add `message_id` field to InboundMessage |
| `src/clawless/channels/test.py` | Generate UUID message IDs, write to store |
| `src/clawless/channels/whatsapp.py` | Write to store instead of create_task, capture outbound SID |
| `src/clawless/tools.py` | Gut — empty MCP server harness |
| `src/clawless/agent.py` | Structured output, message loop, host delivery, cursors, new prompt |
| `src/clawless/app.py` | Wire store, build channel map, start message loop |
| `src/clawless/config.py` | Add bot_name setting |
| `pyproject.toml` | Remove sqlitedict dependency |
| `tests/test_store.py` | **NEW** — unit tests for MessageStore |
| `tests/helpers.py` | Update for store-based message flow |
| `tests/test_channel_integration.py` | Verify store DB and host-controlled delivery |
