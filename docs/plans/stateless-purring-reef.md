# Experiment: Eliminate ~/.claude/ — consolidate into workspace/.claude

**Status: REVERTED** — Consolidation was implemented but caused Claude Code to block
writes to `~/workspace/.claude/skills/` and `agents/` because it treated the
`CLAUDE_CONFIG_DIR` directory as protected. Reverted to the two-directory model:
`~/.claude/` for SDK runtime state, `~/workspace/.claude/` for project-level config.

## Context

Currently the home directory has two `.claude/` directories:
- `~/.claude/` — user-level: CLAUDE.md (persona), settings.json, and SDK runtime state (sessions under `projects/`)
- `~/workspace/.claude/` — project-level: CLAUDE.md (workspace context), settings.json

This split exists because `setting_sources=["user", "project"]` tells the SDK to read from both locations. The SDK also writes session files to `~/.claude/projects/<sanitized-cwd>/` by default.

**Goal:** Eliminate `~/.claude/` entirely. Make `~/workspace/.claude/` the single location for config and runtime state.

## Approach

Two SDK knobs make this possible:

1. **`setting_sources=["project"]`** — only read config from `<cwd>/.claude/` (no user-level)
2. **`env={"CLAUDE_CONFIG_DIR": str(workspace / ".claude")}`** — redirect session writes from `~/.claude/` to `~/workspace/.claude/`

## Branch

Create `experiment/consolidate-claude-dir` from main before making any changes.

## Changes

### 1. `src/clawless/agent.py` — _build_options()

```python
# Before
setting_sources=["user", "project"],

# After
setting_sources=["project"],
env={"CLAUDE_CONFIG_DIR": str(self._workspace / ".claude")},
```

### 2. `src/clawless/init.py` — remove ~/.claude/, merge CLAUDE.md

- Remove `".claude"` from the subdirs list (line 60)
- Remove `user_claude_md` creation (lines 77-79)
- Merge `USER_CLAUDE_MD_TEMPLATE` content into `PROJECT_CLAUDE_MD_TEMPLATE` (persona + workspace context in one file)
- Update the printed directory tree to remove `.claude/` line

### 3. `Dockerfile` — remove ~/.claude/ from mkdir

```dockerfile
# Before
RUN mkdir -p /home/clawless/workspace /home/clawless/.claude \
             /home/clawless/data /home/clawless/plugin

# After
RUN mkdir -p /home/clawless/workspace /home/clawless/data /home/clawless/plugin
```

### 4. `ClawlessPaths` in `config.py` — no changes needed

`ClawlessPaths` doesn't reference `~/.claude/` — it only validates workspace, data, and plugin. No change required.

### 5. Docs — update references

- `docs/ARCHITECTURE.md` — remove `~/.claude/` from directory tree and design decisions
- `docs/CODE_WALKTHROUGH.md` — update init.py description
- `CLAUDE.md` (project root) — update directory tree in "Project structure" if it mentions ~/.claude/

## Files to modify

- `src/clawless/agent.py` (line 94)
- `src/clawless/init.py` (lines 13-24, 60, 77-79, 112-113)
- `Dockerfile` (line 7)
- `docs/ARCHITECTURE.md`
- `docs/CODE_WALKTHROUGH.md`

## Verification

1. Run unit tests: `uv run pytest tests/test_config.py -v`
2. Run integration test: `uv run pytest tests/test_channel_integration.py -v -s`
   - Verify sessions are created under `workspace/.claude/` not `~/.claude/`
   - Check the test's temp home dir for absence of `.claude/` at top level
3. Run `clawless-init /tmp/test-init` and verify:
   - No `.claude/` directory at root
   - `workspace/.claude/CLAUDE.md` contains merged persona + workspace content
4. Docker build succeeds: `docker build -t clawless-test .`

## Open question

**Session path encoding:** `CLAUDE_CONFIG_DIR` redirects the base from `~/.claude/` to `~/workspace/.claude/`. Sessions go to `<config_dir>/projects/<sanitized-cwd>/`. Since `cwd` is `~/workspace`, the session path becomes `~/workspace/.claude/projects/<sanitized-workspace-path>/`. This should work but is worth verifying — if the SDK sanitizes the cwd path and it collides with existing `.claude/` contents, there could be issues.
