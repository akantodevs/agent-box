# agent-box

A containerized, self-driving **Claude Code** development environment. It runs Claude
Code as the `agent` service in a Docker Compose stack, exposes it through a web terminal,
and mounts the host Docker socket so the agent can build, run, and test the _other_
services in the same stack.

`agent-box` is **reusable**: include the prebuilt image (or this directory) in a real
project's compose file, mount that project at `/repo`, and you get an autonomous coding
agent that can develop the project — and its sibling services — from the inside.

---

## Ultra quick getting started

From zero to a working agent in about a minute:

1. **Make sure Docker is installed** (with Compose v2: `docker compose version`).

2. **Create `docker-compose.yml`** in your project directory (empty dirs work too —
   the agent can bootstrap a project from scratch). Use the filename
   `docker-compose.yml` exactly: the agent is instructed to operate on
   `/repo/docker-compose.yml`.

   ```yaml
   name: my-project # Compose project name; also used by the agent inside the box

   services:
     agent:
       image: ghcr.io/akantodevs/agent-box:latest
       container_name: agent-box
       init: true

       ports:
         - 7681:7681
       environment:
         TTYD_USER: "admin"
         TTYD_PASSWORD: "admin"
         CLAUDE_MODEL: "opus"
       volumes:
         - /var/run/docker.sock:/var/run/docker.sock
         - ./:/repo:z
         - claude-data:/home/claude/.claude

   volumes:
     claude-data:
   ```

3. **Run it:**

   ```bash
   docker compose up -d
   ```

   Open **http://localhost:7681** (login `admin` / `admin`), complete the one-time
   Claude login, and start delegating. The agent can take it from here: scaffold code
   in `/repo`, add new services to this same compose file, and build/start/test them
   itself through the mounted Docker socket.

For the full story (building locally, configuration, how it works), read on.

---

## What you get

- **Claude Code in a box** — runs as the non-root `claude` user inside a Debian
  container, accessed from your browser via a [ttyd](https://github.com/tsl0922/ttyd)
  web terminal on port **7681**.
- **Whole-stack control** — the host Docker socket is mounted in, so the agent drives the
  entire Compose stack (`docker compose ps / restart / logs / exec`) from inside the box.
- **Persistent sessions** — credentials, settings, and conversation transcripts live in
  the `claude-data` named volume, so they survive container restarts and rebuilds; on
  reconnect the agent resumes exactly where it left off (`claude --continue`).
- **Configurable model** — set the `CLAUDE_MODEL` env var (defaults to `opus`) to pick
  the model Claude Code launches with.
- **Plugins preinstalled** — anything listed in `agent-box/plugins.txt` (default:
  `superpowers`, `playwright`, and `frontend-design`) is installed _and enabled_
  idempotently on every start.
- **Browser automation built in** — the [Playwright MCP](https://github.com/microsoft/playwright-mcp)
  server and a matching headless Chromium are baked into the image, so the agent can
  drive web pages (navigate, click, fill forms, screenshot) to verify the UIs it
  builds.
- **Operating manual baked in** — `agent-box/CLAUDE.md` ships as the agent's global
  instructions, including the guardrails that keep it inside this stack.
- **Published image** — every push to `main` builds and pushes
  `ghcr.io/akantodevs/agent-box` via GitHub Actions, so consuming projects don't need a
  local checkout of this repo.

---

## Prerequisites

- **Docker** and **Docker Compose v2** (`docker compose ...`).
- Access to the host Docker socket at `/var/run/docker.sock` (Linux / Docker Desktop /
  WSL2 all work).
- A **Claude** account you can log into from a browser (the first-run login flow below
  uses it).

---

## Getting started

> These steps assume you're starting the box for the first time, from the repository
> root (the directory containing `docker-compose.yml`).

### 1. Build and start the box

```bash
docker compose up --build -d
```

This builds the `agent-box:latest` image and starts the `agent-box` container. Watch it
come up:

```bash
docker compose logs -f agent
```

### 2. Open the web terminal and log in

Browse to **http://localhost:7681** and authenticate to ttyd with the credentials from
`docker-compose.yml` (defaults: **`admin` / `admin`** — change these for anything beyond
local use).

On first run, Claude Code will prompt you to **log in**. Follow the prompt in the
terminal (it gives you a URL to open in your browser; authorize, then paste the code
back). The credentials are stored in the `claude-data` named volume, so you won't be
asked again on future starts — even after image rebuilds.

### 3. You're in

The agent starts in `/repo` with the session resumed. From here it can edit code, and run
`docker compose -f /repo/docker-compose.yml ...` to control the rest of the stack.

---

## Using it for a real project

`agent-box` develops whatever is mounted at `/repo`. There are two ways to include it:

### Option A — prebuilt image from ghcr.io (recommended)

Add the `agent` service to your project's `docker-compose.yml`, pulling the published
image instead of building locally:

```yaml
services:
  agent:
    image: ghcr.io/akantodevs/agent-box:latest
    container_name: agent-box
    init: true
    ports:
      - 7681:7681
    environment:
      TTYD_USER: "admin"
      TTYD_PASSWORD: "admin"
      CLAUDE_MODEL: "opus" # optional; opus/sonnet/fable or a full model id
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./:/repo:z
      - claude-data:/home/claude/.claude

volumes:
  claude-data:
```

Notes:

- `latest` is resolved at pull time — update with `docker compose pull agent`. A
  `sha-<commit>` tag is also published per build if you want to pin.
- The `claude-data` volume is **project-scoped** (`<project>_claude-data`), so each
  project logs in once and keeps its own conversation history. Don't share it between
  projects: transcripts are keyed to `/repo`, so a shared volume would make
  `claude --continue` resume another project's conversation.
- `container_name` and the host port are fixed, so only one agent-box runs at a time;
  change both if you need two projects up simultaneously.

### Option B — build from a local checkout

Point `build.context` at this repo instead of using `image:`:

```yaml
build:
  context: ../agent-box/agent-box # path to agent-box/ in your checkout
  dockerfile: Dockerfile
```

Everything else (ports, environment, volumes) is the same as Option A.

### Wiring up your services

1. Add your project's own services to the same `docker-compose.yml`. By convention, each
   service mounts its own subdirectory, `/repo/<service>` — so the agent editing
   `/repo/api` changes the code the `api` service runs.
2. Reach sibling services over the Compose network by **service name as hostname** (e.g.
   `http://api:8000`, `db:5432`), using each service's _internal_ port.
3. Set a `name:` at the top of the compose file. The agent runs
   `docker compose -f /repo/docker-compose.yml ...`, which reads `name:` from the file —
   so host and agent always target the same stack, no env vars needed.

---

## Configuration

All knobs are environment variables on the `agent` service in `docker-compose.yml`:

| Variable                      | Default           | Purpose                                                                                                           |
| ----------------------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------- |
| `TTYD_USER` / `TTYD_PASSWORD` | `admin` / `admin` | Web-terminal login. Change for anything beyond localhost.                                                         |
| `CLAUDE_MODEL`                | `opus`            | Model passed to `claude --model` at launch. Accepts an alias (`opus`, `sonnet`, `fable`, ...) or a full model id. |
| `DISABLE_PLAYWRIGHT`          | unset             | Set to `"true"` to disable the Playwright browser-automation plugin — useful when running agent-box for something other than web development. Clearing it re-enables the plugin on the next start. |

A default **status line** (model, git branch, context usage, plan usage, session cost)
ships in the image. To customize it, edit the `statusLine` entry in the volume's
`~/.claude/settings.json` (or run `/statusline` inside Claude Code) — the entrypoint
only sets the default when no `statusLine` is configured, so your changes stick.

---

## How it works

### Components

| Piece                                     | Role                                                                                                                                                                                                                                                                                                                                                                                      |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker-compose.yml`                      | Defines the `agent` service: build, the `agent-box:latest` image, port `7681`, env vars, and the volume mounts. The `name:` field pins the Compose project name.                                                                                                                                                                                                                          |
| `agent-box/Dockerfile`                    | Builds the image: Debian + Node.js + Claude Code CLI + docker CLI + ttyd + Playwright MCP with headless Chromium, plus everyday CLI tools (`ps`/`pkill`, `jq`, `less`, `nc`, `dig`, `unzip`, `wget`, `tree`, Python with pip/venv, ...), and creates the non-root `claude` user. Ships a healthcheck on port 7681. The Claude Code auto-updater is disabled — the image owns the version. |
| `agent-box/ep.sh`                         | Entrypoint (runs as **root**): fixes ownership, seeds first-run config, grants `claude` access to the Docker socket, installs plugins, then launches ttyd.                                                                                                                                                                                                                                |
| `agent-box/scripts/start_claude.sh`       | Launched per ttyd connection; runs `claude --model "$CLAUDE_MODEL" --continue` if a transcript exists, else starts fresh.                                                                                                                                                                                                                                                                 |
| `agent-box/scripts/install_plugins.sh`    | Idempotently installs **and enables** the plugins from `plugins.txt`.                                                                                                                                                                                                                                                                                                                     |
| `agent-box/scripts/statusline.js`         | Default Claude Code status line (model, git branch, context usage, plan usage, session cost). Wired into `settings.json` by `ep.sh` unless a `statusLine` is already configured.                                                                                                                                                                                                          |
| `agent-box/CLAUDE.md`                     | The agent's global operating manual + guardrails, refreshed into the volume on every start.                                                                                                                                                                                                                                                                                               |
| `.github/workflows/publish-agent-box.yml` | Builds the image on pushes to `main` touching `agent-box/**` and pushes `latest` + `sha-<commit>` tags to ghcr.io.                                                                                                                                                                                                                                                                        |

### Startup lifecycle

1. The container starts `ep.sh` as **root** (PID 1).
2. It `chown`s `/home/claude` and `/repo`, seeds onboarding-skip config (only files that
   don't already exist — `settings.json` lives in the volume and accumulates runtime
   state like plugin enablement, so it is never overwritten), and **grants the `claude`
   user access to the mounted Docker socket** by adding it to a group that matches the
   socket's GID (it never `chmod`s the socket itself, which would alter the host's
   inode).
3. It installs and enables plugins from `plugins.txt` as the `claude` user (idempotent).
4. It launches **ttyd** on port `7681`, which runs `start_claude.sh` as `claude` on each
   connection. ttyd is limited to a single client (`-m 1`) so only one
   `claude --continue` ever touches the transcript.

### Persistence

`/home/claude/.claude` is a named volume (`claude-data`): credentials, settings, plugins,
and conversation transcripts all survive restarts **and** rebuilds. On reconnect,
`start_claude.sh` finds the transcript and runs `claude --continue` to resume.
(Transcript folders are named after the working directory — `/repo` becomes `-repo`.)

### Docker access

The host socket is mounted at `/var/run/docker.sock`. The compose file's `name:` field
pins the Compose project name, and the agent always passes
`-f /repo/docker-compose.yml`, so the agent sees and controls the same stack the host
started — no environment coordination needed.

---

## Guardrails

The agent operates under the rules in `agent-box/CLAUDE.md`. In short:

- Stay inside this container and this Compose stack; don't touch the host or unrelated
  containers.
- **Never** stop, restart, rebuild, or remove the `agent` service — that's the agent's
  own container.
- No git operations (commits/branches/pushes are handled outside the box).
- Be careful with stateful services; don't wipe volumes or run destructive migrations
  against non-test datastores.
- Don't tear the stack down; restarting individual services to apply changes is fine.

---

## Security notes

This is a development convenience, not a sandbox. Treat it accordingly:

- **Docker socket = host root.** Anything that can reach `/var/run/docker.sock` can
  control the host's Docker daemon, which is root-equivalent on the host. On this socket
  the entrypoint adds `claude` to the `root` group to grant access.
- **Unrestricted permissions.** Claude Code runs with `--dangerously-skip-permissions`;
  it will not prompt before running commands.
- **Change the ttyd credentials.** `TTYD_USER` / `TTYD_PASSWORD` default to `admin` /
  `admin` in `docker-compose.yml`. Change them (and don't expose port 7681 publicly)
  before using this anywhere but localhost.
- The `claude-data` volume holds your live credentials and conversation history. Remove
  it (`docker volume rm`) only if you intend to wipe the login and all transcripts.

---

## Common tasks & troubleshooting

- **Apply a change to `Dockerfile`/`ep.sh`/scripts:** these are baked into the image, so
  rebuild and recreate — `docker compose up --build -d`. A plain `restart` reuses the old
  image.
- **Update Claude Code:** the in-container auto-updater is disabled (the npm global dir
  is root-owned and the version should be reproducible anyway). Rebuild the image to pull
  the latest CLI, or pin a version in the Dockerfile
  (`npm install -g @anthropic-ai/claude-code@<version>`).
- **Change the model:** set `CLAUDE_MODEL` on the `agent` service and recreate it. The
  next session launch picks it up.
- **Add a plugin:** add a line to `agent-box/plugins.txt`, then rebuild (or rerun
  `install_plugins.sh` inside the container as the `claude` user).
- **Update the image in a consuming project:** `docker compose pull agent`, then
  `docker compose up -d agent`. Pin a `sha-<commit>` tag instead of `latest` for
  reproducibility.
- **Start over with a fresh login/history:** stop the stack and remove the project's
  `claude-data` volume (this deletes credentials _and_ all transcripts).
- **Check what's running:** `docker compose -f /repo/docker-compose.yml ps`.
