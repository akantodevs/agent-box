# agent-box

A containerized, self-driving **Claude Code** development environment. It runs Claude
Code as the `agent` service in a Docker Compose stack, exposes it through a web terminal,
and mounts the host Docker socket so the agent can build, run, and test the *other*
services in the same stack.

`agent-box` is a **reusable template**: drop it alongside a real project, mount that
project at `/repo`, and you get an autonomous coding agent that can develop the project —
and its sibling services — from the inside.

---

## What you get

- **Claude Code in a box** — runs as the non-root `claude` user inside a Debian
  container, accessed from your browser via a [ttyd](https://github.com/tsl0922/ttyd)
  web terminal on port **7681**.
- **Whole-stack control** — the host Docker socket is mounted in, so the agent drives the
  entire Compose stack (`docker compose ps / restart / logs / exec`) from inside the box.
- **Persistent sessions** — your conversation survives container restarts and rebuilds;
  on reconnect the agent resumes exactly where it left off (`claude --continue`).
- **Plugins preinstalled** — anything listed in `agent-box/plugins.txt` (default:
  `superpowers`) is installed idempotently on every start.
- **Operating manual baked in** — `agent-box/CLAUDE.md` ships as the agent's global
  instructions, including the guardrails that keep it inside this stack.

---

## Prerequisites

- **Docker** and **Docker Compose v2** (`docker compose ...`).
- Access to the host Docker socket at `/var/run/docker.sock` (Linux / Docker Desktop /
  WSL2 all work).
- A **Claude** account you can log into from a browser (the first-run login flow below
  uses it).

---

## Getting started

> These steps assume you're starting the box for the first time. Run them from the
> repository root (the directory containing `docker-compose.yml`).

### 1. Set the project name

Edit `.env` and set the Compose project name. For a real project, use that project's
name — it keeps the host and the in-container agent pointed at the **same** stack.

```env
# .env
COMPOSE_PROJECT_NAME=agent-box
```

### 2. Create the credentials placeholder  ⚠️ required

The compose file bind-mounts a single file for Claude Code credentials:

```
./.claude_session/.claude_credentials.json  ->  /home/claude/.claude/.credentials.json
```

Docker requires the source to **exist before the first `up`**. If it doesn't, Docker
silently creates a *directory* there and your login can never persist. Create an empty
file up front:

```bash
mkdir -p .claude_session/projects
touch .claude_session/.claude_credentials.json
```

> `.claude_session/` is gitignored (it's runtime state, not source), so a fresh clone
> won't have it — this step is needed on every new checkout.

### 3. Build and start the box

```bash
docker compose up --build -d
```

This builds the `agent-box:latest` image and starts the `agent-box` container. Watch it
come up:

```bash
docker compose logs -f agent
```

### 4. Open the web terminal and log in

Browse to **http://localhost:7681** and authenticate to ttyd with the credentials from
`docker-compose.yml` (defaults: **`admin` / `admin`** — change these for anything beyond
local use).

On first run, the mounted credentials file is empty, so Claude Code will prompt you to
**log in**. Follow the prompt in the terminal (it gives you a URL to open in your
browser; authorize, then paste the code back). The credentials are written through the
mount to `.claude_session/.claude_credentials.json`, so you won't be asked again on
future starts.

### 5. You're in

The agent starts in `/repo` with the session resumed. From here it can edit code, and run
`docker compose -f /repo/docker-compose.yml ...` to control the rest of the stack.

---

## Using it for a real project

`agent-box` develops whatever is mounted at `/repo`. To use it on a real project:

1. Place the `agent-box/` directory, `docker-compose.yml`, `.env`, and `.gitignore`
   alongside (or inside) your project so your project's code is what gets mounted at
   `/repo` (see the `./:/repo` volume).
2. Set `COMPOSE_PROJECT_NAME` in `.env` to your project's name.
3. Add your project's own services to `docker-compose.yml`. By convention, each service
   mounts its own subdirectory, `/repo/<service>` — so the agent editing `/repo/api`
   changes the code the `api` service runs.
4. Reach sibling services over the Compose network by **service name as hostname** (e.g.
   `http://api:8000`, `db:5432`), using each service's *internal* port.

---

## How it works

### Components

| Piece | Role |
|------|------|
| `docker-compose.yml` | Defines the `agent` service: build, the `agent-box:latest` image, port `7681`, ttyd creds, and the volume mounts. |
| `agent-box/Dockerfile` | Builds the image: Debian + Node.js + Claude Code CLI + docker CLI + ttyd, and creates the non-root `claude` user. |
| `agent-box/ep.sh` | Entrypoint (runs as **root**): fixes ownership, grants `claude` access to the Docker socket, installs plugins, then launches ttyd. |
| `agent-box/scripts/start_claude.sh` | Launched per ttyd connection; runs `claude --continue` if a transcript exists, else starts fresh. |
| `agent-box/scripts/install_plugins.sh` | Idempotently installs the plugins from `plugins.txt`. |
| `agent-box/CLAUDE.md` | The agent's global operating manual + guardrails. |
| `.env` | Single source of truth for `COMPOSE_PROJECT_NAME`. |

### Startup lifecycle

1. The container starts `ep.sh` as **root** (PID 1).
2. It `chown`s `/home/claude` and `/repo`, writes onboarding-skip config, and **grants
   the `claude` user access to the mounted Docker socket** by adding it to a group that
   matches the socket's GID (it never `chmod`s the socket itself, which would alter the
   host's inode).
3. It installs plugins from `plugins.txt` as the `claude` user (idempotent).
4. It launches **ttyd** on port `7681`, which runs `start_claude.sh` as `claude` on each
   connection. ttyd is limited to a single client (`-m 1`) so only one
   `claude --continue` ever touches the transcript.

### Persistence

`/home/claude/.claude/projects` is bind-mounted to `./.claude_session/projects`, so
conversation transcripts live on the host and survive restarts **and** rebuilds. On
reconnect, `start_claude.sh` finds the transcript and runs `claude --continue` to resume.
(Transcript folders are named after the working directory — `/repo` becomes `-repo`.)

### Docker access

The host socket is mounted at `/var/run/docker.sock`. Because the in-container compose
file lives at `/repo`, its default project name would be `repo` — *different* from the
host's. `COMPOSE_PROJECT_NAME` in `.env` (read by Compose on both sides) pins both to the
same project, so the agent sees and controls the host-started stack.

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
- `.claude_session/` holds your live credentials and conversation history — it's
  gitignored; keep it out of version control.

---

## Common tasks & troubleshooting

- **Apply a change to `Dockerfile`/`ep.sh`/scripts:** these are baked into the image, so
  rebuild and recreate — `docker compose up --build -d`. A plain `restart` reuses the old
  image.
- **The credentials path became a directory:** you skipped step 2. Stop the stack,
  remove the directory, `touch` the file, and `up` again:
  `docker compose down && rm -rf .claude_session/.claude_credentials.json && touch .claude_session/.claude_credentials.json`.
- **Add a plugin:** add a line to `agent-box/plugins.txt`, then rebuild (or rerun
  `install_plugins.sh` inside the container as the `claude` user).
- **Rename the project / image:** set `COMPOSE_PROJECT_NAME` in `.env`; the image is
  pinned to `agent-box:latest` in `docker-compose.yml`.
- **Check what's running:** `docker compose -f /repo/docker-compose.yml ps`.
