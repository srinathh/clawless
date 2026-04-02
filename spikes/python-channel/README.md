# Research Spike 2: Python MCP Channel

**Date:** 2026-04-02
**Branch:** `experiment/spike-2-python-mcp-channel`
**Status:** POSITIVE -- Python MCP SDK can implement a Claude Code channel

## Goal

Verify that the Python MCP SDK (`mcp` package on PyPI) can implement a channel
that Claude Code recognizes. The official channel plugins (Telegram, Discord,
iMessage) are all written in TypeScript/Bun.

## Environment

- Python 3.13 via uv 0.11.1
- `mcp` package version 1.27.0
- Claude Code CLI version 2.1.87
- OS: Linux (Ubuntu)

## Key Findings

### 1. Python MCP SDK supports experimental capabilities -- YES

The low-level `mcp.server.lowlevel.Server` API accepts `experimental_capabilities`
when creating initialization options:

```python
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import NotificationOptions

server = Server(name="my-channel", version="0.1.0", instructions="...")

init_options = server.create_initialization_options(
    notification_options=NotificationOptions(),
    experimental_capabilities={"claude/channel": {}},
)
```

This correctly produces the capability in the MCP initialize response:

```json
{
  "capabilities": {
    "experimental": { "claude/channel": {} },
    "tools": { "listChanged": false }
  }
}
```

**Important:** The high-level `FastMCP` API does NOT expose
`experimental_capabilities`. You must use the low-level `Server` from
`mcp.server.lowlevel`.

### 2. Custom notifications require raw JSON-RPC -- WORKAROUND WORKS

The Python SDK's `ServerNotification` type is a fixed union of standard MCP
notification types (progress, logging, resource updated, etc.). There is no
`notifications/claude/channel` type in the SDK.

**Workaround:** Write raw `JSONRPCNotification` messages directly to the
session's write stream:

```python
from mcp.types import JSONRPCMessage, JSONRPCNotification
from mcp.shared.message import SessionMessage

notification = JSONRPCNotification(
    jsonrpc="2.0",
    method="notifications/claude/channel",
    params={
        "content": "Hello from Python!",
        "meta": {"chat_id": "1", "sender": "test-user"},
    },
)
session_message = SessionMessage(message=JSONRPCMessage(notification))
await write_stream.send(session_message)
```

This produces the correct JSON-RPC output on stdout that Claude Code expects:

```json
{
  "method": "notifications/claude/channel",
  "params": {
    "content": "Hello from Python!",
    "meta": { "chat_id": "1", "sender": "test-user" }
  },
  "jsonrpc": "2.0"
}
```

### 3. Claude Code recognizes the Python channel -- YES

Tested with both `--mcp-config` and `--plugin-dir` loading modes:

```bash
# Via --mcp-config (bare server)
claude --mcp-config mcp-config.json \
  --dangerously-load-development-channels server:python-test-channel

# Via --plugin-dir (plugin mode)
claude --plugin-dir /path/to/spikes/python-channel \
  --dangerously-load-development-channels plugin:python-test-channel
```

Claude Code correctly:
- Loads the Python MCP server via uv
- Discovers the `claude/channel` experimental capability
- Registers the channel notification listener
- Reads the `instructions` and knows the `<channel>` tag format
- Discovers the `reply` tool and knows how to call it

### 4. Notification format matches the official spec

The official Claude Code channels reference specifies:

| Field     | Type                     | Description                                    |
|-----------|--------------------------|------------------------------------------------|
| `content` | `string`                | Becomes the body of the `<channel>` tag        |
| `meta`    | `Record<string, string>` | Each key becomes an attribute on the tag       |

The `source` attribute on the `<channel>` tag is set automatically by Claude Code
from the server's configured name.

Claude renders the notification as:

```xml
<channel source="python-test-channel" chat_id="1" sender="test-user">
Hello from Python!
</channel>
```

### 5. Stdio transport works correctly from Python

The `mcp.server.stdio.stdio_server()` context manager works exactly as expected.
Claude Code spawns the Python server as a subprocess and communicates over
stdin/stdout. No quirks observed.

### 6. HTTP server + MCP stdio coexistence works

Running a threaded HTTP server (for receiving webhooks) alongside the async MCP
stdio loop works. The pattern:

- Main async loop: MCP stdio via anyio
- Background thread: stdlib HTTPServer
- Cross-thread communication: `asyncio.run_coroutine_threadsafe()` to push
  notifications from the HTTP thread to the async MCP write stream

### 7. End-to-end notification delivery NOT fully tested

The `--print` mode exits after one turn (killing the MCP subprocess), so channel
notification delivery could not be tested end-to-end from a non-interactive
session. The `--input-format stream-json` mode did not produce output in testing.

**What was verified:**
- Server starts, declares capability, Claude Code recognizes it
- HTTP POST successfully pushes `notifications/claude/channel` to stdout
- The notification JSON matches the official spec exactly
- Claude Code knows the `<channel>` tag format and the reply tool

**What needs interactive testing:**
- Claude actually receiving and displaying `<channel>` tags from pushed notifications
- Claude calling the `reply` tool in response to a channel message
- Full round-trip: HTTP POST -> notification -> Claude response -> reply tool

This can be tested by running Claude Code in interactive (TTY) mode:

```bash
claude --mcp-config mcp-config.json \
  --dangerously-load-development-channels server:python-test-channel
```

Then in another terminal:

```bash
curl -X POST http://localhost:8799 -d "Hello from the channel test!"
```

### 8. No --channels flag in Claude Code v2.1.87

The implementation plan references `--channels` as a CLI flag, but this flag
does not exist in Claude Code v2.1.87. Instead:

- Use `--dangerously-load-development-channels server:<name>` for bare MCP servers
- Use `--dangerously-load-development-channels plugin:<name>` for plugin-wrapped servers
- The `--plugin-dir` flag loads plugins from a directory

The `--channels` flag may exist in newer versions or may be a different name
for the feature in the final release.

## Architecture Notes for WhatsApp Channel

Based on the spike findings, the WhatsApp channel implementation should:

1. **Use the low-level `mcp.server.lowlevel.Server` API** -- not FastMCP, because
   experimental capabilities are not exposed in FastMCP.

2. **Send notifications via raw JSONRPCNotification** -- the typed
   `ServerNotification` union does not support custom notification methods.
   Write raw `SessionMessage` objects to the write stream.

3. **Use `content` + `meta` notification format** -- NOT a nested object structure.
   The `content` field becomes the `<channel>` tag body, and `meta` entries
   become tag attributes.

4. **Run HTTP server in a background thread** -- the MCP stdio loop runs on the
   main async event loop. Use `asyncio.run_coroutine_threadsafe()` to bridge
   from the HTTP thread.

5. **Package as a plugin** with `.claude-plugin/plugin.json` and `.mcp.json` for
   proper Claude Code integration.

## Files

| File                          | Description                                       |
|-------------------------------|---------------------------------------------------|
| `server.py`                   | Minimal Python MCP channel server                 |
| `pyproject.toml`              | Python project with `mcp` dependency              |
| `.mcp.json`                   | MCP config for plugin mode (uses CLAUDE_PLUGIN_ROOT) |
| `mcp-config.json`             | MCP config for direct --mcp-config loading        |
| `.claude-plugin/plugin.json`  | Plugin manifest for Claude Code                   |

## How to Test

### Quick test (MCP protocol only)

```bash
cd spikes/python-channel
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | uv run server.py
```

### Test with Claude Code (non-interactive)

```bash
claude --print \
  --mcp-config /absolute/path/to/spikes/python-channel/mcp-config.json \
  --dangerously-load-development-channels server:python-test-channel \
  --dangerously-skip-permissions \
  "What channels and MCP servers are loaded?"
```

### Full interactive test

```bash
# Terminal 1: Start Claude with the channel
claude --mcp-config /absolute/path/to/spikes/python-channel/mcp-config.json \
  --dangerously-load-development-channels server:python-test-channel

# Terminal 2: Send a message through the channel
curl -X POST http://localhost:8799 -d "Hello from the channel!"
```

## Conclusion

**The Python MCP SDK CAN implement a Claude Code channel.** The two workarounds
required are:

1. Use the low-level `Server` API instead of FastMCP (for experimental capabilities)
2. Write raw `JSONRPCNotification` messages to the write stream (for custom
   notification methods)

Both workarounds are clean and well-supported by the SDK's architecture. The
notification format is identical to what the TypeScript SDK produces. No
Python-specific quirks with stdio transport were observed.

**Verdict: Proceed with Python for the WhatsApp channel implementation.**
