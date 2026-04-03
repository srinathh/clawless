FROM python:3.13-slim

# Create non-root user (required for bypassPermissions)
RUN useradd -m -s /bin/bash appuser

# Create default data directory for framework state (session maps, etc.)
RUN mkdir -p /var/lib/clawless && chown appuser:appuser /var/lib/clawless

# Install Node.js (required by Claude Code CLI)
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (required by the Agent SDK)
RUN npm install -g @anthropic-ai/claude-code

# Copy and install the app
COPY --chown=appuser:appuser . /app
RUN pip install --no-cache-dir /app

USER appuser
WORKDIR /home/appuser/workspace

EXPOSE 8080

CMD ["clawless"]
