# Plan: Implement `send_message` MCP Tool

## Context

Currently, `process_message()` in [agent.py](src/clawless/agent.py) waits for the entire SDK query to finish, extracts the final `ResultMessage.result`, and makes a single `channel.send()` call at the end (line 143). This means the agent cannot send media, intermediate updates, or multiple messages during a turn.

The Claude Agent SDK (v0.1.54) supports in-process custom tools via `@tool` + `create_sdk_mcp_server`. We'll define a `send_message` MCP tool as the **only way** the agent communicates with the user — matching how nanobot works and how the user's n8n implementation operates. No auto-send of final results.

Starter plan: [send-message-tool.md](docs/plans/send-message-tool.md)

## Design Decisions

1. **`send_message` is the only reply mechanism** — the agent MUST use the `send_message` tool for all responses. The post-query auto-send is removed entirely. Tool instructions go in a system prompt constant in code (prepended to the query), not in CLAUDE.md templates.

2. **`allowed_tools` must explicitly list MCP tools** — the SDK default (empty list) means no tools are allowed. We must set `allowed_tools` to include both built-in SDK tools and `mcp__clawless__*`. This matches nanobot's pattern.

3. **Module-level context state** — per-request state (`_channel`, `_sender`, `_sent_in_turn`) lives in a module-level dict in `tools.py`, with `set_context()` and `was_sent_in_turn()` accessors. Tool definitions are top-level functions (not inside a factory), making it easy to add new tools — just define a `@tool` function and add it to the `tools=[]` list in `build_clawless_mcp_server()`. Safe because concurrent senders are serialized by the per-sender lock + single-user personal assistant use case.

4. **No CLAUDE.md template changes for tools** — tool instructions live in code as a system prompt constant, not in the scaffolded CLAUDE.md templates. Existing deployments get the instructions automatically on next restart.

## Implementation Steps

### Step 1: Create `src/clawless/tools.py`

Tool definitions at module level, factory just assembles the MCP server.

```python
"""Custom MCP tools for the clawless agent."""

from __future__ import annotations

import logging

from claude_agent_sdk import create_sdk_mcp_server, tool

from clawless.channels.base import Channel

logger = logging.getLogger(__name__)

# Per-request context — set before each query by AgentManager.process_message().
# Safe with concurrent senders: per-sender lock serializes processing, and
# the tool handler runs within the same async task as the query.
_ctx: dict = {"channel": None, "sender": "", "sent_in_turn": False}


def set_context(channel: Channel, sender: str) -> None:
    """Bind channel and sender for the current turn."""
    _ctx["channel"] = channel
    _ctx["sender"] = sender
    _ctx["sent_in_turn"] = False


def was_sent_in_turn() -> bool:
    """Return True if send_message was called during the current turn."""
    return _ctx["sent_in_turn"]


@tool(
    "send_message",
    "Send a message or reply to the user. This is the ONLY way to communicate "
    "with the user — you MUST call this tool for every response, including final "
    "answers, intermediate updates, and media/file deliveries. You can call it "
    "multiple times in one turn. For media, provide local file paths.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Message text to send"},
            "media": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of local file paths to attach",
            },
        },
        "required": ["text"],
    },
)
async def send_message(args):
    text = args.get("text", "")
    media = args.get("media") or []
    channel = _ctx["channel"]
    sender = _ctx["sender"]
    if not channel or not sender:
        return {"content": [{"type": "text", "text": "Error: no channel context"}], "is_error": True}
    await channel.send(sender, text=text, media=media or None)
    _ctx["sent_in_turn"] = True
    logger.info("send_message: sent to %s (%d chars, %d media)", sender, len(text), len(media))
    media_info = f" with {len(media)} attachments" if media else ""
    return {"content": [{"type": "text", "text": f"Message sent{media_info}"}]}


def build_clawless_mcp_server():
    """Build in-process MCP server with all clawless tools.

    To add a new tool: define it with @tool above, then add it to the list here.
    """
    return create_sdk_mcp_server(
        name="clawless",
        version="1.0.0",
        tools=[send_message],
    )
```

### Step 2: Edit `src/clawless/agent.py`

**2a. Add imports** (after line 24):
```python
from clawless.tools import build_clawless_mcp_server, set_context, was_sent_in_turn
```

**2b. Add system prompt constant** (after imports, before `_SessionClient` class):
```python
TOOL_SYSTEM_PROMPT = """\
You MUST use the send_message tool for ALL communication with the user.
Your final text response is NOT delivered — only send_message calls reach the user.
Always call send_message at least once per turn with your reply.
For media/files, include local file paths in the media parameter."""
```

**2c. Build MCP server in `__init__`** (after line 51, the `_session_map` line):
```python
self._mcp_server = build_clawless_mcp_server()
```

**2d. Add `mcp_servers` and `allowed_tools` to `_build_options`** — update `ClaudeAgentOptions` constructor (lines 61-68):
```python
options = ClaudeAgentOptions(
    permission_mode="bypassPermissions",
    max_turns=self._config.max_turns,
    max_budget_usd=self._config.max_budget_usd,
    setting_sources=["user", "project"],
    cwd=str(self._workspace),
    plugins=plugins,
    mcp_servers={"clawless": self._mcp_server},
    allowed_tools=[
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
        "WebSearch", "WebFetch",
        "mcp__clawless__*",
    ],
)
```

**2e. Set context and prepend system prompt in `_run_query`** — in `process_message`, after line 117 (`sc = await self._get_or_create_client(sender)`):
```python
set_context(channel, sender)
```

And update the prompt construction inside `_run_query` (line 120):
```python
# Before:
prompt = f"[{channel.formatting_instructions}]\n\n{message.content}"

# After:
prompt = f"{TOOL_SYSTEM_PROMPT}\n\n[{channel.formatting_instructions}]\n\n{message.content}"
```

**2f. Remove auto-send, add fallback warning** — replace lines 140-143:
```python
# Before:
if not final_content:
    final_content = "Done — no text response."
await channel.send(sender, text=final_content)

# After:
if not was_sent_in_turn():
    logger.warning("Agent did not use send_message tool for %s", sender)
    if final_content:
        await channel.send(sender, text=final_content)
```

This logs a warning if the agent failed to use the tool but still delivers the content as a safety net. If there's no content AND no tool use, the user gets nothing (which is appropriate — the agent was told to use the tool and didn't).

### Step 3: Edit `src/clawless/init.py`

Append to `PROJECT_CLAUDE_MD_TEMPLATE` (before the closing `"""`), after the Plugin section:

```
## Sending messages

Use the send_message tool for ALL replies to the user. Your final text response
is NOT delivered directly — only send_message calls reach the user.

- send_message(text="Here's your answer...") — reply to the user
- send_message(text="Here's the file", media=["/path/to/file.png"]) — with attachment
- send_message(text="Working on it...") then send_message(text="Done!") — multiple messages
```

Note: this reinforces the system prompt instruction in the project CLAUDE.md. Both exist for defense-in-depth — the system prompt is authoritative (always present), the CLAUDE.md helps for longer conversations where the system prompt may be compressed away.

### Step 4: Edit integration tests

**Both [test_channel_integration.py](tests/test_channel_integration.py) and [test_docker_integration.py](tests/test_docker_integration.py):**

Add a third scripted message to `TOML_CONFIG`:
```toml
messages = ["Hello, who are you?", "What is 2+2?", "Use the send_message tool to send me a message saying exactly 'tool-test-ok'"]
```

Update assertions in `test_scripted_messages_get_responses`:
- Change `assert len(responses) == 2` to `assert len(responses) >= 3`
- Add marker verification:
```python
all_text = " ".join(r["text"] for r in responses)
assert "tool-test-ok" in all_text
```

### Step 5: Update `docs/ARCHITECTURE.md`

- Add `mcp_servers` and `allowed_tools` rows to the ClaudeAgentOptions table
- Update message processing flow to note: agent uses `send_message` tool for all replies
- Add "Custom MCP Tools" section documenting `send_message` and how to add new tools

## Files Summary

| File | Action |
|---|---|
| `src/clawless/tools.py` | **Create** — module-level tools + MCP server factory |
| `src/clawless/agent.py` | **Edit** — imports, MCP server, allowed_tools, system prompt, remove auto-send |
| `src/clawless/init.py` | **Edit** — append send_message docs to CLAUDE.md template |
| `tests/test_channel_integration.py` | **Edit** — 3rd message + assertions |
| `tests/test_docker_integration.py` | **Edit** — same as above |
| `docs/ARCHITECTURE.md` | **Edit** — document new capability |

## Verification

```bash
# Unit tests (existing, should still pass)
uv run pytest tests/test_config.py -v

# Host integration test (verifies send_message tool works end-to-end)
uv run pytest tests/test_channel_integration.py -v -s

# Docker integration test
uv run pytest -m docker tests/test_docker_integration.py -v -s
```
