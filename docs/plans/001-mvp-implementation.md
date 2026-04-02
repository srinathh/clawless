# Clawless MVP Implementation Plan

## Context

Clawless is a self-hosted personal AI assistant built on Claude Code's native channel architecture. The core idea: Claude Code CLI runs inside Docker, channel plugins bridge messaging platforms (WhatsApp first), and all personalization is injected via bind mounts. See [SPEC-CHANNELS.md](../SPEC-CHANNELS.md) for the full architecture.

The MVP goal is: **send a WhatsApp message, get a Claude response back**, running in Docker with persistent sessions.

## Prerequisites

Before implementation begins, several unknowns need to be resolved through hands-on testing. These are blocking — the results may change the implementation approach.

### Research Spike 1: Channels in Docker

**Goal:** Verify Claude Code CLI with `--channels` works inside a Docker container.

**Steps:**
1. Build a minimal Docker image with Claude Code CLI + Bun installed
2. Mount `~/.claude/.credentials.json` (ro overlay) and a rw `~/.claude/` volume
3. Mount the official `fakechat` plugin from `anthropics/claude-plugins-official`
4. Run `claude --channels plugin:fakechat --dangerously-load-development-channels plugin:fakechat`
5. Verify the fakechat web UI is reachable from the host (port forwarding)
6. Send a message through fakechat and confirm Claude responds

**What we learn:**
- Does CLI auth via `.credentials.json` work inside Docker?
- Does the ro overlay on `.credentials.json` with rw parent work?
- Can a channel plugin MCP server bind to a port and receive external traffic?
- Does `--dangerously-load-development-channels` work non-interactively (Docker has no TTY for the confirmation prompt)?
- What does Claude Code CLI output look like when running headless in Docker?

**Risks:**
- The `--dangerously-load-development-channels` flag may require interactive confirmation (TTY)
- `.credentials.json` may not be the only auth file needed (may also need `statsig/`, `.auth.json`, etc.)
- Claude Code CLI may not run headless — it may expect a terminal

### Research Spike 2: Python MCP Channel

**Goal:** Verify the Python MCP SDK (`mcp` package) can implement a channel that Claude Code recognizes.

**Steps:**
1. Install the `mcp` Python package
2. Write a minimal Python channel server that declares `claude/channel` capability and pushes a test notification
3. Create a `.mcp.json` pointing to `python server.py`
4. Test with `claude --channels server:test-channel --dangerously-load-development-channels server:test-channel`
5. Verify Claude receives the `<channel>` tag

**What we learn:**
- Does the Python MCP SDK support the `claude/channel` experimental capability?
- Is the notification format identical to the TS SDK?
- Any Python-specific quirks with stdio transport?

**Risks:**
- The Python MCP SDK may not support `experimental` capabilities — the channel docs only show TypeScript examples
- If Python doesn't work, fallback is writing the WhatsApp channel in TypeScript (using the Telegram plugin as template)

### Research Spike 3: Plugin Loading from Bind Mount

**Goal:** Verify `--channels plugin:<name>` works when the plugin directory is at a bind-mounted path.

**Steps:**
1. Create a trivial plugin (plugin.json + .mcp.json + server)
2. Mount it ro at `/plugins/test-plugin` in Docker
3. Run `claude --channels plugin:test-plugin`
4. Verify the plugin loads and channel events work

**What we learn:**
- Does Claude Code discover plugins at arbitrary filesystem paths?
- Does the ro mount cause issues (e.g., plugin tries to write to `${CLAUDE_PLUGIN_DATA}`)?

---

## Implementation Steps

The steps below assume the research spikes have been completed and all three work as expected. If any spike fails, the plan will need revision (noted as contingencies below).

### Step 1: Repository Setup

**Files to create/modify:**

1. **Add git submodule** for official plugins:
   ```bash
   git submodule add https://github.com/anthropics/claude-plugins-official.git plugins/vendor
   ```

2. **Create `plugins/whatsapp-channel/` directory** with initial structure:
   ```
   plugins/whatsapp-channel/
   ├── .claude-plugin/
   │   └── plugin.json
   ├── .mcp.json
   ├── pyproject.toml
   └── server.py
   ```

3. **Create `.env.example`**:
   ```
   WORKSPACE_DIR=./data/workspace
   USER_CLAUDE_DIR=./data/user-claude
   CHANNELS=plugin:whatsapp-channel
   ```

4. **Create `Dockerfile`** per spec (Python 3.13 + Node.js + Claude Code CLI + Bun + uv)

5. **Create `docker-compose.yml`** per spec (5 mounts: workspace rw, user-claude-dir rw, credentials ro overlay, plugins ro, app ro — but note: for channels architecture there may not be an /app mount since Claude Code CLI is the process, not a custom app)

6. **Update `.gitignore`** — add:
   ```
   .env
   data/
   ```

**Dependencies:** None. Can start immediately.

### Step 2: WhatsApp Channel Plugin — Scaffold

**File: `plugins/whatsapp-channel/plugin.json`**
```json
{
  "name": "whatsapp-channel",
  "description": "WhatsApp channel for Claude Code via Twilio — messaging bridge with sender allowlist.",
  "version": "0.1.0",
  "keywords": ["whatsapp", "twilio", "messaging", "channel", "mcp"]
}
```

**File: `plugins/whatsapp-channel/.mcp.json`**
```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "uv",
      "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "server.py"]
    }
  }
}
```

**File: `plugins/whatsapp-channel/pyproject.toml`**
```toml
[project]
name = "clawless-whatsapp-channel"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "mcp",
    "twilio",
    "httpx",
    "starlette",
    "uvicorn",
]
```

**Dependencies:** Step 1.

### Step 3: WhatsApp Channel Plugin — MCP Server Core

**File: `plugins/whatsapp-channel/server.py`**

Port the nanobot Twilio bridge ([`srinathh/nanobot` branch `feature/twilio-whatsapp-nightly`](https://github.com/srinathh/nanobot/tree/feature/twilio-whatsapp-nightly), file `nanobot/channels/twilio_whatsapp.py`, 334 lines) into the MCP channel protocol.

**What to keep from nanobot:**
- `_handle_webhook()` — Twilio webhook parsing, signature validation, media URL extraction
- `_download_media()` — authenticated media download from Twilio URLs via httpx
- `send()` / `_stage_media()` / `_serve_media()` — outbound message sending, media file staging and serving
- `TwilioWhatsAppConfig` — config schema (adapt to read from `~/.claude/channels/whatsapp/.env`)
- Message splitting logic (1600 char Twilio limit)

**What to replace:**
- `BaseChannel` / `MessageBus` → MCP `Server` with `claude/channel` capability
- `_handle_message()` → `mcp.notification()` with `notifications/claude/channel`
- `send()` called by bus → MCP `reply` tool handler called by Claude
- aiohttp web server → starlette/uvicorn (or keep aiohttp — either works)
- nanobot config loading → read from `~/.claude/channels/whatsapp/.env`

**Server structure (pseudocode):**
```python
# 1. Create MCP server with channel capability
server = Server("whatsapp", capabilities={"experimental": {"claude/channel": {}}})
server.instructions = "Messages arrive as <channel source='whatsapp' ...>. Reply with the reply tool."

# 2. Register reply tool
@server.tool("reply")
async def reply(chat_id: str, text: str) -> str:
    # Split text at 1600 chars, send via Twilio REST API
    ...

# 3. Register sendMedia tool
@server.tool("sendMedia")
async def send_media(chat_id: str, file_path: str, caption: str = "") -> str:
    # Stage file, get public URL, send via Twilio with media_url
    ...

# 4. Connect MCP over stdio
async with stdio_server() as (read, write):
    await server.run(read, write, ...)

# 5. Start HTTP server for Twilio webhooks (in parallel)
# On POST: validate signature, gate sender, download media, push notification
```

**Dependencies:** Step 2 + Research Spike 2 (confirm Python MCP channels work).

### Step 4: Sender Allowlist

**Approach:** Follow the Telegram plugin pattern — store allowlist in `~/.claude/channels/whatsapp/access.json`.

```json
{
  "allowed_senders": ["+1234567890", "+0987654321"],
  "mode": "static"
}
```

The webhook handler checks the sender against this list before pushing to Claude. Unknown senders are silently dropped.

**Optional skill:** Create `plugins/whatsapp-channel/skills/whatsapp-access/SKILL.md` for managing the allowlist from within Claude (add/remove numbers). This can be deferred to Phase 2.

**Dependencies:** Step 3.

### Step 5: WhatsApp Response Formatting

**Function:** Convert Claude's markdown output to WhatsApp-compatible formatting.

**Rules:**
- `**bold**` → `*bold*`
- `_italic_` → stays as-is (WhatsApp uses same syntax)
- `## Header` → `*Header*` (bold line)
- `- item` → `• item`
- Code blocks → preserve triple backticks (WhatsApp renders them)
- Strip HTML
- Split at 1600 chars (Twilio limit) at natural boundaries (paragraph, sentence)

**Location:** Utility function in `server.py` (or separate `formatter.py` in the plugin directory). Called by the `reply` tool handler before sending via Twilio.

**Port from nanobot:** Check if `nanobot/utils/helpers.py` `split_message()` has formatting logic, or if it's just splitting.

**Dependencies:** Step 3.

### Step 6: Dockerfile and Docker Compose

**Dockerfile** (per spec):
```dockerfile
FROM python:3.13-slim
RUN useradd -m -s /bin/bash appuser
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code bun
RUN pip install --no-cache-dir uv
USER appuser
WORKDIR /home/appuser/workspace
```

**CMD:** This needs careful thought. The `--channels` flag takes plugin references. We need to:
1. Parse the `CHANNELS` env var
2. Pass each channel to `--channels` and `--dangerously-load-development-channels`

Options:
- **Entrypoint script** (`entrypoint.sh`) that builds the claude command from env vars
- **Direct CMD** with shell expansion: `CMD ["sh", "-c", "claude --channels $CHANNELS --dangerously-load-development-channels $CHANNELS"]`

The entrypoint script is cleaner for handling multiple channels, health checks, and graceful shutdown.

**Docker Compose** (per spec):
- 4 volume mounts: workspace (rw), user-claude-dir (rw), credentials.json (ro overlay), plugins (ro)
- Port 8788 exposed (WhatsApp webhook)
- env_file: .env

**Dependencies:** Step 1 + Research Spike 1.

### Step 7: Integration Test

**Manual end-to-end test:**

1. Set up Twilio WhatsApp Sandbox (free tier) or use existing Twilio account
2. Configure ngrok or similar to expose port 8788 publicly
3. Set Twilio webhook URL to `https://<ngrok-url>/twilio/whatsapp`
4. Create `data/user-claude/` with:
   - `CLAUDE.md` (basic persona)
   - `channels/whatsapp/.env` (Twilio credentials)
5. Copy `~/.claude/.credentials.json` (Claude subscription auth)
6. Create `data/workspace/` directory
7. Create `.env` with host paths
8. Run `docker compose up`
9. Send a WhatsApp message to the Twilio sandbox number
10. Verify Claude responds

**Success criteria:**
- Message received by channel plugin
- Pushed to Claude via channel notification
- Claude responds via reply tool
- Response arrives on WhatsApp
- Session persists after `docker compose down && docker compose up`

**Dependencies:** All previous steps.

---

## Contingencies

### If Python MCP channels don't work (Research Spike 2 fails)

**Fallback:** Write the WhatsApp channel in TypeScript/Bun, using the Telegram plugin as a template. The nanobot Python code becomes reference material rather than a direct port. The `.mcp.json` would use `bun` instead of `uv run`.

### If channels require TTY (Research Spike 1 fails)

**Fallback:** The `--dangerously-load-development-channels` flag may need a TTY for the confirmation prompt. Options:
- Use `yes | claude --dangerously...` to auto-confirm
- Check if there's a `--yes` or `--non-interactive` flag
- Use `expect` or `unbuffer` to simulate TTY
- Fall back to the Agent SDK approach (SPEC.md) if channels are fundamentally incompatible with headless Docker

### If plugin loading from bind mounts fails (Research Spike 3 fails)

**Fallback:** Copy plugin files into the Docker image at build time instead of mounting. Loses the ro mount benefit but still works. The `docker-compose.yml` would use a build arg instead of a volume.

---

## Out of Scope (Phase 2+)

These are explicitly deferred from the MVP:
- Permission relay (approve/deny tool use from WhatsApp)
- Access management skill (`/whatsapp-channel:access`)
- Official Telegram/Discord plugin enablement (just needs `CHANNELS` env var update)
- Scheduled tasks / proactive messages
- Health checks and monitoring
- Cost tracking

---

## Estimated Effort

| Step | Description | Effort |
|------|-------------|--------|
| Research Spike 1 | Channels in Docker | ~2 hours |
| Research Spike 2 | Python MCP channel | ~1 hour |
| Research Spike 3 | Plugin from bind mount | ~30 min |
| Step 1 | Repository setup | ~30 min |
| Step 2 | Plugin scaffold | ~15 min |
| Step 3 | MCP server core (port from nanobot) | ~4 hours |
| Step 4 | Sender allowlist | ~1 hour |
| Step 5 | WhatsApp formatting | ~1 hour |
| Step 6 | Dockerfile + compose | ~1 hour |
| Step 7 | Integration test | ~2 hours |

**Total: ~12-13 hours** (assuming no major surprises from research spikes)
