# Plan: Migrate Config from Flat Env Vars to Nested TOML

**Status: Completed**

## Context

The current `config.py` has 12 flat fields loaded from environment variables. As we add
more channels (Telegram, SMS, etc.), flat env vars become unwieldy — each channel needs
its own credentials, webhook paths, allowed senders, etc. Switching to nested TOML config
gives us structured, commented configuration with env var overrides for secrets.

## Changes Made

1. `config.py` — Nested Pydantic models (AppConfig, ClaudeConfig, TwilioWhatsAppConfig, ChannelsConfig) + TomlConfigSettingsSource
2. `app.py` — Dynamic channel instantiation, passes config slices to each component
3. `agent.py` — Takes ClaudeConfig + plugins instead of flat Settings
4. `channels/whatsapp.py` — Takes TwilioWhatsAppConfig, renamed to twilio-whatsapp
5. `docker-compose.yml` — Mounts config.toml read-only, secrets via environment vars
6. `config.toml.example` — Primary config reference (replaces .env.example)
7. Tests added for config, utils, base types
