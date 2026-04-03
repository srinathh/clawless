# Plan: Migrate Config from Flat Env Vars to Nested TOML

## Context

The current `config.py` has 12 flat fields loaded from environment variables. As we add
more channels (Telegram, SMS, etc.), flat env vars become unwieldy — each channel needs
its own credentials, webhook paths, allowed senders, etc. Switching to nested TOML config
gives us structured, commented configuration with env var overrides for secrets.

## TOML Structure

```toml
# App-level config
[app]
plugins = ["/home/appuser/plugins/my-plugin"]

# Claude agent config
# api_key can be empty if .credentials.json is mounted in ~/.claude
[claude]
api_key = ""
max_turns = 30
max_budget_usd = 1.0
max_concurrent_requests = 3

# Channels — only configured channels get instantiated
[channels.twilio-whatsapp]
account_sid = "AC..."
auth_token = ""
whatsapp_from = "whatsapp:+14155238886"
webhook_path = "/twilio/whatsapp"
public_url = "https://example.ngrok-free.app"
ack_message = "Thinking..."
allowed_senders = ["whatsapp:+1234567890"]
```

## Files to Change

### 1. `src/clawless/config.py` — Nested Pydantic models + TOML source

- Replace flat `Settings` with nested sub-models:
  - `AppConfig(BaseModel)`: `plugins: list[str] = []`
  - `ClaudeConfig(BaseModel)`: `api_key: str = ""`, `max_turns`, `max_budget_usd`, `max_concurrent_requests`
  - `TwilioWhatsAppConfig(BaseModel)`: `account_sid`, `auth_token`, `whatsapp_from`, `webhook_path`, `public_url`, `ack_message`, `allowed_senders`
  - `ChannelsConfig(BaseModel)`: `twilio_whatsapp: TwilioWhatsAppConfig | None = None`
- Root `Settings(BaseSettings)` with `app`, `claude`, `channels` fields
- `settings_customise_sources`: env vars first (wins), then `TomlConfigSettingsSource`
- `env_nested_delimiter="__"` so env vars can override nested keys
- TOML file path from `os.environ.get("CONFIG_FILE", "config.toml")`
- `claude.api_key` defaults to `""` — can be empty when using `.credentials.json` auth
- Required fields within `TwilioWhatsAppConfig`: `public_url` and `allowed_senders` (no defaults) — validated only when the section is present

### 2. `src/clawless/app.py` — Dynamic channel instantiation

- Keep `workspace = Path.cwd()` — Docker's WORKDIR handles this
- Pass `settings.claude` and `settings.app.plugins` to `AgentManager`
- Conditionally create WhatsApp channel: `if settings.channels.twilio_whatsapp:`
- Pass `settings.channels.twilio_whatsapp` (not whole Settings) to `WhatsAppChannel`
- Log which channels are active at startup

### 3. `src/clawless/agent.py` — Take ClaudeConfig instead of Settings

- Change `__init__` signature: `(self, config: ClaudeConfig, plugins: list[str], workspace: Path)`
- `self._config` replaces `self._settings`
- Update field access: `self._config.max_turns`, `self._config.max_budget_usd`, etc.
- `self._plugins` for the plugins list

### 4. `src/clawless/channels/whatsapp.py` — Take TwilioWhatsAppConfig instead of Settings

- Rename channel: `name = "twilio-whatsapp"`
- Change `__init__` signature: `(self, config: TwilioWhatsAppConfig, media_dir: Path, app: FastAPI)`
- `self._config` replaces `self._settings`
- Drop `twilio_` prefix from field access: `self._config.account_sid`, `self._config.public_url`, etc.

### 5. `docker-compose.yml` — Mount config.toml, drop env_file

- Add `./config.toml:/home/appuser/config.toml:ro` volume mount
- Remove `env_file: .env` — all config comes from TOML, secrets set directly
  as `environment:` entries in compose or via Docker secrets
- Keep existing volume mounts for workspace, .claude, plugins

### 6. New `config.toml.example` — Primary config reference

- Full commented TOML with all sections and defaults
- Delete `.env.example` — no longer needed

### 7. Delete `.env.example`

## Docker Secrets Handling

No `.env` file. Secrets are passed via `environment:` in docker-compose or
Docker secrets. Example docker-compose snippet:

```yaml
environment:
  - CLAUDE__API_KEY=${CLAUDE_API_KEY}
  - CHANNELS__TWILIO_WHATSAPP__AUTH_TOKEN=${TWILIO_AUTH_TOKEN}
```

Or simply set them in the host environment / CI pipeline.

## Verification

1. Create a `config.toml` from `config.toml.example`
2. Run `python -c "from clawless.config import Settings; s = Settings(); print(s.model_dump())"` — verify nested structure loads
3. Set `CLAUDE__API_KEY=test` env var — verify it overrides TOML value
4. Remove `[channels.twilio-whatsapp]` section — verify app starts without WhatsApp channel
5. Run app with `uvicorn` and verify health endpoint responds
