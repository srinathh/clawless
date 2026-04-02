"""
Minimal Python MCP channel server for Research Spike 2.

Goal: verify that the Python MCP SDK can implement a channel that Claude Code
recognizes.

Architecture:
- Low-level mcp.server.lowlevel.Server (NOT FastMCP) because FastMCP does not
  expose experimental_capabilities.
- Declares experimental capability "claude/channel" via the standard
  experimental_capabilities dict.
- Sends custom "notifications/claude/channel" by writing raw JSONRPCNotification
  messages directly to the session write stream, because the typed
  ServerNotification union in the Python SDK does not include custom notification
  types.
- Starts an HTTP listener on port 8799 (in a background task alongside the
  stdio MCP loop).
- On any POST to /, pushes the POST body as a channel notification.
- Exposes a "reply" tool that prints to stderr (simulating sending a reply).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectSendStream

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Global reference to the write stream so the HTTP handler can push notifications
_write_stream: MemoryObjectSendStream[SessionMessage] | None = None
_loop: asyncio.AbstractEventLoop | None = None

# ---------------------------------------------------------------------------
# MCP Server setup
# ---------------------------------------------------------------------------

server = Server(
    name="python-test-channel",
    version="0.1.0",
    instructions=(
        'Messages arrive as <channel source="python-test-channel" chat_id="..." sender="...">. '
        "Reply with the reply tool, passing the chat_id from the tag."
    ),
)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="reply",
            description="Send a reply message back through the channel",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The channel/chat ID to reply to",
                    },
                    "text": {
                        "type": "string",
                        "description": "The reply text",
                    },
                },
                "required": ["chat_id", "text"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    if name == "reply":
        args = arguments or {}
        chat_id = args.get("chat_id", "unknown")
        text = args.get("text", "")
        # In a real channel, this would send via WhatsApp/Telegram/etc.
        # For the spike, just log to stderr so we can see it.
        print(f"[REPLY to {chat_id}]: {text}", file=sys.stderr)
        return [types.TextContent(type="text", text=f"Reply sent to {chat_id}")]
    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Channel notification helper
# ---------------------------------------------------------------------------

_next_chat_id = 0


async def push_channel_notification(message_body: str, sender: str = "http-test") -> None:
    """Push a notifications/claude/channel message over the MCP session.

    Uses the official notification format from the Claude Code channels reference:
    - content: string -- becomes the body of the <channel> tag
    - meta: Record<string, string> -- each key becomes a tag attribute
    """
    global _write_stream, _next_chat_id
    if _write_stream is None:
        logger.error("Write stream not available yet -- dropping message")
        return

    _next_chat_id += 1
    chat_id = str(_next_chat_id)

    # Build the notification payload matching the official Claude Code channels spec:
    #   method: "notifications/claude/channel"
    #   params: { content: string, meta: Record<string, string> }
    # The "content" becomes the body of the <channel> tag.
    # The "meta" entries become attributes on the <channel> tag.
    # The "source" attribute is set automatically by Claude Code from the server name.
    notification = types.JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={
            "content": message_body,
            "meta": {
                "chat_id": chat_id,
                "sender": sender,
            },
        },
    )
    session_message = SessionMessage(
        message=types.JSONRPCMessage(notification),
    )
    try:
        await _write_stream.send(session_message)
        logger.info("Pushed channel notification: %s", message_body[:80])
    except Exception:
        logger.exception("Failed to push channel notification")


# ---------------------------------------------------------------------------
# Simple HTTP server (runs in a thread because MCP stdio is on the main loop)
# ---------------------------------------------------------------------------

class ChannelHTTPHandler(BaseHTTPRequestHandler):
    """Handles POST requests and pushes them as channel notifications."""

    def do_POST(self):  # noqa: N802
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
        logger.info("HTTP POST received: %s", body[:200])

        # Schedule the async notification push on the event loop
        if _loop is not None:
            future = asyncio.run_coroutine_threadsafe(
                push_channel_notification(body), _loop
            )
            try:
                future.result(timeout=5)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode())
            except Exception as e:
                logger.exception("Failed to push notification")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "event loop not ready"}).encode())

    def log_message(self, format, *args):  # noqa: A002
        """Redirect HTTP server logs to our logger."""
        logger.debug("HTTP: " + format % args)


def start_http_server(port: int = 8799) -> HTTPServer:
    httpd = HTTPServer(("0.0.0.0", port), ChannelHTTPHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    logger.info("HTTP server listening on port %d", port)
    return httpd


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    global _write_stream, _loop
    _loop = asyncio.get_running_loop()

    # Start the HTTP listener
    httpd = start_http_server(8799)

    try:
        async with stdio_server() as (read_stream, write_stream):
            _write_stream = write_stream

            init_options = server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={"claude/channel": {}},
            )

            logger.info("Starting MCP server over stdio with claude/channel capability")
            logger.info("Capabilities: %s", init_options.capabilities.model_dump_json(indent=2))

            await server.run(read_stream, write_stream, init_options)
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    anyio.run(main)
