# Experiment: Skill Creation Path Separation

## Context

When a user asks the Clawless bot to "create a skill", the agent creates it in `~/plugin/skills/` (the loaded plugin directory) instead of `~/workspace/.claude/skills/` (the standalone project directory). This happens because:

1. The system prompt and CLAUDE.md both point the agent to `~/plugin/skills/` as the skills location
2. `~/workspace/.claude/skills/` doesn't exist (never scaffolded)
3. Docker mounts everything read-write, so the plugin dir is writable

**Desired behavior:**
- **Bot-created skills** → `~/workspace/.claude/skills/<name>/SKILL.md` (standalone format, writable)
- **User-provided skills** → `~/plugin/skills/` (pre-configured, read-only)

Claude Code natively supports both: standalone skills in `.claude/skills/` and plugin skills in loaded plugins. They coexist.

## Changes

### 1. Update system prompt — [agent.py:47-49](src/clawless/agent.py#L47-L49)

Replace the `## Plugin` section with expanded guidance covering skills, agents, and MCP servers:

```
## Skills, agents, and plugins

Two locations provide extensibility:

1. **~/workspace/.claude/** — YOUR writable project directory. When asked to create,
   modify, or delete skills, agents, or MCP configs, use this directory:
   - Skills: ~/workspace/.claude/skills/<skill-name>/SKILL.md (invoked as /<skill-name>)
   - Agents: ~/workspace/.claude/agents/<agent-name>.md
   - MCP servers: ~/workspace/.claude/.mcp.json

2. **~/plugin/** — Pre-configured plugin (READ-ONLY). The user has placed skills,
   agents, commands, hooks, and MCP servers here before deployment. Never write to
   this directory. Plugin skills are invoked as /private-plugin:<skill-name>.

Check both locations when looking for available skills and agents.
```

This covers the full range of extensibility points (skills, agents, MCP) rather than just skills. The SDK plugin docs confirm all three can exist in both standalone `.claude/` and plugin directories.

### 2. Update CLAUDE.md template — [init.py:28](src/clawless/init.py#L28)

Change the workspace/skills line in `PROJECT_CLAUDE_MD_TEMPLATE` to:

```
Your working directory is ~/workspace/.

## Extensibility
- To CREATE new skills, agents, or MCP configs: use ~/workspace/.claude/ (standalone format)
  - Skills: ~/workspace/.claude/skills/<name>/SKILL.md
  - Agents: ~/workspace/.claude/agents/<name>.md
  - MCP servers: ~/workspace/.claude/.mcp.json
- Pre-configured extensions from the user are in ~/plugin/ (read-only, do not modify)
```

### 3. Scaffold standalone dirs — [init.py:73](src/clawless/init.py#L73)

After the existing `.claude/` mkdir, add:

```python
(path / "workspace" / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
(path / "workspace" / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
```

Update the `main()` print tree to show these.

### 4. Move config.toml to ~/clawless.toml

Relocate the config file from `~/data/config.toml` to `~/clawless.toml`. This simplifies the mount story: `~/data/` becomes purely runtime state (sessions.db), and `~/clawless.toml` can be mounted read-only independently.

**Code changes:**
- [config.py:120](src/clawless/config.py#L120) — change TOML path from `Path.home() / "data" / "config.toml"` to `Path.home() / "clawless.toml"`
- [init.py:81](src/clawless/init.py#L81) — write config template to `path / "clawless.toml"` instead of `path / "data" / "config.toml"`
- [init.py:32-33](src/clawless/init.py#L32-L33) — update CONFIG_TEMPLATE comment header
- [init.py:107-108](src/clawless/init.py#L107-L108) — update print tree output
- [tests/helpers.py:27](tests/helpers.py#L27) — write test config to `run_dir / "clawless.toml"`
- [tests/test_config.py](tests/test_config.py) — update all references to config.toml path

**Doc updates** (CLAUDE.md, README.md, ARCHITECTURE.md, CODE_WALKTHROUGH.md, SPEC.md) — update references.

### 5. Split Docker volume mounts — [docker-compose.yml:8](docker-compose.yml#L8)

Replace single mount with separate mounts for different access levels:

```yaml
volumes:
  - ${CLAWLESS_HOST_DIR}/workspace:/home/clawless/workspace:rw
  - ${CLAWLESS_HOST_DIR}/data:/home/clawless/data:rw
  - ${CLAWLESS_HOST_DIR}/clawless.toml:/home/clawless/clawless.toml:ro
  - ${CLAWLESS_HOST_DIR}/plugin:/home/clawless/plugin:ro
```

- `~/clawless.toml` is read-only — config is set by the user, not the agent
- `~/plugin/` is read-only — prevents the agent from writing skills there
- `~/data/` stays read-write — purely runtime state (sessions.db)
- `~/workspace/` stays read-write — agent's working directory

### 6. Add integration test — [tests/helpers.py](tests/helpers.py)

Add a 5th scripted message to `TOML_CONFIG`:
```
"Create a skill called 'greet' that responds with 'Hello!' when invoked. Confirm when done."
```

Add assertion in `assert_agent_responses()`:
```python
# Verify skill created in standalone dir, not plugin dir
skill_file = run_dir / "workspace" / ".claude" / "skills" / "greet" / "SKILL.md"
assert skill_file.exists(), f"Agent did not create skill at {skill_file}"
plugin_skill = run_dir / "plugin" / "skills" / "greet" / "SKILL.md"
assert not plugin_skill.exists(), f"Agent wrongly created skill in plugin dir"
```

Bump `assert len(responses) >= 4` to `>= 5` and increase `max_turns` and `max_budget_usd` in the test config since we're adding another message.

## Files to modify

| File | Change |
|------|--------|
| `src/clawless/agent.py` | System prompt: plugin section → skills/agents/plugins section |
| `src/clawless/init.py` | CLAUDE.md template + scaffold skills/agents dirs + config path |
| `src/clawless/config.py` | TOML path: `~/data/config.toml` → `~/clawless.toml` |
| `docker-compose.yml` | Split volume mount, plugin + config as `:ro` |
| `tests/helpers.py` | Config path + add skill-creation test message + assertions |
| `tests/test_config.py` | Update config.toml references to clawless.toml |
| `CLAUDE.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/CODE_WALKTHROUGH.md` | Update config path references |

## No changes needed

- **app.py** — Plugin loading logic stays the same (still load ~/plugin/ as a plugin)
- **Dockerfile** — Already creates the three subdirectories

## Verification

1. `uv run pytest tests/test_channel_integration.py -v -s` — host integration test verifies skill lands in `.claude/skills/`
2. `uv run pytest -m docker tests/test_docker_integration.py -v -s` — Docker test verifies read-only plugin mount + skill creation
3. Manually inspect the test data directory to confirm skill file location
