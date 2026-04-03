# Plan: Add Test Channel for Dockerized Integration Testing

## Context

We need a way to test the agent end-to-end inside Docker without depending on
Twilio or any external service. A test channel accepts scripted input messages
and captures agent responses, letting pytest verify the full pipeline:
config → app → agent → channel.send().

The test channel is a real channel in `src/` (not just `tests/`), configurable
via TOML like any other channel. This allows running tests inside the Docker
container against the real app.

## TOML Config

```toml
[channels.test]
sender = "test:user1"
messages = ["Hello", "What is 2+2?"]
```

When configured, the test channel:
1. Feeds each message to the agent sequentially on startup
2. Captures all agent responses in an ordered list
3. Exposes responses via a GET endpoint for test assertions

## Files to Change

### 1. `src/clawless/config.py` — Add TestChannelConfig

```python
class TestChannelConfig(BaseModel):
    sender: str = "test:user1"
    messages: list[str] = []  # scripted input messages

class ChannelsConfig(BaseModel):
    twilio_whatsapp: TwilioWhatsAppConfig | None = None
    test: TestChannelConfig | None = None
```

### 2. `src/clawless/channels/test.py` — New test channel

- Implements `Channel` protocol
- `name = "test"`, `formatting_instructions = "Plain text only."`
- `__init__(config, app)`:
  - Stores config
  - Registers GET `/test/responses` endpoint on app (returns captured responses as JSON)
  - Stores reference to app for accessing agent via `app.state.agent`
- `send(to, text, media)`: appends `{"to": to, "text": text, "media": media}` to `self.responses` list
- `run()` async method: iterates through `config.messages`, creates `InboundMessage` for each, calls `app.state.agent.process_message(msg, self)` sequentially (awaiting each)
- Run is triggered after app startup (called from lifespan)

### 3. `src/clawless/app.py` — Wire test channel

```python
if settings.channels.test:
    from clawless.channels.test import TestChannel
    app.state.test = TestChannel(settings.channels.test, app)
    # Run test messages after startup
    asyncio.create_task(app.state.test.run())
```

### 4. `tests/test_agent_integration.py` — Full integration tests

- Requires Docker with Claude CLI + API key (real agent calls)
- Creates a `config.toml` with `[channels.test]` section and scripted messages
- Starts the app via `httpx.AsyncClient` + `ASGITransport`
- Waits for test channel to finish running all messages
- Hits `/test/responses` to get captured outputs
- Asserts agent responded to each message with non-empty text

### 5. `config.toml.example` — Document the test channel

Add commented-out section:
```toml
# [channels.test]
# sender = "test:user1"
# messages = ["Hello", "What is 2+2?"]
```

## Verification

1. `uv run pytest tests/ -v` — existing unit tests still pass
2. Inside Docker with API key: add `[channels.test]` to config.toml, run the app, GET `/test/responses` — see real agent replies
3. `has_any()` on ChannelsConfig returns true when only test channel is configured
