# Spec: Move Framework Internals from CLAUDE.md to system_prompt

## Problem

Currently, framework-internal instructions are scattered across CLAUDE.md templates
and inline prompt prepending in `agent.py`. These internals (send_message tool usage,
media file paths, workspace layout) are visible and editable by users in CLAUDE.md,
which is confusing — users may accidentally break framework behavior by editing them.

Additionally, the `TOOL_SYSTEM_PROMPT` in `agent.py` is prepended to the user message
text rather than being part of the actual system prompt, which is semantically wrong.

## Goal

Split instructions into two clear layers:

- **`system_prompt` (in code)**: Framework internals the user should never need to edit
- **CLAUDE.md templates (user-editable)**: Identity, personality, communication style

## SDK Context

`ClaudeAgentOptions.system_prompt` accepts `str | SystemPromptPreset | SystemPromptFile | None`.

### SystemPromptPreset (TypedDict)

```python
{
    "type": "preset",
    "preset": "claude_code",    # Loads the full Claude Code system prompt
    "append": "..."             # Optional: appended to the preset prompt
}
```

- A plain `str` **replaces** the Claude Code system prompt entirely (loses built-in tool instructions)
- `SystemPromptPreset` with `preset="claude_code"` loads the full Claude Code prompt
- The `append` field adds custom instructions **after** the preset without replacing it
- `SystemPromptFile` with `type="file"` loads from an external file

### Interaction with CLAUDE.md

`setting_sources=["user", "project"]` loads CLAUDE.md files independently of
`system_prompt`. Both work together:

1. `system_prompt` provides Claude's core behavioral instructions
2. `setting_sources` loads CLAUDE.md as additional project/user context

## Current State

### agent.py
```python
# Prepended to user message text (not actual system prompt)
TOOL_SYSTEM_PROMPT = """\
You MUST use the send_message tool for ALL communication with the user.
Your final text response is NOT delivered — only send_message calls reach the user.
Always call send_message at least once per turn with your reply.
For media/files, include local file paths in the media parameter."""

# In _run_query():
prompt = f"{TOOL_SYSTEM_PROMPT}\n\n[{channel.formatting_instructions}]\n\n{message.content}"
```

### ClaudeAgentOptions (no system_prompt set)
```python
options = ClaudeAgentOptions(
    # ... no system_prompt parameter
    setting_sources=["user", "project"],
)
```

### USER_CLAUDE_MD_TEMPLATE (init.py) — mostly user-facing, OK as-is
```
# Clawless Personal Assistant
You are a personal AI assistant running on the Clawless framework...
## Communication style
- Be concise and conversational...
```

### PROJECT_CLAUDE_MD_TEMPLATE (init.py) — mix of internals and user content
```
# Workspace
Your working directory is ~/workspace/...          <-- internal
## Media
Inbound media from users arrives as...             <-- internal
Outbound media: save files to...                   <-- internal
## Plugin
A plugin at ~/plugin/ may provide...               <-- semi-internal
## Sending messages
Use the send_message tool for ALL replies...       <-- internal
```

## Proposed Design

### 1. Use `SystemPromptPreset` with `append` in ClaudeAgentOptions

Move all framework internals into the `append` field of a `claude_code` preset.
This preserves all built-in Claude Code tool instructions while adding our
framework-specific behavior.

```python
FRAMEWORK_SYSTEM_PROMPT = """\
You MUST use the send_message tool for ALL communication with the user.
Your final text response is NOT delivered — only send_message calls reach the user.
Always call send_message at least once per turn with your reply.

Your working directory is ~/workspace/. You operate with unrestricted permissions.

## Media handling
- Inbound media from users arrives as `[mime/type: /path/to/file]` tags in the
  message text. Files are stored under ~/workspace/media/inbound/. You can read
  image files directly since you are multimodal.
- To send media/files to the user: save to ~/workspace/media/outbound/ and pass
  the local file path in the send_message tool's media parameter.

## Plugin
A plugin at ~/plugin/ may provide additional skills, agents, commands, and hooks.
Check ~/plugin/skills/ for available skills if relevant to a task."""

# In _build_options():
options = ClaudeAgentOptions(
    system_prompt={
        "type": "preset",
        "preset": "claude_code",
        "append": FRAMEWORK_SYSTEM_PROMPT,
    },
    # ... rest of options
)
```

### 2. Strip internals from PROJECT_CLAUDE_MD_TEMPLATE

The project CLAUDE.md becomes minimal — just a pointer for user customization:

```
# Workspace

This is your working directory. See ~/plugin/skills/ for available skills.
```

Or even empty, leaving it as a place for the user to add their own project-level
instructions.

### 3. Keep USER_CLAUDE_MD_TEMPLATE as-is

The user-level CLAUDE.md (identity, communication style) is already user-facing
and appropriate for CLAUDE.md. No changes needed.

### 4. Remove TOOL_SYSTEM_PROMPT and prompt prepending

The `TOOL_SYSTEM_PROMPT` constant in `agent.py` and the prompt prepending in
`_run_query()` are no longer needed — the system_prompt parameter handles this.

The prompt construction simplifies to:
```python
prompt = f"[{channel.formatting_instructions}]\n\n{message.content}"
```

## Files to Modify

| File | Change |
|------|--------|
| `src/clawless/agent.py` | Add `system_prompt` to `ClaudeAgentOptions`, remove `TOOL_SYSTEM_PROMPT`, simplify prompt construction |
| `src/clawless/init.py` | Strip framework internals from `PROJECT_CLAUDE_MD_TEMPLATE` |
| `docs/ARCHITECTURE.md` | Document `system_prompt` usage, update ClaudeAgentOptions table |

## Migration Note

Existing deployments have the old `PROJECT_CLAUDE_MD_TEMPLATE` content in their
`~/workspace/.claude/CLAUDE.md`. Since `init_home()` only writes if the file doesn't
exist, existing files won't be updated automatically. This is fine — the system_prompt
is authoritative, and duplicate instructions in CLAUDE.md are harmless (just redundant).
Users can optionally clean up their CLAUDE.md.

## Verification

```bash
uv run pytest tests/test_config.py -v
uv run pytest tests/test_channel_integration.py -v -s
uv run pytest -m docker tests/test_docker_integration.py -v -s
```
