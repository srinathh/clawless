"""Custom MCP tools for the clawless agent.

Currently empty — tools can be added here for non-contextual side effects.
The MCP server is registered with the agent so new tools are automatically available.
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server


def build_clawless_mcp_server():
    """Build in-process MCP server with all clawless tools.

    To add a new tool: define it with @tool above, then add it to the list here.
    """
    return create_sdk_mcp_server(
        name="clawless",
        version="1.0.0",
        tools=[],
    )
