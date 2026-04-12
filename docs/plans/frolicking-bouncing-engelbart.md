# Plan: Dead Session Recovery + Send AssistantMessage TextBlocks

## Context

Clawless stopped responding after the SDK session died at 23:51 UTC on 2026-04-11. The dead session was never cleaned up, so every subsequent message failed. Additionally, the current code discards all AssistantMessage content (TextBlocks with real intermediate responses) and only extracts from the final ResultMessage. Also, production runs at LOG_LEVEL=INFO, missing all DEBUG diagnostics.

Three fixes: (1) session recovery, (2) send TextBlocks, (3) enable DEBUG logging.

## 1. store.py — Add `delete_session(sender)`

After `set_session()` (~line 76). Simple `DELETE FROM sessions WHERE sender = ?` + commit.

## 2. agent.py — Add `_reset_session(session_key)` helper

After `_close_client()` (~line 187). Combines:
- `await self._close_client(session_key)` — removes in-memory client
- `self._store.delete_session(session_key)` — clears persisted session_id so next message starts fresh (no resume of dead session)

## 3. agent.py — Use `_reset_session` in all three error paths

- **No response** (line ~275): call `_reset_session`, send "Sorry, I wasn't able to generate a response. I have reset the agent, please try again."
- **Timeout** (line ~282): replace `_close_client` with `_reset_session`
- **Exception** (line ~292): call `_reset_session` before sending error message

## 4. agent.py — Send AssistantMessage TextBlocks

Modify `_run_query()` to also extract non-empty `TextBlock` content from `AssistantMessage` messages and send them immediately via the channel.

Key insight from log analysis: `receive_response()` only replays history when the SDK client is newly created with `options.resume`. Within a living client's lifetime (sequential `query()` calls), it only yields new messages. Replay only happens after pod restart (session resumed from SQLite). Since we now `_reset_session` on all errors (clearing the stored session_id), resume only happens on **clean** pod restarts with a healthy session — the rarest case.

Approach for handling replay on resume:
- `_build_options()` already knows whether we're resuming (line 158-159 sets `options.resume`)
- Pass a `is_resuming: bool` flag into `_run_query()`
- When resuming: only send TextBlocks that appear AFTER we see a ResultMessage (the replayed final result marks the end of history; anything after is the new response)
- When not resuming: send all non-empty TextBlocks immediately

In `_run_query()`:
```python
# Import at top of file
from claude_agent_sdk import AssistantMessage

# Inside _run_query, add handling for AssistantMessage:
elif isinstance(msg, AssistantMessage):
    for block in msg.content:
        if hasattr(block, 'text') and isinstance(block, TextBlock):
            if block.text.strip() and (not is_resuming or past_history):
                await channel.send(sender, text=block.text)
                output_sent = True  # need nonlocal
    # ... existing unhandled logging for other types
```

Also need to import `TextBlock` from `claude_agent_sdk` (check the types module).

Note: `ToolUseBlock(name='StructuredOutput')` in AssistantMessages contains the same content that arrives in `ResultMessage.structured_output` — don't extract from it (would duplicate the final response).

## 5. deployment.yaml — Change LOG_LEVEL to DEBUG

At `deploy/k8s/deployment.yaml:50`, change `"INFO"` to `"DEBUG"`. Keep all log statements in code at their current levels.

## 6. tests/test_store.py — Add `delete_session` tests

Two tests in `TestSessions`:
- `test_delete_session`: set then delete, verify get returns None
- `test_delete_nonexistent_session`: should not raise

## Files to modify

- `src/clawless/store.py` — add `delete_session()`
- `src/clawless/agent.py` — add `_reset_session()`, update 3 error paths, add TextBlock sending in receive loop
- `deploy/k8s/deployment.yaml` — LOG_LEVEL → DEBUG
- `tests/test_store.py` — add 2 tests

## Verification

```bash
# Unit tests
uv run pytest tests/test_store.py tests/test_config.py tests/test_utils.py tests/test_base.py -v

# Integration test (needs ANTHROPIC_API_KEY)
uv run pytest tests/test_channel_integration.py -v -s
```

After deploying: send a message via WhatsApp. If session was dead, first message should log "Reset session" and respond with the reset notice. Second message should work normally with a fresh session. DEBUG logs should now be visible in `kubectl logs`.
