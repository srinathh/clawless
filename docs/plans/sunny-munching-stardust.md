# Add CLAUDE.md Templates to clawless-init

## Context

The Claude SDK loads `~/.claude/CLAUDE.md` (user-level) and `~/workspace/.claude/CLAUDE.md` (project-level) via `setting_sources=["user", "project"]`. Currently neither file exists after `clawless-init` runs. The agent has no persistent context about what it is, how media works, or what its environment looks like. Channel-specific formatting (WhatsApp rules, char limits) is already provided per-message via `formatting_instructions`, so CLAUDE.md should cover everything else.

## Changes

### 1. Add two template constants to `src/clawless/init.py`

**User-level (`~/.claude/CLAUDE.md`)** — identity and behavior:

```markdown
# Clawless Personal Assistant

You are a personal AI assistant running on the Clawless framework. Users reach you through messaging channels (currently WhatsApp).

## Communication style

- Be concise and conversational — this is a chat, not a document
- Skip preamble. Answer directly
- Channel-specific formatting rules are provided in each message — follow them
- When a task is done, say so briefly. Don't recap what you did unless asked
```

**Project-level (`~/workspace/.claude/CLAUDE.md`)** — workspace and capabilities:

```markdown
# Workspace

Your working directory is ~/workspace/. You have all Claude Code tools available with unrestricted permissions.

## Media

Inbound media from users arrives as `[mime/type: /path/to/file]` tags in the message text. The files are stored under ~/workspace/media/. You can read image files directly since you are multimodal.

Outbound media is not yet supported — you can only reply with text.

## Plugin

A plugin at ~/plugin/ may provide additional skills, agents, commands, and hooks. Check ~/plugin/skills/ for available skills if relevant to a task.
```

### 2. Update `init_home()` in `src/clawless/init.py`

- Create `workspace/.claude/` directory (currently doesn't exist)
- Write both CLAUDE.md files with the same if-not-exists guard as config.toml

### 3. Update `main()` print tree in `src/clawless/init.py`

Show the new files in the scaffolding output.

### 4. Update `docs/ARCHITECTURE.md`

Note that CLAUDE.md files are scaffolded by init, with their purposes.

## Files modified

| File | Change |
|------|--------|
| `src/clawless/init.py` | Add templates, create workspace/.claude/, write CLAUDE.md files |
| `docs/ARCHITECTURE.md` | Document CLAUDE.md scaffolding |

## Verification

```bash
# Re-run init on a fresh directory and verify files exist
rm -rf /tmp/test-clawless && uv run clawless-init /tmp/test-clawless
cat /tmp/test-clawless/.claude/CLAUDE.md
cat /tmp/test-clawless/workspace/.claude/CLAUDE.md

# Re-run init — verify it does NOT overwrite existing files
echo "custom" > /tmp/test-clawless/.claude/CLAUDE.md
uv run clawless-init /tmp/test-clawless
cat /tmp/test-clawless/.claude/CLAUDE.md  # should still say "custom"

# Run tests
uv run pytest tests/ -v --ignore=tests/test_channel_integration.py
```

---

# (Previous) Code Review: agent.py — Walkthrough, Q&A & Fixes

## Context

Code review of `src/clawless/agent.py` — the core module that manages Claude SDK client lifecycles, session persistence, and message processing.

---

## Code Walkthrough

### Imports (lines 1-26)

Standard library: `asyncio` (locks, semaphore), `json` (session map), `dataclass`, `Path`.

SDK types from `claude_agent_sdk`:
- `ClaudeSDKClient` — client that talks to the Claude CLI subprocess
- `ClaudeAgentOptions` — configuration bag
- `SystemMessage` — metadata (session init, etc.)
- `AssistantMessage` — streaming text/tool-use chunks (**imported but unused**)
- `ResultMessage` — final complete response

Internal: `Channel` protocol + `InboundMessage` from channel layer, `ClaudeConfig` from config.

### `_SessionClient` dataclass (lines 29-34)

Thin wrapper pairing a live SDK client with its session UUID. Session ID starts `None`, gets populated when the SDK sends a `SystemMessage` with subtype `"init"`. Underscore prefix = module-internal.

### `AgentManager.__init__` (lines 37-54)

Sets up:
- `_clients: dict[str, _SessionClient]` — sender key to live client. Lazily populated on first message.
- `_locks: dict[str, asyncio.Lock]` — per-sender locks. Serializes messages from the same sender (Claude conversations are sequential).
- `_concurrency_gate: asyncio.Semaphore` — caps total concurrent SDK calls (default 3 from `ClaudeConfig`). Prevents API overload.
- `_session_map` / `_session_map_path` — loaded from `~/data/claude_sessions.json` at startup. Persistence layer that survives restarts.

Called from `app.py` during FastAPI lifespan, which passes workspace (`~/workspace`) and data dir (`~/data`).

### Session map persistence (lines 60-74)

- **`_load_session_map`** (lines 60-66) — reads `sender -> session_id` mapping from disk. If corrupt/unreadable, logs warning and starts fresh (losing session continuity > failing to boot).
- **`_save_session_map`** (lines 68-70) — writes full map to disk. Defensive `mkdir` for edge cases, though `data/` should exist from `clawless-init`.
- **`_record_session`** (lines 72-74) — updates in-memory map + flushes to disk. Called when a new session ID is captured from the SDK.

### `_build_options` (lines 80-103)

Constructs `ClaudeAgentOptions`:

- **`allowed_tools`** — Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch. No Agent (no sub-agents), no TodoWrite, no Notebook.
- **`permission_mode="bypassPermissions"`** — no tool approval prompts. Safety comes from OS-level isolation (non-root Docker user), not SDK permission prompts.
- **`setting_sources=["user", "project"]`** — SDK reads both `~/.claude/settings.json` and `~/workspace/.claude/settings.json` + CLAUDE.md.
- **`plugins`** — conditionally included via `**` spread. `if p` filter skips empty strings.
- **`resume`** (lines 97-103) — if a persisted session ID exists for this sender, sets `options.resume` so the SDK continues the conversation instead of starting fresh. This is what gives conversation continuity across restarts.

**Pylance error on plugins**: The SDK expects `list[SdkPluginConfig]` (a `TypedDict` with `type: Literal["local"]` and `path: str`), but we're passing `list[dict[str, str]]`. Functionally correct at runtime, but Pylance can't prove it. Fix: import `SdkPluginConfig` from `claude_agent_sdk.types` and annotate, or just build with the typed dict directly.

### `_get_or_create_client` (lines 105-116)

Lazy initialization — first message from a sender creates the client, subsequent messages reuse it. The SDK client is an async context manager (spawns a subprocess), so `__aenter__` starts it. Client lives until `_close_client` or `close_all`.

Note: `cli_session_id` is fetched again from the map at line 113 even though `_build_options` already used it — slightly redundant but harmless.

### `_close_client` (lines 118-125)

Removes client from tracking, shuts down SDK subprocess via `__aexit__`, removes the lock. `debug`-level logging for teardown errors — not actionable during shutdown.

### `process_message` — the core method (lines 131-171)

**Lock acquisition (lines 138-141):**
`setdefault` atomically gets-or-creates the per-sender lock (safe in single-threaded asyncio). `async with lock, self._concurrency_gate` acquires both: per-sender serialization first, then global concurrency cap.

**Prompt construction (lines 146-147):**
Prepends channel's `formatting_instructions` in square brackets (e.g. WhatsApp formatting rules), then the message content. `query()` sends to the SDK.

**Response streaming (lines 148-157):**
The SDK streams multiple message types. This loop cares about two:
1. `SystemMessage` with subtype `"init"` — captures session UUID. The `new_id != sc.session_id` check avoids redundant disk writes on resume.
2. `ResultMessage` — final message with complete response text. Everything else (tool calls, intermediate text) is ignored.

**Reply & error handling (lines 159-171):**
Fallback `"Done -- no text response."` if agent did work but produced no text. Two-layer error handling: try to notify user on failure; if even that fails, just log. `logger.exception` captures full traceback.

### `close_all` (lines 177-180)

Called during FastAPI lifespan shutdown. `list(self._clients)` snapshots keys since `_close_client` mutates the dict. Iterates and shuts down each SDK subprocess.

---

## Questions & Answers

### Q1: Locks vs. SQLite sequential approach?

**asyncio.Lock is the right choice here.** The app is confirmed single-threaded: `app.py:74` runs `uvicorn.run()` with no `workers` parameter (defaults to 1), and there are no `run_in_executor` calls anywhere. asyncio locks serialize coroutines within one event loop — exactly what we need.

A SQLite queue (like nanobot's bus) would make sense if: (a) you had multiple worker processes, (b) you needed durable message ordering across restarts, or (c) you wanted to decouple message receipt from processing. None of those apply here — messages come in via webhooks, get processed immediately, and the only concurrency is multiple senders hitting the same event loop. The lock + semaphore pattern is simpler and has no I/O overhead.

### Q2: Why limit tools? Remove allowed_tools.

Confirmed via SDK source: `ClaudeAgentOptions.allowed_tools` defaults to `[]`, and when empty, the `--allowedTools` flag is **not passed to the CLI** — meaning all tools are available. Simply remove the `allowed_tools` field from `_build_options`.

### Q3: Pylance error on plugins

The SDK defines `SdkPluginConfig` as a `TypedDict` in `claude_agent_sdk.types`:
```python
class SdkPluginConfig(TypedDict):
    type: Literal["local"]
    path: str
```

Our code builds `list[dict[str, str]]` which is structurally compatible but Pylance can't prove it matches the TypedDict. Fix: import `SdkPluginConfig` and use it to annotate the list.

### Q4: Disk-backed KV store for session map — Approved: sqlitedict

The current hand-rolled JSON read/write works but isn't crash-safe (partial write = corrupt file). Options:

| Library | API | Crash-safe | Async | Notes |
|---------|-----|-----------|-------|-------|
| `shelve` (stdlib) | dict-like | No (platform-dependent corruption) | No | Pickle-based, avoid |
| `sqlitedict` (PyPI) | dict-like with `commit()` | Yes (SQLite) | No | Simple, needs manual commit or autocommit flag |
| `dbm` (stdlib) | dict-like | Depends on backend | No | String values only, platform-dependent |
| `diskcache` (PyPI) | `set()`/`get()` | Yes (SQLite + WAL) | No | Most robust, auto-persists, thread/process-safe |

**Decision: `sqlitedict` with `autocommit=True`.** Dict-like interface, auto-persists on every write, SQLite-backed so crash-safe. Eliminates `_load_session_map`, `_save_session_map`, and `_record_session` entirely.

### Q5: Are we sure we're single-threaded?

**Yes.** `app.py:74` calls `uvicorn.run("clawless.app:app", host="0.0.0.0", port=settings.port)` — no `workers` param, no thread pool usage, no `run_in_executor` anywhere. Single event loop, single thread.

### Q6: Why `__aenter__` at all? Is there a less fragile way?

**Why it's used**: `ClaudeSDKClient` is an async context manager. Normally you'd use `async with client:` but the client needs to outlive the scope of a single method — it's created once in `_get_or_create_client` and reused across many `process_message` calls. You can't use `async with` when the lifetime extends beyond one block.

**Better alternative: `contextlib.AsyncExitStack`**. This is the stdlib tool for exactly this problem — managing async context managers whose lifetimes extend beyond a single `async with` block:

```python
from contextlib import AsyncExitStack

class AgentManager:
    def __init__(self, ...):
        ...
        self._exit_stack = AsyncExitStack()

    async def _get_or_create_client(self, session_key):
        ...
        client = ClaudeSDKClient(options=options)
        await self._exit_stack.enter_async_context(client)  # clean, no raw dunders
        sc = _SessionClient(client=client, ...)
        self._clients[session_key] = sc
        return sc

    async def close_all(self):
        await self._exit_stack.aclose()  # cleans up ALL clients
        self._clients.clear()
```

Benefits:
- No raw `__aenter__`/`__aexit__` calls anywhere
- `enter_async_context` handles the enter-and-track-for-cleanup in one call
- If enter succeeds but later code fails, the stack still knows about the client
- `aclose()` cleans up everything in reverse order
- `_close_client` for individual senders still needs raw `__aexit__` though (exit stack doesn't support removing individual items), so we keep it for that case but with the `__aenter__` guard

**Revised approach**: Use `AsyncExitStack` for the "happy path" in `_get_or_create_client`. For `_close_client` (individual sender teardown, e.g. on timeout), pop from `_clients` and call `__aexit__` directly — this is the one case where raw dunders are unavoidable, but it's simpler since the client is already tracked.

Actually, on reflection, `AsyncExitStack` doesn't support removing individual context managers — it's all-or-nothing with `aclose()`. Since we need per-sender close (on timeout), let's keep the current approach but with the try/except guard on `__aenter__`. It's 3 lines and covers the leak.

### Q7: Lock removal in `_close_client` — Decision: keep locks

**Option A: Keep removing the lock (current behavior)**
- Pro: Clean slate — no stale locks accumulate
- Con: If a message is in-flight holding the old lock, a new message for the same sender creates a fresh lock and bypasses serialization.

**Option B: Stop removing the lock**
- Pro: In-flight messages complete safely. No race.
- Con: Locks accumulate (one per sender, forever). Negligible memory for personal use.

**Decision: Option B** — stop removing the lock.

### Q8: Unit tests for agent.py?

Not worth adding unit tests for `_build_options` or client idempotency — the integration test covers the real behavior, and these methods are simple enough that testing them in isolation adds maintenance without catching real bugs. We'll rely on the existing end-to-end integration test.

---

## Issues Summary

| # | Issue | Status |
|---|-------|--------|
| 1 | Remove unused `AssistantMessage` import | Approved |
| 2 | Add `asyncio.wait_for` timeout | Approved |
| 3 | Add `logger.debug` for unhandled messages | Approved |
| 4 | Guard `__aenter__` with try/except | Approved |
| 5 | Stop removing locks in `_close_client` | Approved |
| 6 | Remove `allowed_tools` (all tools available) | Approved |
| 7 | Replace JSON session map with `sqlitedict` | Approved |
| 8 | Fix Pylance error on plugins TypedDict | Approved |

---

## Implementation Plan

### Step 1: Add `sqlitedict` dependency

Add `sqlitedict` to `pyproject.toml` dependencies.

### Step 2: Update `ClaudeConfig` (config.py)

Add `request_timeout: float = 300.0` field.

### Step 3: Rewrite agent.py

All changes in one pass:

**Imports:**
- Remove `AssistantMessage`, remove `json`
- Add `from sqlitedict import SqliteDict`
- Add `from claude_agent_sdk import SdkPluginConfig` (or from `claude_agent_sdk.types`)

**`__init__`:**
- Replace `_session_map_path` / `_session_map` / `_load_session_map()` with:
  ```python
  self._session_map = SqliteDict(str(data_dir / "sessions.db"), autocommit=True)
  ```

**Delete methods:**
- `_load_session_map`, `_save_session_map`, `_record_session`

**`_build_options`:**
- Remove `allowed_tools` entirely (empty default = all tools available)
- Fix Pylance: type-annotate plugins list with `SdkPluginConfig`
- Session resume logic unchanged

**`_get_or_create_client`:**
- Add try/except guard after `__aenter__`:
  ```python
  await client.__aenter__()
  try:
      sc = _SessionClient(client=client, session_id=self._session_map.get(session_key))
      self._clients[session_key] = sc
  except Exception:
      await client.__aexit__(None, None, None)
      raise
  return sc
  ```

**`_close_client`:**
- Remove `self._locks.pop(session_key, None)` — keep locks

**`process_message`:**
- Extract query+receive into inner `_run_query()` coroutine
- Wrap in `asyncio.wait_for(timeout=self._config.request_timeout)`
- Add `else: logger.debug(...)` for unhandled message types
- Add explicit `except asyncio.TimeoutError` that closes the client and notifies user
- Replace `self._record_session(sender, new_id)` with direct `self._session_map[sender] = new_id`

**`close_all`:**
- Close `self._session_map` (SqliteDict) after closing clients

### Step 4: Update ARCHITECTURE.md

Reflect removal of `allowed_tools`, switch to `sqlitedict`, and new timeout config.

---

## Files Modified

| File | Change |
|------|--------|
| `pyproject.toml` | Add `sqlitedict` dependency |
| `src/clawless/config.py` | Add `request_timeout: float = 300.0` to `ClaudeConfig` |
| `src/clawless/agent.py` | All fixes listed above |
| `docs/ARCHITECTURE.md` | Update to reflect changes |

## Verification

```bash
uv run pytest tests/test_config.py -v              # config model change
uv run pytest tests/test_channel_integration.py -v  # full pipeline still works
```
