FROM debian:trixie

# Base utilities for an autonomous dev agent: process tools (procps/psmisc),
# JSON (jq), paging (less), network probing (netcat/dnsutils), and archive/file
# helpers — all small, all used constantly when driving a compose stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git iproute2 nano \
    procps psmisc jq less netcat-openbsd dnsutils \
    unzip zip wget file tree \
    python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

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

# Install Claude Code CLI (image owns the version; update by rebuilding, not in-container)
ENV DISABLE_AUTOUPDATER=1
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user for Claude Code
RUN useradd -m -s /bin/bash claude

# Playwright MCP server + headless Chromium (used by the playwright plugin in
# plugins.txt). Browsers go to a fixed shared path: the default is per-user
# (~/.cache/ms-playwright), but this install runs as root while the MCP runs
# as `claude`. Chromium is installed through the MCP's own bundled playwright
# so the browser revision matches exactly what the server expects.
# The chown lets the agent self-install a newer revision at runtime if a
# newer @playwright/mcp (fetched by the plugin's `npx @playwright/mcp@latest`)
# ever demands one.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN npm install -g @playwright/mcp \
    && cd /usr/lib/node_modules/@playwright/mcp \
    && npx playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chown -R claude:claude /ms-playwright

COPY ep.sh /ep.sh
RUN chmod +x /ep.sh

# Plugin bootstrap: list + installer script
COPY plugins.txt /opt/agent-box/plugins.txt
COPY scripts/ /opt/agent-box/scripts/
RUN chmod +x /opt/agent-box/scripts/*.sh

# Bake the agent's operating manual so ep.sh can copy it into the .claude volume on every startup,
# keeping it current after image rebuilds even when the volume already has content.
COPY CLAUDE.md /opt/agent-box/CLAUDE.md

# Healthy = ttyd answers HTTP on 8081. No -f flag: ttyd uses basic auth, so an
# unauthenticated probe gets 401 — any HTTP response means the terminal is up.
# Generous start period: ep.sh installs plugins before launching ttyd.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -s -o /dev/null http://localhost:8081 || exit 1

ENTRYPOINT ["/ep.sh"]