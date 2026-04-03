FROM python:3.13-slim

# Create non-root user (required for bypassPermissions)
RUN useradd -m -s /bin/bash clawless

# Create default directories for workspace and framework state
# These live under the user's home; both can be overridden via config/env vars
RUN mkdir -p /home/clawless/workdir /home/clawless/datadir && \
    chown clawless:clawless /home/clawless/workdir /home/clawless/datadir

# Install Node.js (required by Claude Code CLI)
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (required by the Agent SDK)
RUN npm install -g @anthropic-ai/claude-code

# Copy and install the app
COPY --chown=clawless:clawless . /app
RUN pip install --no-cache-dir /app

USER clawless
WORKDIR /home/clawless/workdir

EXPOSE 8080

CMD ["clawless"]
