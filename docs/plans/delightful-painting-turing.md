# Plan: Move Framework Internals from CLAUDE.md to system_prompt

## Context

Framework-internal instructions (send_message usage, workspace paths, media handling) are currently in two wrong places:
1. `TOOL_SYSTEM_PROMPT` in agent.py — prepended to **user message text** instead of being in the actual system prompt
2. `PROJECT_CLAUDE_MD_TEMPLATE` in init.py — user-editable CLAUDE.md containing internals users shouldn't touch

This change consolidates all framework internals into the SDK's `system_prompt` parameter using a `claude_code` preset with `append`, and strips internals from CLAUDE.md templates.

Spec: [system-prompt-split.md](docs/specs/system-prompt-split.md)

## Design decisions

1. **"bypass permissions"** not "unrestricted permissions" — the agent runs with `permission_mode="bypassPermissions"`, so the prompt should use the same terminology.

2. **Outbound media — agent saves anywhere, channel stages** — The agent doesn't need to save to `~/workspace/media/outbound/`. It can save files anywhere under its workspace. When it passes a path to `send_message(media=[...])`, the channel's `_stage_media()` copies the file to the outbound dir and serves it. The instruction should reflect this.

3. **FRAMEWORK_SYSTEM_PROMPT lives in agent.py** (not config.py) — It's consumed exclusively by `AgentManager._build_options()` in the same file. config.py is for data models (pydantic Settings, ClawlessPaths); a prompt string constant isn't configuration. This also follows the existing pattern (`TOOL_SYSTEM_PROMPT` is already in agent.py).

## Changes

### 1. `src/clawless/agent.py`

**a) Replace `TOOL_SYSTEM_PROMPT` (lines 29-33) with `FRAMEWORK_SYSTEM_PROMPT`:**

```python
FRAMEWORK_SYSTEM_PROMPT = """\
You MUST use the send_message tool for ALL communication with the user.
Your final text response is NOT delivered — only send_message calls reach the user.
Always call send_message at least once per turn with your reply.

Your working directory is ~/workspace/. You have all Claude Code tools available \
with bypass permissions.

## Media handling
- Inbound media from users arrives as `[mime/type: /path/to/file]` tags in the \
message text. Files are stored under ~/workspace/media/inbound/. You can read \
image files directly since you are multimodal.
- To send media/files to the user: pass local file paths in the send_message \
tool's media parameter. The channel will stage and serve them automatically.

## Plugin
A plugin at ~/plugin/ may provide additional skills, agents, commands, and hooks. \
Check ~/plugin/skills/ for available skills if relevant to a task."""
```

Content sources: send_message instructions from `TOOL_SYSTEM_PROMPT`, workspace/media/plugin from `PROJECT_CLAUDE_MD_TEMPLATE`. Outbound media instruction corrected to not prescribe a save location.

**b) Add `system_prompt` to `ClaudeAgentOptions` in `_build_options()` (line 71):**

```python
options = ClaudeAgentOptions(
    system_prompt={
        "type": "preset",
        "preset": "claude_code",
        "append": FRAMEWORK_SYSTEM_PROMPT,
    },
    permission_mode="bypassPermissions",
    # ... rest unchanged
)
```

Uses a plain dict (not an imported TypedDict) — simpler, no coupling to SDK internals.

**c) Simplify prompt construction in `_run_query()` (line 137):**

```python
# Before:
prompt = f"{TOOL_SYSTEM_PROMPT}\n\n[{channel.formatting_instructions}]\n\n{message.content}"
# After:
prompt = f"[{channel.formatting_instructions}]\n\n{message.content}"
```

### 2. `src/clawless/init.py`

**Replace `PROJECT_CLAUDE_MD_TEMPLATE` (lines 26-49) with minimal stub:**

```python
PROJECT_CLAUDE_MD_TEMPLATE = """\
# Workspace

This is your working directory. See ~/plugin/skills/ for available skills.
"""
```

All framework internals moved to `FRAMEWORK_SYSTEM_PROMPT`. `USER_CLAUDE_MD_TEMPLATE` stays as-is.

### 3. `docs/ARCHITECTURE.md`

- **Line 93-101**: Add `system_prompt` row to ClaudeAgentOptions table: `{"type": "preset", "preset": "claude_code", "append": FRAMEWORK_SYSTEM_PROMPT}`
- **Line 109**: Update step 5 from "system instructions + channel formatting + user content" to "channel formatting + user content"
- **Lines 51-55**: Update CLAUDE.md description — project-level is now a minimal stub, framework internals are in `system_prompt`
- **Lines 184-185**: Update send_message section to say "`system_prompt` parameter instructs the agent..."
- **After line 101**: Add "Two-Layer Prompt Design" subsection documenting the split: `system_prompt` for framework internals, CLAUDE.md for user customization

### 4. `docs/CODE_WALKTHROUGH.md`

- **Line 70**: Update "Builds prompt with channel formatting instructions prepended" (already accurate post-change)
- **~Line 62**: Add note that `_build_options()` sets `system_prompt` using `claude_code` preset
- **~Line 166**: Update PROJECT_CLAUDE_MD_TEMPLATE description to reflect minimal content

## Migration

Existing deployments keep their old CLAUDE.md (init_home only writes if absent). The duplicate instructions are harmless — `system_prompt` is now authoritative.

## Verification

```bash
uv run pytest tests/test_config.py -v
uv run pytest tests/test_channel_integration.py -v -s
uv run pytest -m docker tests/test_docker_integration.py -v -s
```

No test changes needed — tests verify agent behavior (send_message usage), not prompt construction internals.
