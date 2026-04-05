FROM python:3.13-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Reproducible builds + faster startup
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Create non-root user (required for bypassPermissions)
RUN useradd -m -s /bin/bash clawless

# Create prescribed directory structure under home
RUN mkdir -p /home/clawless/workspace /home/clawless/data /home/clawless/plugin && \
    chown -R clawless:clawless /home/clawless

# Install Node.js (required by Claude Code CLI)
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (required by the Agent SDK)
RUN npm install -g @anthropic-ai/claude-code

# Layer 1: install dependencies only (cached unless pyproject.toml/uv.lock change)
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Layer 2: install the project itself
COPY --chown=clawless:clawless . .
RUN uv sync --frozen

USER clawless
WORKDIR /home/clawless/workspace

EXPOSE 18265

CMD ["clawless"]
