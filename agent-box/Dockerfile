FROM debian:trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git iproute2 nano && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*

# Install ttyd (web terminal) - not in bookworm repos
RUN curl -fsSL -o /usr/local/bin/ttyd https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 \
    && chmod +x /usr/local/bin/ttyd
    
# Install docker client
RUN curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && chmod a+r /etc/apt/keyrings/docker.asc
RUN echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt-get update && apt-get install -y docker-ce-cli && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user for Claude Code
RUN useradd -m -s /bin/bash claude

COPY ep.sh /ep.sh
RUN chmod +x /ep.sh

# Plugin bootstrap: list + installer script
COPY plugins.txt /opt/agent-box/plugins.txt
COPY scripts/ /opt/agent-box/scripts/
RUN chmod +x /opt/agent-box/scripts/*.sh

# Bake the agent's operating manual so ep.sh can copy it into the .claude volume on every startup,
# keeping it current after image rebuilds even when the volume already has content.
COPY CLAUDE.md /opt/agent-box/CLAUDE.md

ENTRYPOINT ["/ep.sh"]