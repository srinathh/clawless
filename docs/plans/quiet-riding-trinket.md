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
