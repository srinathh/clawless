# Plan: Require ANTHROPIC_API_KEY, remove credentials file support

## Context

Mounting `~/.claude/.credentials.json` into Docker is unreliable — the OAuth tokens inside expire and can't be refreshed in a headless container, causing silent auth failures. `ANTHROPIC_API_KEY` is the only auth method that works reliably in Docker. This change simplifies the auth story: require the API key everywhere.

## Changes

### 1. `docker-compose.yml`
- Remove the `CLAUDE_CREDENTIALS_FILE` volume mount (line 12)
- Remove the comment about credentials file (lines 10-11)
- Make `ANTHROPIC_API_KEY` required (remove the `:-` empty default so compose errors if unset)

### 2. `src/clawless/app.py` — startup validation
- Add a check at app startup: if `ANTHROPIC_API_KEY` is not set in the environment, log an error and exit immediately with a clear message.

### 3. `tests/test_docker_integration.py`
- Simplify `_resolve_credentials()`: only check `ANTHROPIC_API_KEY`, remove the `.credentials.json` fallback. Skip if key not set.
- Remove the `CLAUDE_CREDENTIALS_FILE` env var handling.

### 4. `tests/test_channel_integration.py`
- Remove the `.credentials.json` symlink logic (lines 42-45). The test already works with `ANTHROPIC_API_KEY` env var.
- Add a skip if `ANTHROPIC_API_KEY` is not set.

### 5. `Dockerfile`
- Remove `mkdir -p /home/clawless/.claude` — no longer needed since we don't mount credentials there.

### 6. Docs updates
- `CLAUDE.md` — update the Docker section to show only the API key approach
- `docs/ARCHITECTURE.md`, `docs/CODE_WALKTHROUGH.md` — remove any references to `.credentials.json` auth mode

## Files to modify
- `docker-compose.yml`
- `src/clawless/app.py`
- `tests/test_docker_integration.py`
- `tests/test_channel_integration.py`
- `Dockerfile`
- `CLAUDE.md`
- `docs/ARCHITECTURE.md`
- `docs/CODE_WALKTHROUGH.md`

## Verification
1. `uv run pytest tests/test_config.py -v` — unit tests still pass
2. `ANTHROPIC_API_KEY=sk-... docker compose config` — validates compose file, confirms no credentials mount
3. Unset `ANTHROPIC_API_KEY` and run the app — confirm it errors out with a clear message
4. `ANTHROPIC_API_KEY=sk-... uv run pytest tests/test_channel_integration.py -v -s` — integration test passes
