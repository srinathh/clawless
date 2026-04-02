# Clawless — Architecture Spec

_You don't need a claw._

## Overview

**Clawless** is a minimal, self-hosted personal AI assistant that connects messaging channels to the Claude Agent SDK, running in Docker. No middleware frameworks — no NanoClaw, no Nanobot, no OpenClaw. The Agent SDK's native features — `ClaudeSDKClient`, sessions, memory, MCP, skills, hooks — replace all orchestration that those frameworks provide.

The thesis: the claw ecosystem exists because the Agent SDK didn't have sessions, memory, or skills when OpenClaw launched. It does now. Clawless is a few lines of Python glue code, a Dockerfile, and your private config. That's it.

## Goals

*   Single or few shared users personal assistant 
*   Pluggable channel architecture — easy to add new messaging channels
    *   WhatsApp (via Twilio) as the first supported channel 
        *   You can use the free tier to test as needed for personal use
        *   I don't plan to integrate Bailey WhatsApp as it allows group messaging & full access which is dangerous
        *   Other channels withofficial
*   Persistent conversation memory across restarts
*   Extensible via Claude Agent SDK skills and MCP servers

## Non-Goals

*   Multi-tenant / multi-user isolation
*   Container-per-conversation sandboxing
*   Provider abstraction (Claude only)
*   Web UI or dashboard

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Docker Container (fixed paths inside)                       │
│                                                              │
│  ┌─────────────┐    ┌──────────────────────────┐            │
│  │  FastAPI     │    │  ClaudeSDKClient          │            │
│  │  (webhooks)  │───▶│  - session management     │            │
│  │  port 8080   │◀───│  - plugins (channels etc) │            │
│  └─────────────┘    │  - skills & agents         │            │
│                      │  - MCP servers             │            │
│                      │  - hooks                   │            │
│                      │  - context compaction      │            │
│                      └──────────┬───────────────┘            │
│                                 │                             │
│  CONTAINER PATHS (fixed) ───────┼──────────────────────────  │
│                                 │                             │
│  /home/appuser/workspace  (rw)  │  agent's cwd (WORKDIR)     │
│    └─ (files the agent creates) │                             │
│                                 │                             │
│  /home/appuser/.claude/   (rw)  │  SDK state + user config   │
│    ├─ .credentials.json   (ro)  │  CLI auth (optional overlay)│
│    └─ projects/                 │  session .jsonl files       │
│       └─ <encoded-cwd>/        │                             │
│                                 │                             │
│  /plugins/                (ro)  │  immutable plugin packages  │
│    ├─ whatsapp-channel/         │  (channels, skills, agents, │
│    └─ my-custom-plugin/         │   hooks, MCP servers)       │
│                                 │                             │
│  /app/                    (ro)  │  open source app code       │
│                                                              │
│  ┌──────────────────────────────────────────────┐            │
│  │  Scheduler (APScheduler, optional)            │            │
│  │  - Daily summaries, reminders, proactive tasks │            │
│  └──────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────┘

HOST PATHS (configurable via .env)
  WORKSPACE_DIR      → /home/appuser/workspace
  USER_CLAUDE_DIR    → /home/appuser/.claude
  PLUGINS_DIR        → /plugins
  CREDENTIALS_FILE   → /home/appuser/.claude/.credentials.json (optional, ro)
```

### Design Principle: Open Source App, Private Config

The open-source repository contains ONLY the generic app code — the FastAPI webhooks, the ClaudeSDKClient wrapper, the Dockerfile. It has zero knowledge of any specific user's persona, skills, MCP servers, or credentials.

All personalization is injected at runtime via Docker bind mounts and plugins. This means:

*   The app repo can be public on GitHub
*   Your personal config, skills, and data never touch the repo
*   Channels (WhatsApp, Telegram, etc.) are plugins — self-contained and immutable
*   Someone else can fork the repo and mount their own config to get their own assistant
*   `docker-compose.yml` and `.env` are PRIVATE (a `docker-compose.sample.yml` and `.env.example` ship in the repo)

---

## Core Components

### 1\. Channel Architecture

Channels are pluggable — each channel implements a common interface for receiving messages, sending replies, and handling media. The core app routes messages from any channel through the same session management and agent pipeline.

**Channel interface (each channel must implement):**

*   Receive incoming messages (text and media)
*   Validate incoming requests (e.g. webhook signatures)
*   Send replies back to the sender
*   Handle channel-specific formatting constraints
*   Handle channel-specific timeouts and async patterns

**WhatsApp via Twilio (first channel):**

*   Incoming text messages
*   Incoming media (images, voice, video, documents)
*   Twilio webhook signature validation (`X-Twilio-Signature`)
*   WhatsApp message status callbacks (delivered, read, failed)
*   Rate limiting / debouncing (WhatsApp can send duplicate webhooks)
*   Responding within Twilio's 15-second timeout (return 200 immediately, process async)

**Key design decision:** Twilio webhooks are stateless HTTP POST. The app must:

1.  Return HTTP 200 to Twilio immediately (or within 15s)
2.  Process the message asynchronously
3.  Send the reply via the Twilio REST API (not TwiML response), because the Agent SDK may take longer than 15s to respond

This means the webhook handler should enqueue the message and a background task should process it and call the Twilio API to send the response.

**Future channels** (contributions welcome): Telegram, Signal, Slack, Discord, SMS, etc.

### 2\. Session Management

Sessions are stored by the SDK at `~/.claude/projects/<encoded-cwd>/*.jsonl`, where `<encoded-cwd>` is the absolute cwd with non-alphanumeric chars replaced by `-`. `ClaudeSDKClient` manages session IDs automatically across multiple `query()` calls.

**Design:**

*   Map each sender (channel + sender ID, e.g. `whatsapp:+1234567890`) to a `ClaudeSDKClient` instance
*   Each client maintains its own session automatically
*   Store the session\_id mapping (from `ResultMessage.session_id`) for resume across restarts
*   On first message from a new sender: create a new client and session
*   On subsequent messages: reuse the client (or resume via `session_id` after restart)
*   Consider session lifecycle: when to fork, when to start fresh, how to handle very long conversations

**Open questions to research:**

*   Can `ClaudeSDKClient` be kept alive as a long-running process, or should it be instantiated per-message?
*   Can multiple concurrent `query()` calls share the same `ClaudeSDKClient` instance, or do we need one client per sender?
*   What is the maximum practical session length before compaction degrades quality?

### 3\. Agent Configuration

Configuration comes from three sources:

**Plugins** (immutable, ro mount at `/plugins/`):

*   Channel implementations (WhatsApp, Telegram, etc.)
*   Curated skills, agents, hooks, MCP servers
*   Loaded via `plugins` parameter in `ClaudeAgentOptions`, paths from `CLAWLESS_PLUGINS` env var

**User config** (`~/.claude/`, rw mount):

*   `CLAUDE.md` — persona and instructions
*   `settings.json` — SDK settings and hooks
*   The agent can also write here (settings, etc.)

**Workspace** (`<cwd>/.claude/`, rw):

*   Skills and agents the agent creates on the fly
*   Loaded via `setting_sources=['project']`

**Example CLAUDE.md** (placed in `~/.claude/CLAUDE.md` in the user-claude-dir):

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

### 4\. MCP Server Configuration

MCP servers can be provided via:

*   **Plugins** — each plugin can bundle its own `.mcp.json` (e.g. a WhatsApp channel plugin bundles its Twilio MCP server)
*   **Workspace** — `.mcp.json` in the workspace root (loaded by `setting_sources=['project']`)

The open-source app has no opinion on which MCP servers you use — this is entirely up to the operator and their plugins.

**Example MCP servers users might configure:**

*   Google Calendar, Google Drive, Gmail
*   Web search (or use the SDK's built-in WebSearch tool)
*   Custom domain-specific tools

### 5\. Media Handling

**Research:** How the Agent SDK handles multimodal input.

**Incoming media from WhatsApp via Twilio:**

*   Twilio provides `MediaUrl0`, `MediaContentType0` etc. in the webhook payload
*   Download media from Twilio's URL (authenticated with account SID/auth token)
*   For images: convert to base64 and pass as image content block in the prompt
*   For voice: transcribe (Whisper API? or let Claude handle it?) then pass as text
*   For documents: save to workspace, reference in prompt

**Outgoing media to WhatsApp:**

*   Agent creates files in workspace → send via Twilio `messages.create()` with `media_url`
*   Need to serve files over HTTPS for Twilio to fetch them
*   Options: pre-signed URLs, temporary file server, or upload to a hosting service

**Open questions:**

*   Does `ClaudeSDKClient.query()` accept multimodal content (image + text) in the prompt parameter?
*   What's the prompt format for passing images/documents to the SDK?
*   Can the SDK's built-in Read tool handle images in the workspace?

### 6\. Response Formatting

Each channel has its own formatting constraints. The formatter is part of the channel implementation.

**WhatsApp formatting constraints:**

*   Max message length: 4096 characters (split longer responses)
*   Supported formatting: `*bold*`, `_italic_`, `~strikethrough~`, `` `code` ``
*   No HTML, no headers, no bullet symbols beyond basic unicode
*   Links are auto-detected

**WhatsApp formatter must:**

*   Strip markdown headers (`##`) and convert to _bold_ lines
*   Convert markdown bullet lists to simple `•` prefixed lines
*   Split responses at natural boundaries if over 4000 chars
*   Preserve code blocks with triple backticks

### 7\. Async Processing & Concurrency

**Critical design pattern:**

```
Webhook POST → enqueue(channel, sender, message) → return 200 immediately
                     │
                     ▼
Background worker → get ClaudeSDKClient for sender
                  → client.query(message, session_id=sender_session)
                  → collect full response
                  → channel.format(response)
                  → channel.send(sender, formatted_response)
```

**Research:**

*   Can `asyncio.Queue` handle this, or do we need something more robust?
*   What happens if two messages arrive from the same sender before the first finishes processing?
*   Should we use a per-sender lock to serialize processing?
*   How does `ClaudeSDKClient` behave under concurrent access?

### 8\. Scheduler (Optional, Phase 2)

For proactive agent behavior:

*   Morning briefing (calendar, reminders)
*   Scheduled content posting reminders
*   Follow-up nudges

**Research:** Can the SDK's `query()` (stateless) be used for scheduled tasks while `ClaudeSDKClient` handles interactive sessions? Or should scheduled tasks also go through the client?

---

## Docker Setup & Mount Strategy

### Design Principles

1.  **API key auth**: Pass `ANTHROPIC_API_KEY` as an environment variable. This is the officially supported authentication method for the Agent SDK. Optionally, CLI credentials can be bind-mounted for subscription-based auth (YMMV — not officially supported by Anthropic for third-party agents).
2.  **Fixed container paths, configurable host paths**: Container paths are fixed (e.g. `/home/appuser/workspace/`, `/home/appuser/.claude/`, `/plugins/`). Host paths are configurable via `.env` variables.
3.  **Plugins for immutable extensions**: Channels, custom skills, agents, hooks, and MCP servers are packaged as plugins and mounted read-only at `/plugins/`. The agent cannot modify them.
4.  **Writable SDK state**: `~/.claude/` is mounted rw so the SDK can write sessions, settings, and anything else it needs. Credentials (if used) are protected via a ro overlay.
5.  **Open source / private separation**: App code ships in the repo. All personalization — plugins, workspace data, credentials — is injected at runtime via bind mounts and env vars.

### Dockerfile

```
FROM python:3.13-slim

# Create non-root user (required for bypassPermissions)
RUN useradd -m -s /bin/bash appuser

# Install Node.js (required by Claude Code CLI) and npm
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (required by the Agent SDK)
RUN npm install -g @anthropic-ai/claude-code

# Install Python dependencies
RUN pip install --no-cache-dir \
    claude-agent-sdk \
    fastapi \
    uvicorn \
    httpx

USER appuser
WORKDIR /home/appuser/workspace

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### .env.example (IN the open-source repo)

```
# Required: Anthropic API key
ANTHROPIC_API_KEY=sk-ant-...

# Host paths (map to fixed container paths)
WORKSPACE_DIR=./data/workspace
USER_CLAUDE_DIR=./data/user-claude
PLUGINS_DIR=./plugins

# Optional: CLI auth instead of API key (YMMV / at your own risk)
# Mount your ~/.claude/.credentials.json as read-only into the container.
# Omit ANTHROPIC_API_KEY above if using this.
# CREDENTIALS_FILE=~/.claude/.credentials.json

# Channel credentials (add as needed per channel plugin)
# TWILIO_ACCOUNT_SID=...
# TWILIO_AUTH_TOKEN=...
# TWILIO_WHATSAPP_FROM=whatsapp:+...
```

### docker-compose.yml (IN the open-source repo)

```
services:
  agent:
    build: .
    ports:
      - "8080:8080"
    env_file: .env
    volumes:

      # ────────────────────────────────────────────────
      # WORKSPACE (rw) — agent's working directory
      # ────────────────────────────────────────────────
      - ${WORKSPACE_DIR}:/home/appuser/workspace:rw

      # ────────────────────────────────────────────────
      # USER CLAUDE DIR (rw) — SDK state (sessions etc)
      # The SDK writes session .jsonl files, settings,
      # and other state here. Persists across restarts.
      # ────────────────────────────────────────────────
      - ${USER_CLAUDE_DIR}:/home/appuser/.claude:rw

      # ────────────────────────────────────────────────
      # PLUGINS (ro) — immutable plugin packages
      # Channels, skills, agents, hooks, MCP servers.
      # Loaded via CLAWLESS_PLUGINS env var.
      # ────────────────────────────────────────────────
      - ${PLUGINS_DIR}:/plugins:ro

      # ────────────────────────────────────────────────
      # APP CODE (ro)
      # ────────────────────────────────────────────────
      - ./app:/app:ro

      # ────────────────────────────────────────────────
      # OPTIONAL: CLI credentials (ro overlay)
      # Uncomment if using CLI auth instead of API key.
      # This mounts read-only ON TOP of the rw parent,
      # protecting the credentials from agent writes.
      # ────────────────────────────────────────────────
      # - ${CREDENTIALS_FILE}:/home/appuser/.claude/.credentials.json:ro

    restart: unless-stopped
```

---

## Host Directory Structure

```
~/
├── clawless/                         # ← git clone, OPEN SOURCE
│   ├── app/
│   │   ├── main.py                   # FastAPI app, startup/shutdown
│   │   ├── agent.py                  # ClaudeSDKClient wrapper, session management
│   │   └── config.py                 # Environment variable loading, constants
│   ├── Dockerfile
│   ├── docker-compose.yml            # uses env vars for host paths
│   ├── .env.example                  # template
│   ├── .gitignore
│   └── README.md
│
├── my-plugins/                       # ← PRIVATE, mounted ro at /plugins/
│   ├── whatsapp-channel/             # channel plugin (MCP server + hooks + skills)
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── skills/
│   │   ├── agents/
│   │   ├── hooks/
│   │   ├── .mcp.json
│   │   └── scripts/
│   └── my-custom-plugin/             # any other plugin
│       └── ...
│
├── my-user-claude/                   # ← PRIVATE, mounted rw at ~/.claude/
│   ├── CLAUDE.md                     # persona / instructions (optional)
│   ├── settings.json                 # SDK settings (optional)
│   └── projects/                     # session .jsonl files (SDK writes these)
│       └── <encoded-cwd>/
│
├── my-workspace/                     # ← PRIVATE, mounted rw at cwd
│   └── (agent creates/modifies files here at runtime)
│
└── .env                              # ← PRIVATE, API key + host paths
```

---

## How setting\_sources and Plugins Interact

The SDK loads configuration from two mechanisms:

### setting\_sources

Controls which filesystem-based settings the SDK discovers. Clawless uses `setting_sources=['project']` to load from the workspace:

| Source | What it loads | Location in container |
| --- | --- | --- |
| `"project"` | `.claude/skills/`, `.claude/agents/`, `.claude/settings.json`, `.claude/rules/*.md`, `CLAUDE.md`, `.mcp.json` | `<cwd>/.claude/` and parent dirs |
| `"user"` | `CLAUDE.md`, `skills/`, `agents/`, `settings.json`, `rules/*.md` | `~/.claude/` |
| `"local"` | `CLAUDE.local.md`, `.claude/settings.local.json` | `<cwd>/` |

### Plugins

Loaded programmatically via the `plugins` parameter in `ClaudeAgentOptions`. Each plugin is a self-contained directory that can include skills, agents, hooks, MCP servers, and channel declarations. Plugins are mounted read-only at `/plugins/` and their paths are configured via the `CLAWLESS_PLUGINS` env var.

### What goes where

| Config type | Where it lives | Mutable? |
| --- | --- | --- |
| Curated skills, agents, hooks, MCP, channels | Plugins (`/plugins/`, ro) | No — immutable |
| User persona (CLAUDE.md), settings | `~/.claude/` (rw) | Yes — user can update |
| On-the-fly skills created by agent | `<cwd>/.claude/` (rw) | Yes — agent creates these |
| Sessions | `~/.claude/projects/<encoded-cwd>/` | Yes — SDK writes these |
| Auth | `ANTHROPIC_API_KEY` env var, or `.credentials.json` (ro overlay) | No |

**Note:** Auto memory (`~/.claude/projects/<project>/memory/`) is a CLI-only feature and is **not loaded by the SDK**.

---

## Implementation Phases

### Phase 1: Core Loop (MVP)

1.  Core orchestrator: FastAPI + ClaudeSDKClient + plugin loading
2.  WhatsApp channel as first plugin (Twilio MCP server + webhook + formatter)
3.  Maps sender to session (1:1 per WhatsApp number), creates ClaudeSDKClient
4.  Passes message to SDK, collects response
5.  Channel plugin formats and sends reply
6.  Text-only, single conversation per sender
7.  Docker container with mount model: workspace (rw), user-claude-dir (rw), plugins (ro)
8.  Plugin paths via `CLAWLESS_PLUGINS` env var

### Phase 2: Media & Additional Plugins

1.  Incoming image/voice/document handling (per-channel plugin)
2.  Outgoing file sharing (per-channel media support)
3.  Additional channel plugins (Telegram, etc.)
4.  MCP server integration via plugins

### Phase 3: Scheduling & Proactive

1.  APScheduler for daily briefings
2.  Reminder system
3.  Content calendar integration
4.  Google Calendar MCP integration

### Phase 4: Hardening

1.  Health checks and monitoring
2.  Graceful container restart / session recovery
3.  Cost tracking and budget alerts
4.  Backup strategy for user-claude-dir (sessions) and workspace

---

## Key Research Tasks

Before implementing, research and document findings on each of these. For SDK internals, read the source code at `github.com/anthropics/claude-agent-sdk-python`.

### SDK Client & Sessions

**ClaudeSDKClient lifecycle**: Can it be a long-running singleton? Or instantiate per-request? What are the resource implications? Does it hold a subprocess open?

**Concurrency model**: Can one `ClaudeSDKClient` instance handle multiple concurrent sessions (via different `session_id` values)? Or do we need one client per active conversation? What about concurrent `query()` calls on the same session?

**Session persistence across restarts**: Test that sessions created inside the container (written to `~/.claude/projects/<encoded-cwd>/`) survive container restart when the projects directory is a bind mount. The encoded-cwd uses the container-internal path with non-alphanumeric chars replaced by `-`.

**Session lifecycle**: What is the maximum practical session length before compaction degrades quality? When should we fork vs start fresh? How does `continue_conversation=True` differ from explicit `session_id` resume?

### Docker Mounts

**Credentials ro overlay**: Test that mounting `~/.claude/` as rw and then `~/.claude/.credentials.json` as ro on top works correctly — the SDK can write sessions/state but cannot modify the credentials file.

### Multimodal & Media

**Multimodal input via ClaudeSDKClient**: Confirm the exact format for passing images and documents as content blocks through `client.query()`. The `query()` function accepts `str | AsyncIterable[dict]` — what does the dict look like for image + text?

**Media from Twilio**: Document the Twilio webhook payload fields for media (MediaUrl0, MediaContentType0, NumMedia). How to download media from Twilio URLs (requires account auth).

### Tools & MCP

**MCP server process lifecycle**: When `.mcp.json` configures a stdio MCP server, does the SDK start/stop the process? Does it start on ClaudeSDKClient init and persist, or start per-query? What happens on process crash?

### Channel Integration

**Twilio async response pattern**: Verify the best practice for responding to WhatsApp via Twilio REST API (not TwiML) when processing takes >15s.

**WhatsApp message formatting**: Research the exact WhatsApp formatting syntax and character limits (4096 chars). Build the formatter module that converts Claude's markdown output to WhatsApp-compatible formatting.

**Twilio media serving**: For outgoing files (agent creates a file and wants to send it), Twilio needs a publicly accessible HTTPS URL. Research options: serve from the container with a temporary URL, upload to a file host, use Twilio's content API.

### Operations

**Cost tracking**: The SDK exposes `total_cost_usd` and `usage` on `ResultMessage`. Design a simple cost tracking approach (log file? SQLite table?).

**Error handling**: What happens when the SDK hits rate limits, context overflow, or API errors? How should the bridge handle these gracefully? Research the SDK's error types and design retry/fallback behavior. What error message should the user see on WhatsApp when something goes wrong?