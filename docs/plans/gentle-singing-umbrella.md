# Plan: Require ANTHROPIC_API_KEY, remove credentials file support, make config.toml optional

## Context

Two related simplifications:
1. Mounting `~/.claude/.credentials.json` into Docker is unreliable — OAuth tokens expire and can't be refreshed headlessly. Additionally, as of April 4, 2026, Anthropic's ToS disallows third-party harnesses from using the OAuth/credentials login. Require `ANTHROPIC_API_KEY` everywhere.
2. For Kubernetes (DOKS) deployment, config should come from env vars/Secrets without needing a TOML file. Make TOML optional — env vars alone are sufficient.

Supersedes `zazzy-plotting-boot.md`.

## Settings inventory

All settings with their env var equivalents (`__` delimiter):

| Setting | Env var | Default | Required? |
|---------|---------|---------|-----------|
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | — | **yes** |
| `port` | `PORT` | `18265` | no |
| `claude.max_turns` | `CLAUDE__MAX_TURNS` | `30` | no |
| `claude.max_budget_usd` | `CLAUDE__MAX_BUDGET_USD` | `1.0` | no |
| `claude.max_concurrent_requests` | `CLAUDE__MAX_CONCURRENT_REQUESTS` | `3` | no |
| `claude.request_timeout` | `CLAUDE__REQUEST_TIMEOUT` | `300.0` | no |
| `channels.twilio_whatsapp.*` | `CHANNELS__TWILIO_WHATSAPP__*` | varies | if twilio channel used |
| `channels.test.*` | `CHANNELS__TEST__*` | varies | if test channel used |

### Config source priority (highest wins)
1. **Environment variables** — always checked
2. **`.env` file** (CWD) — pydantic-settings built-in, no extra dependency. Gracefully ignored if missing.
3. **`~/data/config.toml`** — existing TOML source. Gracefully ignored if missing (verified).

### Validation (all enforced by pydantic at `Settings()` construction time)
- `anthropic_api_key: str` — required field, no default. Pydantic raises `ValidationError` if missing from all sources.
- At least one channel: `@model_validator(mode='after')` on `Settings` calls `channels.has_any()`. This runs after all fields are parsed. If both `anthropic_api_key` is missing AND no channels configured, pydantic reports both errors in one `ValidationError`.
- No separate startup checks needed in `app.py` — if `Settings()` succeeds, config is fully valid.

## Changes

### 1. `src/clawless/config.py`
- Add `anthropic_api_key: str` to `Settings` (required, no default)
- Add `env_file: '.env'` to `model_config` — enables `.env` loading (no dependency needed)
- Add `dotenv_settings` to `settings_customise_sources` return tuple, between `env_settings` and TOML
- Add `@model_validator(mode='after')` on `Settings`: raise `ValueError` if `not self.channels.has_any()`
- Remove `config_file` existence check from `ClawlessPaths._validate()` (lines 45-49)
- Remove `config_file` property — only used by the deleted check and one test assertion
- Update module docstring

### 2. `src/clawless/app.py`
- Remove the `has_any()` runtime check (lines 51-52) — now handled by pydantic model_validator
- Update error message on line 52 is no longer needed

### 3. `docker-compose.yml`
- Remove lines 10-12: `CLAUDE_CREDENTIALS_FILE` volume mount and comments
- Remove comment on line 14
- Keep `ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}` — comes from `.env` or host environment

### 4. `Dockerfile`
- Keep `.claude` in `mkdir -p` — still needed for sessions, SDK state

### 5. `tests/test_docker_integration.py`
- Simplify `_resolve_credentials()`: only check `ANTHROPIC_API_KEY`, skip if not set. Remove `.credentials.json` fallback. If missing, `Settings()` would fail with a clear pydantic error anyway, but we skip to avoid a confusing test failure.

### 6. `tests/test_channel_integration.py`
- Remove lines 42-45: `.credentials.json` symlink logic
- Update module docstring (line 3) — remove `.credentials.json` reference

### 7. `tests/test_config.py`
- Remove `config_file` assertion (line 39)
- Tests that call `Settings()` now need `ANTHROPIC_API_KEY` in env — add to `_setup_home` or use env var fixture
- `test_no_channel_configured`: update to expect `ValidationError` since model_validator now enforces at least one channel
- `test_load_from_toml`, `test_env_var_overrides_toml`, `test_defaults`: add `ANTHROPIC_API_KEY` to env

### 8. `CLAUDE.md`
- Docker section: remove "Two auth modes", show only API key (via `.env` or env var)
- Key conventions: remove `.credentials.json` reference, note config.toml is optional when all config comes from env vars

### 9. `docs/ARCHITECTURE.md`
- Remove `~/.claude/.credentials.json` references
- Remove credentials file volume mount from yaml snippet
- Remove "Two auth modes" — API key only
- Note config source priority: env vars > `.env` > config.toml (all optional except env vars)

### 10. `docs/CODE_WALKTHROUGH.md`
- Remove credentials symlink mention in host integration test description
- Simplify Docker test description
- Remove `config_file` property reference

## Files to modify
- `src/clawless/config.py`
- `src/clawless/app.py`
- `docker-compose.yml`
- `tests/test_docker_integration.py`
- `tests/test_channel_integration.py`
- `tests/test_config.py`
- `CLAUDE.md`
- `docs/ARCHITECTURE.md`
- `docs/CODE_WALKTHROUGH.md`

## Verification
1. `uv run pytest tests/test_config.py -v` — unit tests pass (including updated validation tests)
2. `uv run pytest tests/test_base.py tests/test_utils.py -v` — unaffected tests still pass
3. `uv run pytest tests/test_channel_integration.py -v -s` — host integration passes
4. `uv run pytest -m docker tests/test_docker_integration.py -v -s` — Docker test passes
5. Unset `ANTHROPIC_API_KEY` → `Settings()` raises `ValidationError` with clear message
6. Set `ANTHROPIC_API_KEY` but no channels → `Settings()` raises `ValidationError` about channels
