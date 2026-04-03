# Plan: Simplify to Home Directory Convention

## Context

The current setup has multiple separately-configurable paths (`workspace`, `data_dir`,
`plugins` list) and the docker-compose has 5 separate volume mounts. This is too
complex. We simplify by assuming everything lives under `~` (the home dir of the
`clawless` user in Docker).

### Key insight from Claude Agent SDK reference

`ClaudeAgentOptions` accepts:
- **`cwd`**: workspace path — set to `~/workspace`
- **`plugins`**: `[{"type": "local", "path": "..."}]` — individual plugin paths
- **`setting_sources`**: `["user", "project"]` — SDK loads `~/.claude/settings.json`
  (user) and `{cwd}/.claude/settings.json` + CLAUDE.md (project) automatically
- **`env`**: dict of env vars passed to the CLI process

The SDK finds `.claude` via `~`. In Docker, user is `clawless`, so `~` = `/home/clawless`.
No explicit `.claude` configuration needed — it just works.

Our framework state (`claude_sessions.json`, `config.toml`) is NOT part of the SDK — 
it goes in `~/data/` where the agent can't see it.

## Directory Structure

```
~ (/home/clawless in Docker)
├── workspace/              # SDK cwd — agent operates here. rw
│   ├── media/              # Channel media files (auto-created)
│   └── .claude/            # Project-level settings + CLAUDE.md
├── .claude/                # User-level SDK settings + credentials
│   ├── settings.json       # User settings (loaded by SDK via setting_sources=["user"])
│   └── .credentials.json   # API credentials (mountable from Docker host)
├── data/                   # Framework state. rw, NOT agent-accessible
│   ├── config.toml         # App config (channels, claude options, etc.)
│   └── claude_sessions.json # Session persistence (auto-created by our AgentManager)
└── plugin/                # THE plugin directory — this IS the plugin, not a parent
    ├── .claude-plugin/
    │   └── plugin.json
    ├── skills/
    ├── agents/
    ├── commands/
    └── hooks/
```

Note: `~/plugin/` is itself a single plugin (with its own `.claude-plugin/plugin.json`),
not a container of multiple plugins. Passed to SDK as one entry:
`[{"type": "local", "path": str(Path.home() / "plugin")}]`.

## Bootstrap

Everything is relative to `Path.home()`. No env var needed in production.
A `ClawlessPaths` class in `config.py` provides all derived paths as properties,
keeping path logic centralized and out of `app.py`.

## Files to Change

### 1. `src/clawless/config.py` — Remove AppConfig, add ClawlessPaths

Remove `AppConfig` entirely. Add a paths class that derives everything from `~`
and validates that required dirs exist on construction:

```python
class ClawlessPaths:
    """All paths derived from the user's home directory.
    
    Validates that required directories exist on construction.
    Use clawless-init to create the expected structure.
    """

    def __init__(self) -> None:
        self._home = Path.home()
        self._validate()

    def _validate(self) -> None:
        missing = [
            name for name, path in [
                ("workspace", self.workspace),
                ("data", self.data_dir),
                ("plugin", self.plugin_dir),
            ]
            if not path.is_dir()
        ]
        if missing:
            raise RuntimeError(
                f"Missing directories under {self._home}: {', '.join(missing)}. "
                f"Run 'clawless-init {self._home}' to create the expected structure."
            )
        if not self.config_file.exists():
            raise RuntimeError(
                f"Config file not found: {self.config_file}. "
                f"Run 'clawless-init {self._home}' to create it."
            )

    @property
    def home(self) -> Path: return self._home

    @property
    def workspace(self) -> Path: return self._home / "workspace"

    @property
    def data_dir(self) -> Path: return self._home / "data"

    @property
    def plugin_dir(self) -> Path: return self._home / "plugin"

    @property
    def config_file(self) -> Path: return self.data_dir / "config.toml"

    @property
    def media_dir(self) -> Path: return self.workspace / "media"
```

Update `Settings.settings_customise_sources` to use
`Path.home() / "data" / "config.toml"` as default TOML path (note: can't use
`ClawlessPaths()` here since validation would fail before Settings is loaded;
just compute the path directly).

`Settings` keeps only `claude: ClaudeConfig` and `channels: ChannelsConfig`.

### 2. `src/clawless/app.py` — Use ClawlessPaths

```python
paths = ClawlessPaths()  # validates dirs exist, raises if not

# media_dir is auto-created (runtime artifact, not part of init structure)
paths.media_dir.mkdir(parents=True, exist_ok=True)

# Single plugin if plugin_dir has the plugin manifest
plugins = [str(paths.plugin_dir)] if (paths.plugin_dir / ".claude-plugin").is_dir() else []

app.state.agent = AgentManager(settings.claude, plugins, paths.workspace, paths.data_dir)
```

### 3. `src/clawless/agent.py` — setting_sources=["user", "project"]

Change `setting_sources=["project"]` to `setting_sources=["user", "project"]`.
Constructor stays the same: `(config, plugins, workspace, data_dir)`.

### 4. `src/clawless/init.py` — New `clawless-init` command

Scaffolds the full home structure including plugin skeleton. Reused by tests.

```python
def init_home(path: Path) -> None:
    """Create the prescribed clawless directory structure."""
    for subdir in ["workspace", ".claude", "data"]:
        (path / subdir).mkdir(parents=True, exist_ok=True)

    # Plugin skeleton with prescribed structure
    plugin = path / "plugin"
    for plugin_subdir in [".claude-plugin", "skills", "agents", "commands", "hooks"]:
        (plugin / plugin_subdir).mkdir(parents=True, exist_ok=True)

    # Minimal plugin.json
    manifest = plugin / ".claude-plugin" / "plugin.json"
    if not manifest.exists():
        manifest.write_text('{"name": "private-plugins"}\n')

    # Config template
    config_dest = path / "data" / "config.toml"
    if not config_dest.exists():
        config_dest.write_text(CONFIG_TEMPLATE)

def main():
    parser = argparse.ArgumentParser(prog="clawless-init")
    parser.add_argument("path", nargs="?", default=str(Path.home() / "clawless_home"))
    args = parser.parse_args()
    path = Path(args.path).resolve()
    init_home(path)
    print(f"Initialized clawless home at {path}")
```

### 5. `pyproject.toml` — Add entry point

```toml
[project.scripts]
clawless = "clawless.app:main"
clawless-init = "clawless.init:main"
```

### 6. `Dockerfile` — Create structure

```dockerfile
RUN useradd -m -s /bin/bash clawless
RUN mkdir -p /home/clawless/workspace /home/clawless/.claude \
             /home/clawless/data /home/clawless/plugin && \
    chown -R clawless:clawless /home/clawless
USER clawless
WORKDIR /home/clawless/workspace
```

### 7. `docker-compose.yml` — Single bind mount, explicit env vars

```yaml
services:
  agent:
    build: .
    ports:
      - "8080:8080"
    volumes:
      # Host dir created by: clawless-init /path/to/my/data
      - ${CLAWLESS_HOST_DIR:?Set CLAWLESS_HOST_DIR to the host path created by clawless-init}:/home/clawless:rw

      # Optional: mount host Claude credentials for subscription auth
      # - ~/.claude/.credentials.json:/home/clawless/.claude/.credentials.json:ro
    environment:
      # Set explicitly — not inherited from host environment
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY}
    restart: unless-stopped
```

Note: `ANTHROPIC_API_KEY` is the standard env var the Claude SDK reads directly.
`CLAUDE__API_KEY` (double underscore) was a pydantic-settings mapping to our
`ClaudeConfig.api_key` field — we can drop that field since the SDK reads the
env var itself.

### 8. `config.toml.example` — Remove [app] section

```toml
# Clawless configuration — place at ~/data/config.toml
# In Docker: /home/clawless/data/config.toml

[claude]
max_turns = 30
max_budget_usd = 1.0
max_concurrent_requests = 3

# ... channels unchanged
```

Remove `api_key` from `[claude]` — the SDK reads `ANTHROPIC_API_KEY` env var
directly (or `~/.claude/.credentials.json`).

### 9. `tests/test_channel_integration.py` — Set HOME, reuse init_home()

```python
from clawless.init import init_home

@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client():
    run_dir = (PROJECT_ROOT / "data" / str(uuid.uuid4())).resolve()
    init_home(run_dir)
    (run_dir / "data" / "config.toml").write_text(TOML_CONFIG)

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(run_dir)
    try:
        from clawless.app import app
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        if old_home:
            os.environ["HOME"] = old_home
        else:
            os.environ.pop("HOME", None)
```

### 10. `tests/test_config.py` — Update for removed AppConfig

Remove `plugins`, `app.workspace`, `app.data_dir` assertions.
Add tests for `ClawlessPaths` property derivation.

## Verification

1. `uv run pytest tests/ -v` — all tests pass
2. `clawless-init /tmp/test-home && ls -la /tmp/test-home` — prescribed structure
3. Integration tests pass with isolated HOME under `./data/<uuid>/`
4. Docker: `clawless-init ./my-data && CLAWLESS_HOST_DIR=./my-data docker compose up`

---

# (Previous plan — implemented)

# Plan: Add Test Channel for Integration Testing

## Context

We need a test channel that can run end-to-end integration tests against the real
Claude Agent SDK without depending on Twilio or any external messaging service. The
test channel feeds scripted messages to the agent, captures responses, and exposes
them via HTTP endpoints for test assertions.

This is a real channel in `src/` (not `tests/`), configurable via TOML like any
other channel, allowing tests to exercise the full pipeline: config → app → agent → channel.send().

## Improvements over 002-test-channel.md

- **Completion signal**: adds an `asyncio.Event` and `/test/status` endpoint so tests
  know when all scripted messages have been processed (no arbitrary sleeps/polling)
- **Clearer run() lifecycle**: `run()` is launched as a background task in lifespan,
  sets a done event on completion, and handles errors per-message
- **Response structure**: each captured response includes the original input message
  for easier assertion matching

## TOML Config

```toml
[channels.test]
sender = "test:user1"
messages = ["Hello", "What is 2+2?"]
```

## Files to Change

### 1. `src/clawless/config.py` — Add TestChannelConfig

```python
class TestChannelConfig(BaseModel):
    sender: str = "test:user1"
    messages: list[str] = []
```

Add `test: TestChannelConfig | None = None` to `ChannelsConfig`.

### 2. `src/clawless/channels/test.py` — New test channel

Implements the `Channel` protocol from `channels/base.py`, explicitly inheriting
from it for clarity (same pattern as `WhatsAppChannel(Channel)`).

```python
class TestChannel(Channel):
    name = "test"
    formatting_instructions = "Plain text only. No markdown or special formatting."

    def __init__(self, config: TestChannelConfig, app: FastAPI):
        self._config = config
        self._app = app
        self._responses: list[dict] = []   # {"to", "text", "media", "input"}
        self._done = asyncio.Event()
        self._error: str | None = None

        # Register endpoints
        app.get("/test/responses")(self._get_responses)
        app.get("/test/status")(self._get_status)

    async def send(self, to: str, text: str = "", media: list[str] | None = None) -> None:
        self._responses.append({"to": to, "text": text, "media": media or []})

    async def run(self) -> None:
        """Feed scripted messages to the agent sequentially, then signal done."""
        try:
            agent = self._app.state.agent
            for content in self._config.messages:
                msg = InboundMessage(sender=self._config.sender, content=content)
                await agent.process_message(msg, self)
        except Exception as e:
            self._error = str(e)
            logger.exception("Test channel run failed")
        finally:
            self._done.set()

    async def _get_responses(self):
        return {"responses": self._responses}

    async def _get_status(self):
        return {
            "done": self._done.is_set(),
            "total_messages": len(self._config.messages),
            "total_responses": len(self._responses),
            "error": self._error,
        }
```

Key design notes:
- `run()` calls `process_message` directly (not fire-and-forget) so messages are
  sequential and we know when all are done
- `process_message` internally calls `channel.send()` with the response, so
  `self._responses` fills up naturally
- `_done` event signals completion; `_error` captures any failure
- No `input` field needed in responses — responses are ordered to match input messages

### 3. `src/clawless/app.py` — Wire test channel

In the lifespan, after agent creation and before `yield`:

```python
if settings.channels.test:
    from clawless.channels.test import TestChannel
    app.state.test = TestChannel(settings.channels.test, app)
    asyncio.create_task(app.state.test.run())
    logger.info("Test channel active — %d scripted messages",
                len(settings.channels.test.messages))
```

Import `asyncio` at top of file.

### 4. `tests/test_channel_integration.py` — Integration tests

Requires a real Claude API key (via mounted `.credentials.json` or `CLAUDE__API_KEY`).

```python
import asyncio
import os
import tempfile

import httpx
import pytest
from httpx import ASGITransport

TOML_CONFIG = """
[claude]
max_turns = 5
max_budget_usd = 0.50

[channels.test]
sender = "test:user1"
messages = ["Hello, who are you?", "What is 2+2?"]
"""

@pytest.fixture
async def client():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(TOML_CONFIG)
        f.flush()
        os.environ["CONFIG_FILE"] = f.name

    from clawless.app import app
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    os.environ.pop("CONFIG_FILE", None)

async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200

async def test_scripted_messages_get_responses(client):
    # Wait for test channel to finish (poll /test/status)
    for _ in range(120):  # up to 2 minutes
        r = await client.get("/test/status")
        status = r.json()
        if status["done"]:
            break
        await asyncio.sleep(1)

    assert status["done"] is True
    assert status["error"] is None

    r = await client.get("/test/responses")
    responses = r.json()["responses"]
    assert len(responses) == 2
    for resp in responses:
        assert resp["text"]  # non-empty response from agent
        assert resp["to"] == "test:user1"
```

### 5. `config.toml.example` — Document the test channel

Add commented-out section:
```toml
# [channels.test]
# sender = "test:user1"
# messages = ["Hello", "What is 2+2?"]
```

## Verification

1. `uv run pytest tests/test_base.py tests/test_config.py tests/test_utils.py -v` — existing tests still pass
2. Add `[channels.test]` to config and `TestChannelConfig` to config, then
   `uv run pytest tests/test_config.py -v` — config loading works with test channel
3. With API key available: `uv run pytest tests/test_channel_integration.py -v` —
   real agent responds to scripted messages
4. `has_any()` returns true when only test channel is configured
