FROM debian:trixie

# Base utilities for an autonomous dev agent: process tools (procps/psmisc),
# JSON (jq), paging (less), network probing (netcat/dnsutils), and archive/file
# helpers — all small, all used constantly when driving a compose stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git iproute2 nano \
    procps psmisc jq less netcat-openbsd dnsutils openssh-client \
    unzip zip wget file tree arping iputils-ping \
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

# Install Terraform from HashiCorp's official apt repo (same signed-repo pattern
# as the docker client above). Lets the agent run `terraform` for IaC tasks.
RUN curl -fsSL https://apt.releases.hashicorp.com/gpg -o /etc/apt/keyrings/hashicorp.asc && chmod a+r /etc/apt/keyrings/hashicorp.asc
RUN echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/hashicorp.asc] https://apt.releases.hashicorp.com $(. /etc/os-release && echo "$VERSION_CODENAME") main" | tee /etc/apt/sources.list.d/hashicorp.list > /dev/null
RUN apt-get update && apt-get install -y terraform && rm -rf /var/lib/apt/lists/*

# Install kubectl (Kubernetes CLI). Single dependency-free static binary, so it's
# fetched straight from the official release bucket like ttyd above rather than via
# an apt repo. `stable.txt` resolves to the current stable release at build time, and
# the published sha256 guards against a corrupted/truncated download. Works against
# any conformant cluster (vanilla k8s, k3s, …); the kubeconfig is supplied per
# deployment, not baked here.
RUN KVER="$(curl -fsSL https://dl.k8s.io/release/stable.txt)" \
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KVER}/bin/linux/amd64/kubectl" \
    && curl -fsSL -o /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KVER}/bin/linux/amd64/kubectl.sha256" \
    && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum --check \
    && chmod +x /usr/local/bin/kubectl \
    && rm -f /tmp/kubectl.sha256

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
RUN chmod +x /opt/agent-box/scripts/*.sh \
# The name ttyd is started under, and the only reason this link exists: ttyd
# tells its client the window title "<command> (<hostname>)", and the client
# appends that to the tab's own title. The command is therefore user-visible
# text in every terminal tab, and a full path to a shell script is not what
# anyone wants at the end of it. ep.sh runs the session launcher through here.
    && ln -s /opt/agent-box/scripts/launch_session.sh /usr/local/bin/agent-session

# Bake the content the image owns inside ~/.claude, laid out exactly as it lands there.
# ep.sh runs sync_claude_home.sh on every startup to copy this into the claude-data
# volume: a named volume is pre-populated from the image only while it is EMPTY, so a
# COPY alone would reach a fresh box once and never update an existing one.
# CLAUDE.md keeps its repo path (README.md and /workspace/CLAUDE.md both name it) and
# is mapped into the tree here.
COPY CLAUDE.md /opt/agent-box/claude-home/CLAUDE.md
COPY skills/ /opt/agent-box/claude-home/skills/

# Healthy = ttyd answers HTTP on 8091 (agent tabs) and the session list answers
# on 8090. No -f on the ttyd probe: it uses basic auth, so an unauthenticated
# request gets 401 — any HTTP response means the terminal is up. /healthz needs
# no auth, so it can be checked properly. Generous start period: ep.sh installs
# plugins first. These are the container-side ports, which are also the default
# host ports; test_ports.py pins them against ep.sh and docker-compose.yml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -s -o /dev/null http://localhost:8091 \
        && curl -sf -o /dev/null http://localhost:8090/healthz || exit 1

ENTRYPOINT ["/ep.sh"]