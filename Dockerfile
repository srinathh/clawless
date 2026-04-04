FROM python:3.13-slim

# Create non-root user (required for bypassPermissions)
RUN useradd -m -s /bin/bash clawless

# Create prescribed directory structure under home
RUN mkdir -p /home/clawless/workspace /home/clawless/.claude \
             /home/clawless/data /home/clawless/plugin && \
    chown -R clawless:clawless /home/clawless

# Install Node.js (required by Claude Code CLI)
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (required by the Agent SDK)
RUN npm install -g @anthropic-ai/claude-code

# Copy and install the app
COPY --chown=clawless:clawless . /app
RUN pip install --no-cache-dir /app

USER clawless
WORKDIR /home/clawless/workspace

EXPOSE 18265

CMD ["clawless"]
