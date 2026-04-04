# Plan: Give the agent a `send_message` custom tool

## Context

Currently, `process_message()` in agent.py waits for the entire SDK query to finish,
extracts `ResultMessage.result`, and makes a single `channel.send()` call at the end.
This means the agent cannot send media, cannot send intermediate updates during long
operations, and cannot interact conversationally mid-turn.

The Claude Agent SDK supports in-process custom tools via `@tool` + `create_sdk_mcp_server`.
We'll follow the pattern established in nanobot: define a `send_message` MCP tool whose
handler calls `channel.send()` as a side effect, track whether the tool was used during
the turn, and suppress the auto-sent final result if so.

## Approach

### 1. New file: `src/clawless/tools.py`

Define the `send_message` tool and the MCP server using closure variables for
per-request context (channel, sender), following nanobot's `set_context()` pattern.

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

def build_clawless_mcp_server():
    """Build in-process MCP server with clawless tools."""
    _channel = None
    _sender = ""
    _sent_in_turn = False

    def set_context(channel, sender):
        nonlocal _channel, _sender, _sent_in_turn
        _channel = channel
        _sender = sender
        _sent_in_turn = False

    def was_sent_in_turn():
        return _sent_in_turn

    @tool(
        "send_message",
        "Send a message to the user immediately. Use this to deliver files, "
        "images, or intermediate updates. You can call this multiple times. "
        "For media, provide local file paths in the media parameter.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text to send"},
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of local file paths to attach",
                },
            },
            "required": ["text"],
        },
    )
    async def send_message(args):
        nonlocal _sent_in_turn
        text = args.get("text", "")
        media = args.get("media") or []
        if not _channel or not _sender:
            return {"content": [{"type": "text", "text": "Error: no channel context"}], "is_error": True}
        await _channel.send(_sender, text=text, media=media or None)
        _sent_in_turn = True
        media_info = f" with {len(media)} attachments" if media else ""
        return {"content": [{"type": "text", "text": f"Message sent{media_info}"}]}

    server = create_sdk_mcp_server(
        name="clawless",
        version="1.0.0",
        tools=[send_message],
    )

    return server, set_context, was_sent_in_turn
```

### 2. Modify `src/clawless/agent.py`

**In `__init__`**: Build the MCP server once, store `set_context` and `was_sent_in_turn`.

**In `_build_options`**: Add `mcp_servers={"clawless": self._mcp_server}` and
`allowed_tools=["mcp__clawless__*"]` to `ClaudeAgentOptions`.

**In `process_message`**: 
- Call `self._set_context(channel, sender)` before the query
- After `_run_query()` completes, check `self._was_sent_in_turn()`:
  - If True: skip the final `channel.send()` (tool already sent)
  - If False: send `final_content` as before (backward compatible)

### 3. Update `src/clawless/init.py` — CLAUDE.md template

Update the PROJECT_CLAUDE_MD_TEMPLATE to document the `send_message` tool:

```
## Sending messages

Your final text response is automatically sent to the user. For intermediate
updates, media attachments, or multiple messages, use the send_message tool:

- send_message(text="Working on it...") — intermediate update
- send_message(text="Here's the file", media=["/path/to/file.png"]) — with attachment
- send_message(text="Part 1...") then send_message(text="Part 2...") — multiple messages

When you use send_message, your final text response is suppressed to avoid duplicates.
```

### 4. Files to modify/create

| File | Action |
|---|---|
| `src/clawless/tools.py` | **Create** — MCP server with send_message tool |
| `src/clawless/agent.py` | **Edit** — integrate MCP server, context setting, sent-in-turn check |
| `src/clawless/init.py` | **Edit** — update CLAUDE.md template with send_message docs |

### 5. Async architecture impact

**No changes needed** to the async model:
- The tool handler runs inside the SDK's query loop, which is already inside
  `process_message()`, which already holds the per-sender lock + semaphore
- `channel.send()` is async and already handles concurrent Twilio API calls
  via `asyncio.to_thread()`
- The timeout wraps the entire query including tool executions — tool calls
  count against the 300s budget, which is appropriate
- Test channel's `send()` is just list.append(), already supports multiple calls

**Closure variables are safe** because:
- `set_context()` is called at the start of `process_message()`
- Per-sender lock ensures only one message per sender is being processed
- The MCP server is shared across senders, but context is reset per-message

### 6. Verification

```
# Unit test: verify tool sends and sets flag
# Host integration test: update test channel messages to ask agent to create+send a file
# Docker integration test: same, via docker compose

uv run pytest tests/test_config.py -v          # existing unit tests still pass
uv run pytest tests/test_channel_integration.py -v -s  # host integration
uv run pytest -m docker -v -s                  # docker integration
```
