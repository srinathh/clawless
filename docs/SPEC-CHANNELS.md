# Clawless — Architecture Spec (Channels Edition)

_You don't need a claw._

## Overview

**Clawless** is a minimal, self-hosted personal AI assistant built on Claude Code's native channel architecture, running in Docker. No middleware frameworks, no custom webhook handlers, no orchestration glue. Claude Code's built-in features — channels, sessions, skills, agents, hooks, MCP, context compaction — do all the work.

The thesis: the claw ecosystem exists because Claude Code didn't have channels, sessions, or skills when OpenClaw launched. It does now. Clawless is a Dockerfile, a channel plugin, and your private config. That's it.

## Goals

*   Single or few shared users personal assistant
*   Channel plugins for messaging — each channel is a self-contained MCP server
    *   WhatsApp (via Twilio) as the first custom channel (Python, using MCP Python SDK)
    *   Official channel plugins (Telegram, Discord, iMessage) included via git submodule from `anthropics/claude-plugins-official`
*   Persistent conversation sessions across container restarts
*   Extensible via plugins (skills, agents, hooks, MCP servers)
*   Open source app + private config separation

## Non-Goals

*   Multi-tenant / multi-user isolation
*   Container-per-conversation sandboxing
*   Provider abstraction (Claude only)
*   Web UI or dashboard

---

## Architecture

Clawless runs Claude Code inside Docker with channel plugins that bridge messaging platforms. Channels push inbound messages into Claude's session; Claude responds via reply tools exposed by the channel.

```
┌──────────────────────────────────────────────────────────────┐
│  Docker Container                                            │
│                                                              │
│  ┌──────────────────────────────────────────────────┐       │
│  │  Claude Code CLI                                   │       │
│  │  (long-running process, manages the agent loop)    │       │
│  │                                                    │       │
│  │  ┌────────────────────────────────────────────┐   │       │
│  │  │  Channel Plugins (MCP servers, stdio)       │   │       │
│  │  │  ┌──────────┐ ┌──────────┐ ┌──────────┐   │   │       │
│  │  │  │ WhatsApp  │ │ Telegram │ │   ...    │   │   │       │
│  │  │  │ (Twilio)  │ │ (grammy) │ │          │   │   │       │
│  │  │  │ HTTP:8788 │ │ polling  │ │          │   │   │       │
│  │  │  └────┬─────┘ └────┬─────┘ └────┬─────┘   │   │       │
│  │  │       │push         │push        │push      │   │       │
│  │  │       ▼             ▼            ▼          │   │       │
│  │  │  notifications/claude/channel ──────────▶ Claude │       │
│  │  │                                              │   │       │
│  │  │  Claude ──▶ reply tool ──▶ send via platform │   │       │
│  │  └────────────────────────────────────────────┘   │       │
│  │                                                    │       │
│  │  Sessions, skills, agents, hooks, MCP, compaction  │       │
│  └──────────────────────────────────────────────────┘       │
│                                                              │
│  CONTAINER PATHS (fixed) ────────────────────────────────── │
│                                                              │
│  /home/appuser/workspace/          (rw)  agent's cwd         │
│  /home/appuser/.claude/            (rw)  SDK state + config  │
│  /home/appuser/.claude/            (ro)  .credentials.json   │
│             └─ .credentials.json         (overlay)           │
│  /plugins/                         (ro)  channel + other     │
│                                          plugins             │
│                                                              │
│  HOST PATHS (configurable via .env) ─────────────────────── │
│                                                              │
│  WORKSPACE_DIR     → /home/appuser/workspace                 │
│  USER_CLAUDE_DIR   → /home/appuser/.claude                   │
│  PLUGINS_DIR       → /plugins                                │
│  ~/.claude/.credentials.json → .credentials.json (ro)        │
└──────────────────────────────────────────────────────────────┘
```

### How Channels Work

A channel is an MCP server that declares the `claude/channel` capability. Claude Code spawns it as a subprocess over stdio. The channel:

1.  **Receives** messages from the external platform (HTTP webhook, API polling, etc.)
2.  **Pushes** them into Claude's session via `notifications/claude/channel`
3.  **Exposes** a reply tool (standard MCP tool) that Claude calls to send responses back
4.  **Optionally** relays permission prompts so you can approve/deny tool use from your phone

Messages arrive in Claude's context as `<channel source="whatsapp" chat_id="...">` tags. Claude responds by calling the channel's reply tool with the `chat_id` and message text.

### Design Principle: Open Source App, Private Config

The open-source repository contains:

*   The Dockerfile
*   The WhatsApp channel plugin (and any other public channel plugins)
*   Public skills, agents, hooks
*   `docker-compose.yml` (uses env vars for host paths)
*   `.env.example`

All personalization is injected at runtime:

*   `~/.claude/.credentials.json` — your Claude subscription auth
*   User Claude dir — your CLAUDE.md, settings, private skills/agents
*   Workspace — agent's working directory
*   Private plugins — any plugins you don't want in the repo

---

## Core Components

### 1\. WhatsApp Channel Plugin

A self-contained MCP server (Python, using the `mcp` Python SDK) following the same pattern as the official Telegram plugin. It bridges Twilio WhatsApp and Claude Code.

The MCP protocol is language-agnostic (JSON-RPC over stdio). The official plugins use TypeScript/Bun, but the `command` field in `.mcp.json` can point to any executable. A Python channel uses `python` (or `uv run`) instead of `bun`.

**Structure:**

```
whatsapp-channel/
├── .claude-plugin/
│   └── plugin.json
├── .mcp.json                  # {"mcpServers": {"whatsapp": {"command": "uv", "args": ["run", ...]}}}
├── pyproject.toml             # deps: mcp, twilio, starlette, uvicorn
├── server.py                  # MCP server: webhook listener + reply tool
└── skills/
    └── whatsapp-access/
        └── SKILL.md           # manage allowlist, pairing
```

**Inbound (Twilio → Claude):**

*   `server.py` starts an HTTP listener on a configurable port (e.g. 8788)
*   Twilio sends WhatsApp webhook POSTs to this port
*   The server validates the Twilio signature (`X-Twilio-Signature`)
*   Gates on sender allowlist (phone number)
*   Pushes the message into Claude via `mcp.notification()`:

```
await mcp.notification({
  method: 'notifications/claude/channel',
  params: {
    content: messageBody,
    meta: { chat_id: fromNumber, sender_name: profileName },
  },
})
```

*   Claude sees: `<channel source="whatsapp" chat_id="+1234567890" sender_name="Alice">Hey, can you check my calendar?</channel>`

**Outbound (Claude → Twilio):**

*   Exposes a `reply` tool and optionally `sendMedia`:

```
tools: [{
  name: 'reply',
  inputSchema: {
    type: 'object',
    properties: {
      chat_id: { type: 'string', description: 'Phone number to reply to' },
      text: { type: 'string', description: 'Message to send' },
    },
    required: ['chat_id', 'text'],
  },
}]
```

*   When Claude calls `reply`, the handler sends via Twilio REST API
*   WhatsApp formatting: the server (or a hook) converts Claude's markdown to WhatsApp format (`*bold*`, `_italic_`, `•` bullets, split at 4096 chars)

**Media handling:**

The channel protocol is text-only (`content` is a string), so media is handled by the plugin: download to disk, pass the file path in `meta`, and Claude reads it with its built-in tools (Read handles images natively since Claude is multimodal).

*   Incoming media: Twilio webhook includes `MediaUrl0`, `MediaContentType0` → plugin downloads the file (authenticated with account SID/auth token) → saves to workspace (e.g. `workspace/media/`) → pushes notification with file path in `meta`:

```python
await server.notification(
    method="notifications/claude/channel",
    params={
        "content": caption or "Sent you a photo",
        "meta": {
            "chat_id": from_number,
            "media_file": "/home/appuser/workspace/media/img_001.jpg",
            "media_type": "image/jpeg",
        },
    },
)
```

*   Outgoing media: Claude creates files in the workspace → the `reply` tool (or a `sendMedia` tool) reads the file, uploads it, and sends via Twilio `messages.create()` with `media_url`

The nanobot reference implementation on [`feature/twilio-whatsapp-nightly`](https://github.com/srinathh/nanobot/tree/feature/twilio-whatsapp-nightly) already handles both incoming media download and outgoing media upload correctly. The channel plugin ports this into the MCP channel protocol.

**Permission relay (optional):**

*   Declare `claude/channel/permission` capability
*   Forward tool approval prompts to WhatsApp
*   Parse `yes <id>` / `no <id>` replies as verdicts
*   Approve Bash commands, file writes, etc. from your phone

**Key differences from Telegram plugin:**

|   | Telegram (official, TS/Bun) | WhatsApp (clawless, Python) |
| --- | --- | --- |
| **Language** | TypeScript / Bun | Python / uv |
| **Inbound** | Polls Telegram Bot API | Listens for HTTP webhooks from Twilio |
| **Auth** | Bot token | Account SID + Auth Token |
| **Webhook validation** | N/A (polling) | `X-Twilio-Signature` HMAC |
| **Message limits** | 4096 chars | 4096 chars |
| **Media** | Telegram File API | Twilio Media URLs |
| **Formatting** | Markdown subset | WhatsApp-specific (`*bold*`, `_italic_`) |
| **MCP SDK** | `@modelcontextprotocol/sdk` (npm) | `mcp` (PyPI) |

**Reference implementation:** The WhatsApp/Twilio bridge in [github.com/srinathh/nanobot](https://github.com/srinathh/nanobot/tree/feature/twilio-whatsapp-nightly) (branch `feature/twilio-whatsapp-nightly`) already handles text, images, voice, video, documents, and Twilio webhook validation correctly. The channel plugin should port this existing implementation into the MCP channel protocol rather than starting from scratch.

### 2\. Session Management

Claude Code manages sessions natively. Sessions are stored at `~/.claude/projects/<encoded-cwd>/*.jsonl`.

*   Each sender maps to a conversation within the same Claude session
*   `chat_id` in the channel meta identifies the sender
*   Claude maintains context across messages from the same sender
*   Context compaction handles long conversations automatically
*   Sessions persist across container restarts (via the user-claude-dir mount)

**Open questions:**

*   With multiple senders messaging concurrently, how does Claude Code handle interleaved channel events?
*   Should each sender get a separate session (via forking), or should all messages go to one session?
*   How does `--channels` interact with concurrent messages?

### 3\. Agent Configuration

**CLAUDE.md** — placed in the user-claude-dir (`~/.claude/CLAUDE.md`):

```
# Personal Assistant

You are a personal AI assistant.

## Context
- [Your personal context here]

## Capabilities
- Content creation
- Research and web search
- File operations in the workspace
- Scheduling reminders
- Travel planning

## Communication Style
- Warm, direct, concise
- Responds via WhatsApp — keep messages readable on mobile
- Use formatting sparingly (WhatsApp supports *bold* and _italic_)
- Break long responses into multiple messages if needed
```

**Skills, agents, hooks** — three sources:

| Source | Location | Mutable? |
| --- | --- | --- |
| Plugins (`/plugins/`, ro) | Channel implementations, curated skills/agents/hooks/MCP | No |
| User config (`~/.claude/`, rw) | Personal CLAUDE.md, settings.json | Yes |
| Workspace (`<cwd>/.claude/`, rw) | Skills/agents the agent creates on the fly | Yes |

### 4\. MCP Server Configuration

MCP servers can be provided via:

*   **Plugins** — each plugin can bundle its own `.mcp.json`
*   **Workspace** — `.mcp.json` in the workspace root

**Example MCP servers users might configure:**

*   Google Calendar, Google Drive, Gmail
*   Web search (or use the built-in WebSearch tool)
*   Custom domain-specific tools

---

## Known Limitations

### Shared session across senders

In the channels model, all messages from all senders go into **one Claude Code session**. Claude distinguishes senders via `chat_id` in the `<channel>` tag, but the conversation history is shared — Claude has full context of everyone's messages.

For a personal assistant with 1-2 trusted users, this is a **feature**: Claude knows both contexts, can cross-reference ("you mentioned X earlier"), and it's like a family group chat with Claude.

If per-sender session isolation is needed in the future, options include:
*   Multiple container instances (one per user)
*   Hybrid approach using the Agent SDK with per-sender `ClaudeSDKClient` sessions
*   Future channel protocol support for session routing (channels are in research preview)

### Channels research preview

Channels require Claude Code v2.1.80+ and are in research preview. Custom channels (like the WhatsApp plugin) require `--dangerously-load-development-channels`. This is fine for personal self-hosted use. To remove the flag, submit the plugin to the official marketplace for security review.

---

## Docker Setup

### Design Principles

1.  **CLI subscription auth**: Mount `~/.claude/.credentials.json` read-only. This is required for channels.
2.  **Fixed container paths, configurable host paths**: Container paths are fixed. Host paths are configured via `.env`.
3.  **Plugins for immutable extensions**: Channels and curated skills/agents/hooks mount read-only at `/plugins/`.
4.  **Writable SDK state**: `~/.claude/` is rw for sessions and runtime state. Credentials are protected via ro overlay.
5.  **No hardcoded paths in app code**: The Dockerfile sets `WORKDIR`; the operator can override in compose.

### Dockerfile

```
FROM python:3.13-slim

# Create non-root user (required for bypassPermissions)
RUN useradd -m -s /bin/bash appuser

# Install Node.js + npm (required by Claude Code CLI)
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Bun (runtime for official TS channel plugins: Telegram, Discord, etc.)
RUN npm install -g bun

# Install uv (for Python channel plugins)
RUN pip install --no-cache-dir uv

USER appuser
WORKDIR /home/appuser/workspace

# CHANNELS env var controls which channels to enable (set in .env)
# Claude Code runs as the main process
CMD ["claude", "--channels", "${CHANNELS}", "--dangerously-load-development-channels", "${CHANNELS}"]
```

**Note:** The `--dangerously-load-development-channels` flag is required during the channels research preview for custom (non-allowlisted) channels. Once channels are approved upstream, this flag can be removed. The official Telegram/Discord plugins may already be on the allowlist.

### .env.example

```
# Host paths (map to fixed container paths)
WORKSPACE_DIR=./data/workspace
USER_CLAUDE_DIR=./data/user-claude

# Channels to enable (comma-separated plugin references)
# Custom plugins from /plugins/, official plugins from /plugins/vendor/
CHANNELS=plugin:whatsapp-channel

# Channel credentials are stored in USER_CLAUDE_DIR/channels/<name>/.env
# not here — see the channel plugin docs for setup instructions.
# e.g. USER_CLAUDE_DIR/channels/whatsapp/.env:
#   TWILIO_ACCOUNT_SID=AC...
#   TWILIO_AUTH_TOKEN=...
#   TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
# e.g. USER_CLAUDE_DIR/channels/telegram/.env:
#   TELEGRAM_BOT_TOKEN=123456789:AAH...
```

### docker-compose.yml

```
services:
  agent:
    build: .
    ports:
      - "8788:8788"    # WhatsApp channel webhook port (Twilio POSTs here)
    env_file: .env
    volumes:

      # ────────────────────────────────────────────────
      # WORKSPACE (rw) — agent's working directory
      # ────────────────────────────────────────────────
      - ${WORKSPACE_DIR}:/home/appuser/workspace:rw

      # ────────────────────────────────────────────────
      # USER CLAUDE DIR (rw) — SDK state, sessions,
      # user CLAUDE.md, settings, channel config
      # ────────────────────────────────────────────────
      - ${USER_CLAUDE_DIR}:/home/appuser/.claude:rw

      # ────────────────────────────────────────────────
      # CREDENTIALS (ro overlay) — CLI subscription auth
      # Required for channels. Mounted ro on top of rw
      # parent to protect from agent writes.
      # ────────────────────────────────────────────────
      - ~/.claude/.credentials.json:/home/appuser/.claude/.credentials.json:ro

      # ────────────────────────────────────────────────
      # PLUGINS (ro) — all plugins in one mount
      # Custom plugins (e.g. whatsapp-channel/) and
      # official plugins (vendor/) from git submodule.
      # ────────────────────────────────────────────────
      - ./plugins:/plugins:ro

    restart: unless-stopped
```

---

## Host Directory Structure

```
~/
├── clawless/                              # ← git clone, OPEN SOURCE
│   ├── plugins/
│   │   ├── whatsapp-channel/              # custom WhatsApp plugin (Python)
│   │   │   ├── .claude-plugin/
│   │   │   │   └── plugin.json
│   │   │   ├── .mcp.json
│   │   │   ├── pyproject.toml
│   │   │   ├── server.py
│   │   │   └── skills/
│   │   │       └── whatsapp-access/
│   │   │           └── SKILL.md
│   │   └── vendor/                        # ← git submodule: anthropics/claude-plugins-official
│   │       └── external_plugins/
│   │           ├── telegram/              # official Telegram plugin (TS/Bun)
│   │           ├── discord/               # official Discord plugin (TS/Bun)
│   │           ├── imessage/              # official iMessage plugin
│   │           ├── slack/                 # official Slack plugin
│   │           └── fakechat/              # official test chat UI
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── .gitignore
│   ├── .gitmodules
│   └── README.md
│
├── my-user-claude/                        # ← PRIVATE, mounted rw at ~/.claude/
│   ├── CLAUDE.md                          # persona / instructions
│   ├── settings.json                      # SDK settings (optional)
│   ├── channels/                          # channel-specific config + secrets
│   │   ├── whatsapp/
│   │   │   └── .env                       # TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, etc.
│   │   └── telegram/
│   │       └── .env                       # TELEGRAM_BOT_TOKEN=...
│   └── projects/                          # session .jsonl files (auto-created)
│       └── <encoded-cwd>/
│
├── my-workspace/                          # ← PRIVATE, mounted rw at cwd
│   └── (agent creates/modifies files here at runtime)
│
└── .env                                   # ← PRIVATE, host paths + CHANNELS list
```

### Git Submodule Setup

The official channel plugins are included via git submodule from `anthropics/claude-plugins-official` (Apache-2.0 licensed):

```
# Add the submodule (one-time)
git submodule add https://github.com/anthropics/claude-plugins-official.git plugins/vendor

# Clone with submodules
git clone --recurse-submodules https://github.com/<you>/clawless.git

# Update official plugins to latest
git submodule update --remote plugins/vendor
```

The submodule brings all official plugins. The `plugins/` directory is mounted ro at `/plugins/` in the container, giving Claude Code access to both custom (whatsapp-channel) and official (telegram, discord, etc.) plugins.

To enable a channel, add it to `CHANNELS` in `.env`:

```
# Just WhatsApp
CHANNELS=plugin:whatsapp-channel

# WhatsApp + Telegram
CHANNELS=plugin:whatsapp-channel,plugin:vendor/external_plugins/telegram

# WhatsApp + Telegram + Discord
CHANNELS=plugin:whatsapp-channel,plugin:vendor/external_plugins/telegram,plugin:vendor/external_plugins/discord
```

Each channel's credentials go in the user-claude-dir under `channels/<name>/.env`, following the same pattern as the official plugins.

---

## Implementation Phases

### Phase 1: Core Loop (MVP)

1.  WhatsApp channel plugin in Python (MCP server with Twilio webhook + reply tool)
2.  Dockerfile running Claude Code CLI with `--channels`
3.  Docker compose with mount model: workspace (rw), user-claude-dir (rw), credentials (ro), plugins (ro)
4.  Git submodule for official plugins (`anthropics/claude-plugins-official`)
5.  Port existing Twilio bridge from [srinathh/nanobot](https://github.com/srinathh/nanobot) (branch `feature/claude-agent-engine`) into MCP channel protocol
6.  Text + images + voice + documents (already handled in nanobot bridge)
7.  Sender allowlist (gate on phone number)
8.  WhatsApp response formatting (markdown → WhatsApp)

### Phase 2: Permission Relay & Access Management

1.  Permission relay — approve/deny tool use from WhatsApp
2.  Access management skill (`/whatsapp-channel:access`)

### Phase 3: Additional Channels & Scheduling

1.  Enable official Telegram/Discord plugins via submodule (just add to CHANNELS env var)
2.  Scheduler for proactive behavior (daily briefings, reminders)
3.  Google Calendar MCP integration

### Phase 4: Hardening

1.  Health checks and monitoring
2.  Graceful container restart / session recovery
3.  Cost tracking
4.  Backup strategy for user-claude-dir and workspace
5.  Submit WhatsApp channel to official plugin marketplace

---

## Key Research Tasks

### Channels & SDK

**Channels in Docker**: Test that Claude Code CLI with `--channels` works inside Docker. Verify the channel MCP server (subprocess) can bind to a port and receive external traffic.

**Concurrent messages**: How does Claude Code handle multiple channel events arriving concurrently? Does it queue them? Process in parallel? What if two WhatsApp senders message at the same time?

**Session model with channels**: Does Claude Code use one session per channel, or one global session? How does context from different senders interact? Should we fork sessions per sender?

**Channel + plugin loading**: Verify that `--channels plugin:whatsapp-channel` works when the plugin is at a bind-mounted path (`/plugins/whatsapp-channel`).

### WhatsApp / Twilio

**Twilio webhook to channel**: Verify the pattern: Twilio POSTs to `localhost:8788` inside the container, channel MCP server receives it and pushes to Claude. Port must be exposed in Docker.

**Twilio signature validation**: Implement `X-Twilio-Signature` HMAC validation in the channel server. The webhook URL used for validation must match what Twilio sends to.

**WhatsApp formatting**: Build the markdown → WhatsApp converter. Handle `*bold*`, `_italic_`, `~strikethrough~`, code blocks, bullet lists, and message splitting at 4096 chars.

**Media serving**: For outgoing files, Twilio needs a publicly accessible HTTPS URL. Options: serve from the container with a temporary URL, upload to a file host, use Twilio's content API.

### Docker & Auth

**Credentials ro overlay**: Test that mounting `~/.claude/` as rw and `~/.claude/.credentials.json` as ro on top works correctly.

**Non-root + bypassPermissions**: Verify `bypassPermissions` works with the `appuser` non-root user in Docker.

### Operations

**Cost tracking**: Claude Code tracks costs per session. Research how to extract and log costs.

**Error handling**: What happens when the channel MCP server crashes? Does Claude Code restart it? How should the WhatsApp channel handle Twilio errors, rate limits, or Claude API errors?