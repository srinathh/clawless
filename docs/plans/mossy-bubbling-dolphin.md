# Plan: Docker Compose Integration Test with Test Channel

## Context

The project has an existing in-process integration test ([test_channel_integration.py](tests/test_channel_integration.py)) that starts the FastAPI app via ASGI transport and exercises the test channel. This is fast but doesn't test the Docker image, Dockerfile, or the full container startup path. The user wants a second, slower integration test that runs the app inside Docker Compose and communicates with the test channel over real HTTP — useful for validating the container build and trying variations of test messages.

## Approach

### 1. Update `docker-compose.yml` — credentials.json support

Add the credentials.json mount directly to the main docker-compose file using an env var for the host path:

```yaml
volumes:
  - ${CLAWLESS_HOST_DIR:?...}:/home/clawless:rw
  - ${CLAUDE_CREDENTIALS_FILE:-/dev/null}:/home/clawless/.claude/.credentials.json:ro
```

Make `ANTHROPIC_API_KEY` optional (remove `?` check, default to empty):

```yaml
environment:
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
```

This supports two auth modes:
- **API key**: set `ANTHROPIC_API_KEY`, leave `CLAUDE_CREDENTIALS_FILE` unset (mounts `/dev/null`, harmless)
- **Credentials file**: leave `ANTHROPIC_API_KEY` unset, set `CLAUDE_CREDENTIALS_FILE=~/.claude/.credentials.json`

### 2. Update `tests/test_channel_integration.py` — timestamp dirs

Change the run directory name from UUID to a filename-safe timestamp with milliseconds (e.g. `data/20260404_153012_456/`).

### 3. New test file: `tests/test_docker_integration.py`

Follows the same pattern as the existing integration test:
- Creates an isolated home dir under `data/<timestamp>/` using `init_home()`
- Writes a `config.toml` with the test channel configured
- Starts the app via `docker compose up` using the existing `docker-compose.yml`
- Polls `http://localhost:<port>/test/status` and `/test/responses` via `httpx`
- Tears down with `docker compose down` in cleanup

Key implementation details:
- **Port**: Use a fixed test port `18791` (prod default is `18790`). Passed as `PORT` env var to docker compose.
- **Docker Compose invocation**: `subprocess.run` with env vars `CLAWLESS_HOST_DIR`, `ANTHROPIC_API_KEY` (or `CLAUDE_CREDENTIALS_FILE`), `PORT` pointing at the existing `docker-compose.yml` at project root
- **Fixture**: A session-scoped pytest fixture that builds, starts, and tears down the container
- **Polling**: Poll `/test/status` until `done` is true, with up to 5 minutes timeout. Print a progress line every 10 seconds so the user knows the test is alive and doesn't kill it.
- **Credentials** (checked in this order):
  1. If `ANTHROPIC_API_KEY` env var exists → pass it to Docker Compose
  2. Else if `~/.claude/.credentials.json` exists on the host → set `CLAUDE_CREDENTIALS_FILE` pointing to it
  3. Else → skip the test with `pytest.skip("No ANTHROPIC_API_KEY or ~/.claude/.credentials.json found")`

### 4. Pytest marker configuration

- Register a custom `docker` marker
- Add `addopts = "-m 'not docker'"` to `[tool.pytest.ini_options]` in `pyproject.toml` so docker tests are skipped by default
- To run: `uv run pytest -m docker -v -s`
- To run everything: `uv run pytest -m '' -v`

### 5. Files to modify/create

| File | Action |
|---|---|
| `docker-compose.yml` | **Edit** — add credentials.json volume mount, make API key optional |
| `tests/test_channel_integration.py` | **Edit** — change UUID to timestamp-based dir names |
| `tests/test_docker_integration.py` | **Create** — Docker Compose integration test |
| `pyproject.toml` | **Edit** — add `[tool.pytest.ini_options]` with marker registration and `addopts` |

### 6. Test structure

```python
# Fixture (session-scoped):
#   1. Generate timestamp-based dir name: data/20260404_153012_456/
#   2. init_home(run_dir) + write config.toml with test channel messages
#   3. Resolve credentials (ANTHROPIC_API_KEY → ~/.claude/.credentials.json → skip)
#   4. docker compose up -d --build (with CLAWLESS_HOST_DIR, PORT=18791, credentials)
#   5. Wait for /health to return 200 (with progress prints every 10s)
#   6. yield httpx.Client(base_url="http://localhost:18791")
#   7. docker compose down (always, even on failure)
#      Print docker compose logs on failure for debugging

# Tests:
#   test_health() — GET /health returns 200
#   test_scripted_messages_get_responses() — poll /test/status (with 10s progress prints),
#       then check /test/responses
```

The test messages in config.toml are the same two as the existing test (`"Hello, who are you?"`, `"What is 2+2?"`), making it easy to compare behavior between host and Docker runs.

### 7. Verification

```bash
# Build and run the Docker test (requires Docker + API key or credentials.json):
uv run pytest -m docker tests/test_docker_integration.py -v -s

# Confirm default pytest still skips it:
uv run pytest tests/ -v  # should NOT run test_docker_integration

# Confirm the existing host integration test is unaffected:
uv run pytest tests/test_channel_integration.py -v
```
