# Plan: Fix Message Architecture — Host-Controlled Delivery

## Context

Clawless delegates message delivery to the agent via a `send_message` MCP tool. The agent calls the tool, the tool calls `channel.send()` as a side-effect, and returns `"Message sent"` back to the agent. This caused the dot-spam bug: the agent couldn't distinguish tool results from new user messages and entered an infinite `send_message(".")` loop.

The current fixes (trivial message rejection, rate limiting, prompt warnings in commit 3456459) are band-aids. Nanoclaw avoids this class of bug entirely because the host controls delivery: the agent returns data, the host sends it. The SDK already supports this — `ResultMessage.result` captures the agent's final text, but the code at `agent.py:218-222` is commented out and unused.

This plan switches clawless to host-controlled delivery and adds a proper message store for observability, deduplication, and crash recovery.

## Changes

### 1. New file: `src/clawless/store.py`

SQLite message store replacing SqliteDict. Uses stdlib `sqlite3` with WAL mode.

**Schema:**

```sql
CREATE TABLE sessions (
    sender      TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender      TEXT NOT NULL,
    message_id  TEXT,           -- platform ID (Twilio MessageSid), NULL for outbound
    direction   TEXT NOT NULL,  -- 'inbound' or 'outbound'
    content     TEXT NOT NULL DEFAULT '',
    media_files TEXT,           -- JSON array or NULL
    sender_name TEXT DEFAULT '',
    is_from_bot INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE(sender, message_id)  -- dedup constraint
);

CREATE TABLE cursors (
    sender          TEXT PRIMARY KEY,
    last_message_id INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);
```

**Class `MessageStore`** with methods:
- `get_session(sender)` / `set_session(sender, session_id)` — replaces SqliteDict
- `store_inbound(sender, content, message_id, ...)` → returns row ID
- `store_outbound(sender, content, ...)` → returns row ID
- `is_duplicate(sender, message_id)` → bool
- `get_cursor(sender)` / `set_cursor(sender, message_id)` — per-sender watermark
- `close()`

**Migration:** On init, if `sessions.db` (SqliteDict) exists alongside the new DB, read its `unnamed` table with `sqlite3` + `pickle.loads()`, insert into `sessions`, rename old file to `.migrated`.

### 2. Modify `src/clawless/channels/base.py`

Add `message_id: str = ""` field to `InboundMessage` (between `content` and `media_files`). Empty string means no dedup check.

### 3. Modify `src/clawless/channels/whatsapp.py`

- Pass `message_sid` as `message_id` when constructing `InboundMessage` (line 117)
- Add dedup check before `asyncio.create_task` (line 126): query `request.app.state.store.is_duplicate()`, drop with empty TwiML if duplicate

### 4. Modify `src/clawless/agent.py` — Core change

**4a. Constructor:** Accept `store: MessageStore` parameter instead of creating SqliteDict internally. Remove `sqlitedict` import.

**4b. `process_message()`** — new flow:
1. Store inbound message → get `msg_row_id`
2. Advance cursor optimistically to `msg_row_id` (save old cursor)
3. Run agent via SDK
4. **Primary delivery:** capture `ResultMessage.result`, host calls `channel.send()` and `store.store_outbound()`
5. On error before output: roll back cursor to old value
6. `was_sent_in_turn()` still tracked — if agent used `send_message` for intermediate updates AND produced final content, both reach the user (intermediate updates are progress messages, final content is the answer)

**4c. `FRAMEWORK_SYSTEM_PROMPT`** — replace with:

```
Your text responses are delivered to the user automatically. Simply respond naturally.
You do NOT need to call send_message for normal replies — just write your response.

The send_message tool is available for:
(1) Intermediate progress updates during long operations
(2) Sending media/file attachments (the only way to deliver files)

IMPORTANT — avoid loops:
- After calling send_message, you will see a tool result confirmation.
  This is NOT a new user message — do NOT respond to it.
- If you have already provided your response, STOP.
```

Plus the existing media handling, skills/plugins, and workspace sections (unchanged).

**4d.** Replace `self._session_map` calls with `self._store` equivalents.

### 5. Modify `src/clawless/tools.py`

- `set_context()` accepts optional `store` parameter
- Tool description changed: "Send an INTERMEDIATE message... Your final text response is delivered automatically."
- Tool handler logs outbound to store via `store.store_outbound()`
- Tool result text changed to `"Intermediate message delivered to user."` (clearer it's not a user message)
- Rate limiting and trivial rejection kept as defense-in-depth

### 6. Modify `src/clawless/app.py`

- Create `MessageStore` in lifespan, attach to `app.state.store`
- Pass store to `AgentManager` constructor
- Close store on shutdown

### 7. Modify `pyproject.toml`

Remove `"sqlitedict"` from dependencies. Migration reads the old format with raw `sqlite3`.

## New Message Flow

```
Webhook → Channel._handle_webhook()
  ├─ Dedup: store.is_duplicate(sender, message_sid)? → drop
  └─ create_task → AgentManager.process_message()
       ├─ store.store_inbound() → msg_row_id
       ├─ store.set_cursor(sender, msg_row_id)  [optimistic advance]
       ├─ SDK client.query(prompt)
       │   ├─ [optional] Agent calls send_message → intermediate update sent + logged
       │   └─ ResultMessage.result → final_content
       ├─ HOST sends final_content via channel.send()  ← THE KEY CHANGE
       ├─ store.store_outbound(sender, final_content)
       └─ On error before output: roll back cursor
```

## Files Modified

| File | Change |
|------|--------|
| `src/clawless/store.py` | **NEW** — SQLite message store |
| `src/clawless/channels/base.py` | Add `message_id` field to InboundMessage |
| `src/clawless/channels/whatsapp.py` | Pass message_sid, add dedup check |
| `src/clawless/agent.py` | Host-controlled delivery, cursor tracking, new system prompt |
| `src/clawless/tools.py` | Demote send_message to optional, log outbound |
| `src/clawless/app.py` | Wire store into lifespan |
| `pyproject.toml` | Remove sqlitedict dependency |

## Test Strategy

**New `tests/test_store.py`** (unit, no API key):
- Session roundtrip, message storage, deduplication, cursor tracking
- SqliteDict migration (create old format DB, verify migration)
- WAL mode verification

**Update `tests/helpers.py`**:
- Third scripted message: change to test intermediate send_message ("Send me an intermediate progress message saying 'tool-test-ok' using send_message, then give your final answer")
- `assert_agent_responses`: keep `"tool-test-ok"` assertion (proves send_message tool still works for intermediate updates)
- Add assertion: verify no dot/loop messages in responses

**Update integration test**:
- After test run, verify `clawless.db` exists in data dir
- Verify messages table has entries for both inbound and outbound

## Implementation Order

1. `store.py` + `channels/base.py` (independent, parallel)
2. `channels/whatsapp.py` (depends on 1)
3. `agent.py` + `tools.py` + `app.py` (tightly coupled, together)
4. `pyproject.toml`
5. Tests
6. Update `docs/ARCHITECTURE.md` and `docs/CODE_WALKTHROUGH.md`

## Edge Cases

- **Agent uses send_message AND produces ResultMessage.result:** Both reach user. Intermediate messages are progress updates; final content is the answer. This is correct behavior.
- **Agent produces no ResultMessage.result and didn't use send_message:** Log warning, no message sent. Same as current behavior.
- **Agent only uses send_message (e.g., for media):** `final_content` is empty, `was_sent_in_turn()` is True — no extra send needed. Correct.
- **Duplicate webhooks racing:** First passes dedup, fires task. Second may also pass dedup (not yet stored). Per-sender lock serializes processing. `store_inbound` uses `INSERT OR IGNORE` on the unique constraint — second insert is a no-op and returns 0, caller skips processing.
