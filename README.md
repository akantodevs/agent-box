# agent-box

A containerized, self-driving **Claude Code** environment. It runs Claude Code as the
`agent` service in a Docker Compose stack, exposes it through a web terminal, and
mounts the host Docker socket so the agent can build, run, observe, and test the
_other_ services in the same stack.

`agent-box` is **reusable**: include the prebuilt image (or this directory) in a
project's compose file, mount that project at `/workspace`, and you get an autonomous
agent working from the inside. Software development is the headline use case, but the
same box can run as a production log analyzer, exception triager, or any other role —
see [Beyond development](#beyond-development).

---

## Ultra quick getting started

From zero to a working agent in about a minute:

1. **Make sure Docker is installed** (with Compose v2: `docker compose version`).

2. **Create `docker-compose.yml`** in your project directory (empty dirs work too —
   the agent can bootstrap a project from scratch). Use the filename
   `docker-compose.yml` exactly: the agent is instructed to operate on
   `/workspace/docker-compose.yml`.

   ```yaml
   name: my-project # Compose project name; also used by the agent inside the box

   services:
     agent:
       image: ghcr.io/akantodevs/agent-box:latest
       container_name: agent-box
       init: true

       ports:
         - 8090:8090 # Agent sessions
         - 8091:8091 # Agent tabs
       environment:
         TTYD_USER: "admin"
         TTYD_PASSWORD: "admin"
         CLAUDE_MODEL: "opus"
       volumes:
         - /var/run/docker.sock:/var/run/docker.sock
         - ./:/workspace:z
         - claude-data:/home/claude/.claude

   volumes:
     claude-data:
   ```

3. **Run it:**

   ```bash
   docker compose up -d
   ```

   Open the **session administration page** at **http://localhost:8090** (login
   `admin` / `admin`) and click **+ New session** — it opens a terminal tab on
   http://localhost:8091. Complete the one-time Claude login there, and start
   delegating. The agent can take it from here: scaffold code in `/workspace`, add new
   services to this same compose file, and build/start/test them itself through the
   mounted Docker socket.

For the full story (building locally, configuration, how it works), read on.

---

## What you get

- **Claude Code in a box** — runs as the non-root `claude` user inside a Debian
  container, accessed from your browser via a [ttyd](https://github.com/tsl0922/ttyd)
  web terminal on container port **8091**.
- **Whole-stack control** — the host Docker socket is mounted in, so the agent drives the
  entire Compose stack (`docker compose ps / restart / logs / exec`) from inside the box.
- **Many sessions, one box** — a [session administration page](#sessions) on container
  port **8090** lists every conversation in the volume and opens each one in its own
  browser tab, so several jobs can run side by side.
- **Persistent sessions** — credentials, settings, and conversation transcripts live in
  the `claude-data` named volume, so they survive container restarts and rebuilds; pick
  a session from the admin page and it resumes exactly where it left off.
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

This builds the `agent-box:latest` image and starts the `agent-box-dev` container. Watch it
come up:

```bash
docker compose logs -f agent
```

### 2. Open the session page and log in

Browse to **http://localhost:8090** — the session administration page — and
authenticate with the credentials from `docker-compose.yml` (defaults: **`admin` /
`admin`** — change these for anything beyond local use). The same credentials guard the
agent tabs on **http://localhost:8091**.

> To run two boxes at once, set `AGENT_BOX_SESSION_LIST_PORT` and `AGENT_BOX_TABS_PORT`
> (in a `.env` file next to the compose file, or in the environment) rather than editing
> the `ports:` mapping. Each variable is used twice — once to publish the port, once to
> tell the container about it — so overriding the variable moves both, while editing
> only the mapping leaves the session list handing out links to the old port.

Click **+ New session** to open a terminal tab. On first run, Claude Code will prompt
you to **log in**. Follow the prompt in the terminal (it gives you a URL to open in your
browser; authorize, then paste the code back). The credentials are stored in the
`claude-data` named volume, so you won't be asked again on future starts — even after
image rebuilds.

### 3. You're in

The agent starts in `/workspace`. From here it can edit code, and run
`docker compose -f /workspace/docker-compose.yml ...` to control the rest of the stack.
Leave the tab and come back later: closing it stops that agent, but the session stays
listed on the admin page, and reopening it there resumes the conversation.

---

## Sessions

One box runs **as many Claude Code sessions as you open tabs** — one session per tab,
not one per container. The two servers divide the work: the admin page decides _which_
session a tab gets, the terminal runs it.

- **The admin page is the way in.** It lists every session in the `claude-data` volume,
  newest activity first, with its name, context size, entry count, and — for a running
  one — what it is currently doing. Clicking a row opens that session in a new browser
  tab; **+ New session** opens a fresh one. The page polls every five seconds, so a
  session you start in one tab shows up in the list on its own.
- **Opening the terminal port directly starts a _new_ session.** The terminal URL with
  no `?arg=` on it no longer resumes the most recent conversation — it begins an empty
  one. This is the biggest behavioural change from earlier versions of agent-box, which
  resumed a single, always-the-same conversation on every connect. To get back to an
  existing conversation, open it from the admin page (or keep its `?arg=<session-id>`
  URL: the tab's address is stable and bookmarkable).
- **A session can only be open in one tab.** Two Claude processes writing one transcript
  corrupt it, so the launcher refuses the second tab and says so. Running sessions are
  shown but not clickable; close the tab that holds one to get it back. (A browser
  refresh is fine — the launcher waits out the old process before taking over.)
- **Closing the tab ends the session.** The tab _is_ the session's terminal, so when it
  goes, Claude is asked to stop — and killed if it will not — together with the MCP
  servers it started. Nothing is lost: the transcript stays in the volume and the
  session reopens from the admin page where it left off. Work in flight does stop with
  the tab, so leave it open for a long autonomous run.
- **Names are read-only.** A session is named by the title Claude Code writes for it,
  falling back to its first real prompt, then to `(untitled)`. There is no rename; a
  session titles itself once the conversation has something to go on.
- **Browser tabs carry those names.** A session tab is called
  `Agent: <session name> · <AGENT_NAME>` and follows the session as it renames itself —
  a fresh one reads `Agent: new session` until Claude Code titles it, then changes in
  place. The admin page's own tab is `Sessions: <AGENT_NAME>`, so two boxes open at
  once stay apart. (Each tab's full title ends in ttyd's own `agent-session
(<hostname>)`; the part a tab shows you is the session name.)
- **Delete is permanent.** Deleting a session from the page removes its transcript from
  the volume — the conversation cannot be recovered, and there is no undo. A running
  session cannot be deleted at all.
- **Context is shown in tokens, not percent.** A transcript does not record the size of
  the context window it was written against, so any percentage would be measured against
  a guess. `120k ctx` is the absolute figure.
- **The entries count is transcript entries**, tool results included — it runs at
  roughly twice a human's idea of how many turns were taken. That is why it is not
  labelled "messages".

## Using it for a real project

`agent-box` develops whatever is mounted at `/workspace`. There are two ways to include it:

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
      - 8090:8090 # session list
      - 8091:8091 # Agent tabs
    environment:
      TTYD_USER: "admin"
      TTYD_PASSWORD: "admin"
      # Change ports used by the container
      # SESSION_LIST_PUBLIC_PORT: 8090
      # AGENT_TABS_PUBLIC_PORT: 8091
      CLAUDE_MODEL: "opus" # optional; opus/sonnet/fable or a full model id
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./:/workspace:z
      - claude-data:/home/claude/.claude

volumes:
  claude-data:
```

Notes:

- `latest` is resolved at pull time — update with `docker compose pull agent`. A
  `sha-<commit>` tag is also published per build if you want to pin.
- The `claude-data` volume is **project-scoped** (`<project>_claude-data`), so each
  project logs in once and keeps its own conversation history. Don't share it between
  projects: the admin page lists every session in the volume, so a shared one would
  offer another project's conversations alongside this project's.
- `container_name` is fixed, so only one agent-box runs at a time; change it — and set
  `AGENT_BOX_SESSION_LIST_PORT` / `AGENT_BOX_TABS_PORT` to free ports — if you need two
  projects up simultaneously.

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
   service mounts its own subdirectory, `/workspace/<service>` — so the agent editing
   `/workspace/api` changes the code the `api` service runs.
2. Reach sibling services over the Compose network by **service name as hostname** (e.g.
   `http://api:8000`, `db:5432`), using each service's _internal_ port.
3. Set a `name:` at the top of the compose file. The agent runs
   `docker compose -f /workspace/docker-compose.yml ...`, which reads `name:` from the file —
   so host and agent always target the same stack, no env vars needed.

---

## Beyond development

The baked-in manual defines the _environment and guardrails_; each deployment defines
its _role_ by placing a `CLAUDE.md` in the mounted workspace (Claude Code reads
`/workspace/CLAUDE.md` automatically as project instructions). That makes the same
image useful for non-coding jobs:

- **Production log analyzer / exception triager** — run agent-box alongside a
  production stack; the agent reads sibling-service logs (`docker compose logs`),
  diagnoses exceptions, and writes triage reports into `/workspace`. The manual's
  operational rules make it observe-first: it reports and recommends rather than
  restarting things, unless your workspace `CLAUDE.md` explicitly authorizes actions.
- **Data analysis station** — mount CSVs, dumps, or exports into `/workspace`;
  Python (with pip/venv), `jq`, and `sqlite3`-style tooling via service containers
  cover most workflows.
- **Ops sidekick** — health summaries, config audits, certificate-expiry checks
  across the stack, on demand from the web terminal.

Tips for non-development deployments:

- Set the role in `<project>/CLAUDE.md` (mounted at `/workspace/CLAUDE.md`): what the
  agent is for, what it may and may not touch, where to write reports.
- Set `DISABLE_PLAYWRIGHT: "true"` if no browser automation is needed.
- For observe-only roles, consider **not** mounting the Docker socket — the agent
  then sees only what's in `/workspace` (e.g. bind-mounted log directories, ideally
  read-only: `- ./logs:/workspace/logs:ro`).
- Treat the web terminal as production access: strong `TTYD_USER`/`TTYD_PASSWORD`,
  and never expose either port — terminal or admin page — publicly.

---

## Configuration

All knobs are environment variables on the `agent` service in `docker-compose.yml`:

| Variable                      | Default            | Purpose                                                                                                                                                                                                                                                                                                                                                                                                 |
| ----------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENT_NAME`                  | container name     | What this box is called in browser tabs: the session administration page is titled `Sessions: <name>` and every session tab ends in it, which is how two agent-boxes open at once stay apart. Unset, the container's own name is used (asked of Docker over the mounted socket), falling back to the hostname — so set it whenever the container name is not what you want to read in a tab.            |
| `TTYD_USER` / `TTYD_PASSWORD` | `admin` / `admin`  | Login for the web terminal **and** the session administration page. Change for anything beyond localhost.                                                                                                                                                                                                                                                                                               |
| `AGENT_TABS_PUBLIC_PORT`      | `8091`             | The **host** port the agent tabs are published on (the left half of the `8091:8091` mapping). The container can't discover this itself, and the session list needs it to build its links — so it must match the `ports:` entry, or every link on the page points at the wrong port. In the shipped compose files both come from `AGENT_BOX_TABS_PORT`, so overriding that variable moves them together. |
| `SESSION_LIST_PUBLIC_PORT`    | `8090`             | The host port the session list is published on (from `AGENT_BOX_SESSION_LIST_PORT`). Only used for the "Container ready" line in the container log; the page itself works regardless.                                                                                                                                                                                                                   |
| `CLAUDE_MODEL`                | `opus`             | Model passed to `claude --model` at launch. Accepts an alias (`opus`, `sonnet`, `fable`, ...) or a full model id.                                                                                                                                                                                                                                                                                       |
| `DISABLE_PLAYWRIGHT`          | unset              | Set to `"true"` to disable the Playwright browser-automation plugin — useful when running agent-box for something other than web development. Clearing it re-enables the plugin on the next start.                                                                                                                                                                                                      |
| `ALLOW_TERRAFORM_MODIFY`      | `Ask`<sup>\*</sup> | Whether the agent may run infrastructure-mutating Terraform (`apply`, `destroy`, `import`, `state rm`/`mv`, `taint`, ...). `No` blocks, `Ask` prompts once per terraform directory then remembers it, `Yes` runs freely. Read-only commands always run. <sup>\*</sup>Shipped as `Ask` in `docker-compose.yml`; if unset/unrecognized the guard fails **closed** (blocks).                               |
| `REMOTE_CONTROL_NAME`         | unset              | When set, Claude Code launches with `--remote-control <name>-<suffix>`, enabling Remote Control and naming the session. Set the **base** name; each session appends its own suffix (its slugified title, or the head of its id) so concurrent sessions stay distinguishable. Leave unset to keep Remote Control off (the default).                                                                      |

A default **status line** (model, git branch, context usage, plan usage, session cost)
ships in the image. To customize it, edit the `statusLine` entry in the volume's
`~/.claude/settings.json` (or run `/statusline` inside Claude Code) — the entrypoint
only sets the default when no `statusLine` is configured, so your changes stick.

---

## How it works

### Components

| Piece                                     | Role                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker-compose.yml`                      | Defines the `agent` service: build, the `agent-box:latest` image, the two published ports (session list `8090`, agent tabs `8091` — each written once as a variable and used both to publish and to tell the container), env vars, and the volume mounts. The `name:` field pins the Compose project name.                                                                                                                                                                                                                          |
| `agent-box/Dockerfile`                    | Builds the image: Debian + Node.js + Claude Code CLI + docker CLI + Terraform + kubectl + ttyd + Playwright MCP with headless Chromium, plus everyday CLI tools (`ps`/`pkill`, `jq`, `less`, `nc`, `dig`, `unzip`, `wget`, `tree`, Python with pip/venv, ...), and creates the non-root `claude` user. Ships a healthcheck that probes **both** servers (8091 and `/healthz` on 8090). The Claude Code auto-updater is disabled — the image owns the version.                                                                       |
| `agent-box/ep.sh`                         | Entrypoint (runs as **root**): fixes ownership, seeds first-run config, grants `claude` access to the Docker socket, installs plugins, resolves the box's name (`agent_name.sh`), then launches ttyd and — in a restart loop, so a crash there never costs you the terminal — the session administration server.                                                                                                                                                                                                                    |
| `agent-box/scripts/launch_session.sh`     | ttyd's entry point for every browser tab. Validates the session id the browser passed as `?arg=` — session-id format, a transcript that exists, and no live process holding it — before handing off to `start_claude.sh` under `su - claude`. Fails closed: only a clean "not live" answer permits a resume, so a second tab on a running session is refused rather than allowed to corrupt the transcript.                                                                                                                         |
| `agent-box/scripts/start_claude.sh`       | Runs the Claude Code process for one tab: `claude --model "$CLAUDE_MODEL" --resume <id>`, or `--session-id <fresh uuid>` for a new session. Appends `--remote-control <name>-<suffix>` when `REMOTE_CONTROL_NAME` is set. Also starts the tab-title watcher alongside it.                                                                                                                                                                                                                                                           |
| `agent-box/scripts/sessions.py`           | The session administration server on container port `8090` (stdlib only, runs as `claude`): the page itself at `/`, `GET /api/sessions`, `POST /api/sessions/<uuid>/delete`, and an unauthenticated `/healthz` for the healthcheck. Everything else is behind the same basic-auth credentials as ttyd.                                                                                                                                                                                                                              |
| `agent-box/scripts/session_store.py`      | Read-only discovery of sessions from Claude Code's own state files — transcripts under `~/.claude/projects/`, live processes from `~/.claude/sessions/`. Stores no state of its own. Also the `--is-live` / `--slug` helper the two shell scripts call. Its only mutating function is the transcript delete.                                                                                                                                                                                                                        |
| `agent-box/scripts/agent_name.sh`         | Resolves what this box is called, once at boot: `AGENT_NAME` if the operator set one, else the container's name from `docker inspect` over the mounted socket, else the hostname. Never fails — an unnamed box costs a tab title, not a boot.                                                                                                                                                                                                                                                                                       |
| `agent-box/scripts/session_title.py`      | Names the browser tab. Started per session by `start_claude.sh`, it writes `Agent: <session name>` to the terminal as an OSC title and rewrites it whenever the session renames itself, then ends with the Claude process it was started from.                                                                                                                                                                                                                                                                                      |
| `agent-box/scripts/sessions_page.html`    | The admin page's UI. `sessions.py` bakes `AGENT_TABS_PUBLIC_PORT` into it at startup so the rows can link to the agent tabs, and `AGENT_NAME` as the page's browser-tab title.                                                                                                                                                                                                                                                                                                                                                      |
| `agent-box/scripts/install_plugins.sh`    | Idempotently installs **and enables** the plugins from `plugins.txt`.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `agent-box/scripts/statusline.js`         | Default Claude Code status line (model, git branch, context usage, plan usage, session cost). Wired into `settings.json` by `ep.sh` unless a `statusLine` is already configured.                                                                                                                                                                                                                                                                                                                                                    |
| `agent-box/scripts/terraform-guard.js`    | `PreToolUse`/`PostToolUse` hook enforcing `ALLOW_TERRAFORM_MODIFY` (`No`/`Ask`/`Yes`; fail-closed) for infrastructure- or state-mutating Terraform (`apply`, `destroy`, `import`, `state rm`/`mv`, `taint`, ...); read-only commands (`plan`, `validate`, `show`, ...) pass through. In `Ask` mode it prompts once per terraform directory and remembers it (so stage vs prod ask separately), persisting approvals in `~/.claude/terraform-approvals.json`. Registered for both events idempotently in `settings.json` by `ep.sh`. |
| `agent-box/CLAUDE.md`                     | The agent's global operating manual + guardrails, refreshed into the volume on every start.                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `agent-box/skills/`                       | Skills baked into the image and synced to `~/.claude/skills/` on every start, so every deployment has them offline. Project-specific skills belong in the workspace at `/workspace/.claude/skills/` instead, where Claude Code reads them in place.                                                                                                                                                                                                                                                                                 |
| `agent-box/scripts/sync_claude_home.sh`   | Mirrors the baked `~/.claude` content (manual, skills) into the `claude-data` volume on every start. Needed because a named volume is pre-populated from the image only while empty — afterwards the volume wins, so a `COPY` alone would never reach an existing box. Image wins for what it ships; anything else in the volume is untouched; content dropped from a later image is removed.                                                                                                                                       |
| `.github/workflows/publish-agent-box.yml` | Builds the image on pushes to `main` touching `agent-box/**` and pushes `latest` + `sha-<commit>` tags to ghcr.io.                                                                                                                                                                                                                                                                                                                                                                                                                  |

### Startup lifecycle

1. The container starts `ep.sh` as **root** (PID 1).
2. It syncs the image-owned `~/.claude` content — the operating manual and any baked
   skills — into the `claude-data` volume via `sync_claude_home.sh`. It `chown`s
   `/home/claude` and `/workspace`, seeds onboarding-skip config (only files that
   don't already exist — `settings.json` lives in the volume and accumulates runtime
   state like plugin enablement, so it is never overwritten), and **grants the `claude`
   user access to the mounted Docker socket** by adding it to a group that matches the
   socket's GID (it never `chmod`s the socket itself, which would alter the host's
   inode).
3. It installs and enables plugins from `plugins.txt` as the `claude` user (idempotent).
4. It launches **ttyd** on port `8091` with `-a` (so a tab can name its session as
   `?arg=<session-id>`) and no client limit. Every connection runs
   `launch_session.sh` (through its `agent-session` alias), which validates that id and
   then starts one Claude Code process as `claude`. There is no global "one client" cap
   any more — the rule is per session: one tab per conversation, enforced by the
   launcher. ttyd runs with **no fixed title**, so each session can name its own browser
   tab.
5. It launches the **session administration server** on port `8090` as `claude`,
   supervised by a restart loop, and then tails the container log.

### Persistence

`/home/claude/.claude` is a named volume (`claude-data`): credentials, settings, plugins,
and conversation transcripts all survive restarts **and** rebuilds. Each session is one
transcript file there, and the admin page is a view of that directory — nothing about a
session is stored anywhere else, so a session survives exactly as long as its transcript
does. (Transcript folders are named after the working directory — `/workspace` becomes
`-workspace`.) Reopening a session from the page runs `claude --resume <id>`.

### Docker access

The host socket is mounted at `/var/run/docker.sock`. The compose file's `name:` field
pins the Compose project name, and the agent always passes
`-f /workspace/docker-compose.yml`, so the agent sees and controls the same stack the host
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
  `admin` in `docker-compose.yml`. Change them before using this anywhere but localhost.
- **Two ports, both sensitive.** The same credentials guard the terminal and the session
  administration page; expose neither publicly. The terminal is a root-capable shell by
  proxy, and the admin page can **permanently delete conversations** — anyone who
  reaches it can destroy transcripts that have no backup.
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
- **"Refusing to resume: session ... is already open in another tab":** that session is
  running. Find the tab that holds it and close it, then reopen the session from the
  admin page.
- **"Refusing to resume: no transcript for ...":** the session list handed out a link to
  a port that is not this box's agent tabs — usually another agent-box, which is asked
  for a session it has never heard of. `AGENT_TABS_PUBLIC_PORT` doesn't match the host
  port the tabs are published on in `ports:`. Set both from `AGENT_BOX_TABS_PORT` as the
  compose files above do, and recreate the service — the port is baked into the page when
  the server starts. (The same message is genuine when the transcript really is gone,
  e.g. deleted from the session list or aged out by Claude Code's own cleanup.)
- **Add a plugin:** add a line to `agent-box/plugins.txt`, then rebuild (or rerun
  `install_plugins.sh` inside the container as the `claude` user).
- **Update the image in a consuming project:** `docker compose pull agent`, then
  `docker compose up -d agent`. Pin a `sha-<commit>` tag instead of `latest` for
  reproducibility. **Coming from an image published before the port change**, also
  update the service's `ports:` and `environment:` to the block shown in
  [Option A](#option-a--prebuilt-image-from-ghcrio-recommended): the servers now listen
  on `8090`/`8091` inside the container, and `TTYD_PUBLIC_PORT` / `ADMIN_PUBLIC_PORT`
  are no longer read. An un-updated mapping publishes to a container port nothing
  listens on, so the box fails to answer rather than serving wrong links.
- **Start over with a fresh login/history:** stop the stack and remove the project's
  `claude-data` volume (this deletes credentials _and_ all transcripts).
- **Check what's running:** `docker compose -f /workspace/docker-compose.yml ps`.
