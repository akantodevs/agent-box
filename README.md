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
- **Git & GitHub ready** — `git` and the GitHub CLI (`gh`) are installed; give the box
  a `GH_TOKEN` and both are authenticated. Whether the agent may commit, push and open
  PRs is up to you (`ALLOW_GIT_WRITE`, off by default) — see [GitHub access](#github-access).
- **Operating manual baked in** — `agent-box/CLAUDE.md` ships as the agent's global
  instructions, including the guardrails that keep it inside this stack.
- **Published image** — every push to `main` builds and pushes
  `ghcr.io/akantodevs/agent-box` via GitHub Actions, so consuming projects don't need a
  local checkout of this repo. It is multi-arch (`linux/amd64` and `linux/arm64`), so it
  runs natively on Apple Silicon Macs as well as x86 Linux/Windows hosts.

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

Browse to the session administration page and authenticate with the credentials from
`docker-compose.yml` (defaults: **`admin` / `admin`** — change these for anything beyond
local use). The same credentials guard the agent tabs.

This repository's own compose file publishes the two servers on **8095** (session list)
and **8096** (agent tabs), leaving the default ports free for a second box; the snippet
under [Using it for a real project](#using-it-for-a-real-project) uses **8090** and
**8091**. Check the `ports:` entries in the compose file you started.

> **Changing the ports.** Each surface is named twice — once in `ports:` as
> `<host>:<container>`, once in `environment:` as `SESSION_LIST_PUBLIC_PORT` /
> `AGENT_TABS_PUBLIC_PORT` — because the container cannot see the host side of a
> mapping and the session list has to be told which port to put in its links. Change
> both together, or the page hands out links to a port this box does not answer on.
> `agent-box/tests/test_ports.py` fails if they disagree. The container-side ports
> (8090 and 8091) are fixed by the image; only the host side is yours to choose.

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
      # Optional GitHub access — see "GitHub access" below
      # (GH_TOKEN comes from .credentials via env_file below)
      # GIT_USER_NAME: "agent-box"
      # GIT_USER_EMAIL: "agent-box@example.com"
      # ALLOW_GIT_WRITE: "No"
    # Optional secrets file (GH_TOKEN=...); keep it out of version control
    # env_file:
    #   - path: .credentials
    #     required: false
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./:/workspace:z
      - claude-data:/home/claude/.claude

volumes:
  claude-data:
```

Notes:

- `latest` is resolved at pull time — update with `docker compose pull agent`. To pin,
  use a release version (`v1.8.0`, named like the repo's git tags) or the `sha-<commit>`
  tag published per build; a version is the same image as its commit's `sha-` tag.
- The image is published for `linux/amd64` and `linux/arm64`; Docker pulls the variant
  matching the host, so Apple Silicon (M1 and later) Macs run it natively without
  Rosetta emulation. No `platform:` key is needed.
- The `claude-data` volume is **project-scoped** (`<project>_claude-data`), so each
  project logs in once and keeps its own conversation history. Don't share it between
  projects: the admin page lists every session in the volume, so a shared one would
  offer another project's conversations alongside this project's.
- `container_name` is fixed, so only one agent-box runs at a time; change it — and move
  both surfaces to free host ports (the `ports:` mapping *and* the matching
  `SESSION_LIST_PUBLIC_PORT` / `AGENT_TABS_PUBLIC_PORT` entry) — if you need two projects
  up simultaneously.

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
| `AGENT_TABS_PUBLIC_PORT`      | `8091`             | The **host** port the agent tabs are published on (the left half of its `ports:` mapping; this repo's own compose file uses `8096`). The container can't discover this itself, and the session list needs it to build its links — so it must match the `ports:` entry, or every link on the page points at the wrong port. Change it and the `ports:` entry together. |
| `SESSION_LIST_PUBLIC_PORT`    | `8090`             | The host port the session list is published on (the left half of its `ports:` mapping; this repo's own compose file uses `8095`). Only used for the "Container ready" line in the container log; the page itself works regardless.                                                                                                                                                                                                                   |
| `CLAUDE_MODEL`                | `opus`             | Model passed to `claude --model` at launch. Accepts an alias (`opus`, `sonnet`, `fable`, ...) or a full model id.                                                                                                                                                                                                                                                                                       |
| `DISABLE_PLAYWRIGHT`          | unset              | Set to `"true"` to disable the Playwright browser-automation plugin — useful when running agent-box for something other than web development. Clearing it re-enables the plugin on the next start.                                                                                                                                                                                                      |
| `STARTUP_AUTO_UPDATE`         | `No`               | Whether to update the Claude Code CLI to the newest published version on every container start, before any session is launched. Only `Yes` (any case) enables it; unset or anything else keeps the version baked into the image. Off by default: it adds a sizeable download to every start and lets a running box drift from its image tag. The in-session auto-updater stays off either way — every session in the box shares one install, so startup is the only moment it can be replaced safely. A failed update is logged and the box starts on the image's version. |
| `ALLOW_TERRAFORM_MODIFY`      | `Ask`<sup>\*</sup> | Whether the agent may run infrastructure-mutating Terraform (`apply`, `destroy`, `import`, `state rm`/`mv`, `taint`, ...). `No` blocks, `Ask` prompts once per terraform directory then remembers it, `Yes` runs freely. Read-only commands always run. <sup>\*</sup>Shipped as `Ask` in `docker-compose.yml`; if unset/unrecognized the guard fails **closed** (blocks).                               |
| `REMOTE_CONTROL_NAME`         | unset              | When set, Claude Code launches with `--remote-control <name>-<suffix>`, enabling Remote Control and naming the session. Set the **base** name; each session appends its own suffix (its slugified title, or the head of its id) so concurrent sessions stay distinguishable. Leave unset to keep Remote Control off (the default).                                                                      |
| `GH_TOKEN`                    | unset              | GitHub token for `git` and `gh` (see [GitHub access](#github-access)). `gh` reads it directly; git uses it through `gh auth git-credential`, so it is never written to disk. Supply it through a gitignored `.credentials` file loaded with `env_file:` — never in the compose file itself. Unset leaves GitHub unauthenticated (public reads still work). |
| `SOPS_AGE_KEY` / `SOPS_AGE_KEY_FILE` | unset          | The age private key `sops` decrypts with (see [sops secrets](#sops-secrets)), either the key itself or the path to a mounted key file. Supply it through `.credentials`, like `GH_TOKEN`. Unset, `sops` can still encrypt to a public key but cannot decrypt. |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | unset              | Commit identity, written to the `claude` user's `~/.gitconfig` at startup. Unset, git refuses to commit. |
| `ALLOW_GIT_WRITE`             | `No`<sup>\*</sup>  | Whether the agent may run git/gh write operations (`commit`, `push`, `pull`, `merge`, `rebase`, `reset`, `checkout`/`switch`, creating branches or tags, `gh pr create`, ...). `No` blocks them and the agent leaves changes in the working tree; `Yes` allows them, and the agent works on a branch and opens a PR. Reads (`status`, `diff`, `log`, `fetch`, `clone`, `gh pr view`, ...) always run. <sup>\*</sup>Shipped as `No`; if unset/unrecognized the guard fails **closed** (blocks). |

### GitHub access

`git` and `gh` are always installed. To let them talk to GitHub as you, create a
**fine-grained personal access token** and hand it to the box as `GH_TOKEN`:

1. On GitHub, go to **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**
   ([direct link](https://github.com/settings/personal-access-tokens/new)).
2. Give it a name and an **expiration**, and pick the **resource owner** — your user,
   or the organization that owns the repositories (an organization may have to
   approve the token before it works).
3. Under **Repository access**, choose **Only select repositories** and pick just the
   repositories this box works on.
4. Under **Repository permissions**, grant:

   | Permission    | `ALLOW_GIT_WRITE: "No"` | `ALLOW_GIT_WRITE: "Yes"` | Used for                                              |
   | ------------- | ----------------------- | ------------------------ | ----------------------------------------------------- |
   | Metadata      | Read (always required)  | Read (always required)   | Everything — GitHub adds it automatically             |
   | Contents      | Read                    | Read and write           | `git clone`/`fetch`; `git push`                        |
   | Pull requests | Read                    | Read and write           | `gh pr view/list/diff`; `gh pr create`                 |
   | Issues        | Read                    | Read and write           | `gh issue view/list`; labels, assignees and milestones on a PR (`gh pr create --label ...`), commenting on or filing issues |
   | Commit statuses | Read (optional)       | Read (optional)          | Status checks from external CI in `gh pr checks`      |
   | Actions       | Read (optional)         | Read (optional)          | `gh run list/view` to check CI                         |
   | Workflows     | —                       | Read and write, only if the agent may change `.github/workflows/` | Pushing commits that touch workflow files |

   Leave everything else at **No access**.
5. Generate the token, copy it, and put it in a `.credentials` file next to
   `docker-compose.yml`, one `KEY=value` per line. Keep it out of version control;
   this repo's `.gitignore` already does:

   ```bash
   GH_TOKEN=github_pat_...
   ```

   The compose file loads it with `env_file:` (`path: .credentials`,
   `required: false`, so the box still starts without it). Don't also list
   `GH_TOKEN` under `environment:`: an entry there overrides `env_file` and would
   blank the token. Recreate the container (`docker compose up -d`) for a new or
   rotated token to take effect.

Set `GIT_USER_NAME` / `GIT_USER_EMAIL` if the agent should commit, and
`ALLOW_GIT_WRITE: "Yes"` to allow it to push branches and open pull requests. The
token's scope is the real limit on what the agent can reach, so keep it to the
repositories and permissions it needs; a classic token with the `repo` scope works too,
but grants access to every repository you can reach (and needs the `workflow` scope as
well to push changes under `.github/workflows/`).

The token lands in two places the agent can read: the `.credentials` file (inside the
`/workspace` mount) and the container's own configuration (`docker inspect` over the
mounted socket). That is inherent to giving it a token at all — which is why its
scope is the real boundary. `~/.gitconfig` is rebuilt from these variables on every
start, so a `git config --global` change made inside the box lasts only until the
next restart.

### sops secrets

`sops` and `age` are installed for encrypted secrets kept in git. To let the box
decrypt them, give it its **own** age key, so it can be revoked without touching
anyone else's:

1. On the host, outside the repo: `age-keygen -o ~/agent-box-sops.txt`. It prints the
   public key (`age1...`); the file holds the private key (`AGE-SECRET-KEY-1...`).
2. Add the private key to `.credentials`:

   ```bash
   echo "SOPS_AGE_KEY=$(grep '^AGE-SECRET-KEY-' ~/agent-box-sops.txt)" >> .credentials
   ```

   Or mount the key file read-only and set `SOPS_AGE_KEY_FILE` to its path in the
   container instead.
3. Recreate the container (`docker compose up -d`).
4. In the repo holding the secrets, add the **public** key to the recipients in
   `.sops.yaml` and run `sops updatekeys <file>` for each existing secret file.

Revoke the box by removing its public key from `.sops.yaml` and running
`sops updatekeys` again; secrets it could already read should then be rotated. As with
`GH_TOKEN`, the agent can read the key — that is what lets it decrypt.

#### Infrastructure secrets: `secrets` and `tf`

Two commands on `PATH` wrap sops for Terraform roots kept under
`/workspace/infrastructure/<env>/` (e.g. `stage`, `production`). An environment is any
such directory holding a sops config `.sops-infra.yaml` and a dotenv file
`secrets.enc.env` encrypted with it:

```bash
secrets stage edit                       # edit the secrets in $EDITOR
secrets stage exec -- terraform plan     # run any command with them in its environment
secrets stage updatekeys                 # re-encrypt after changing the recipients
cd /workspace/infrastructure/stage/3-app && tf plan   # terraform with stage's secrets
```

`tf` picks the environment from the directory it runs in; outside an environment
(including shared dirs like `infrastructure/modules`) it is plain `terraform`. A
directory with `secrets.enc.env` but no `.sops-infra.yaml` is treated as a broken
environment: `tf` refuses to run there instead of falling back to terraform without
its secrets.
`terraform-guard.js` treats `tf` as `terraform`, so `tf apply` is gated by
`ALLOW_TERRAFORM_MODIFY` exactly like `terraform apply`.

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
| `agent-box/Dockerfile`                    | Builds the image: Debian + Node.js + Claude Code CLI + docker CLI + Terraform + kubectl + sops/age + ttyd + Playwright MCP with headless Chromium, plus everyday CLI tools (`ps`/`pkill`, `jq`, `less`, `nc`, `dig`, `unzip`, `wget`, `tree`, Python with pip/venv, ...), and creates the non-root `claude` user. Ships a healthcheck that probes **both** servers (8091 and `/healthz` on 8090). The in-session Claude Code auto-updater is disabled — the image owns the version, taken from the `CLAUDE_CODE_VERSION` build arg (CI resolves the newest published release on every build).                                                                       |
| `agent-box/ep.sh`                         | Entrypoint (runs as **root**): fixes ownership, seeds first-run config, grants `claude` access to the Docker socket, optionally updates the CLI (`STARTUP_AUTO_UPDATE`), installs plugins, resolves the box's name (`agent_name.sh`), then launches ttyd and — in a restart loop, so a crash there never costs you the terminal — the session administration server.                                                                                                                                                                                                                    |
| `agent-box/scripts/launch_session.sh`     | ttyd's entry point for every browser tab. Validates the session id the browser passed as `?arg=` — session-id format, a transcript that exists, and no live process holding it — before handing off to `start_claude.sh` under `su - claude`. Fails closed: only a clean "not live" answer permits a resume, so a second tab on a running session is refused rather than allowed to corrupt the transcript.                                                                                                                         |
| `agent-box/scripts/start_claude.sh`       | Runs the Claude Code process for one tab: `claude --model "$CLAUDE_MODEL" --resume <id>`, or `--session-id <fresh uuid>` for a new session. Appends `--remote-control <name>-<suffix>` when `REMOTE_CONTROL_NAME` is set. Also starts the tab-title watcher alongside it.                                                                                                                                                                                                                                                           |
| `agent-box/scripts/sessions.py`           | The session administration server on container port `8090` (stdlib only, runs as `claude`): the page itself at `/`, `GET /api/sessions`, `POST /api/sessions/<uuid>/delete`, and an unauthenticated `/healthz` for the healthcheck. Everything else is behind the same basic-auth credentials as ttyd.                                                                                                                                                                                                                              |
| `agent-box/scripts/session_store.py`      | Read-only discovery of sessions from Claude Code's own state files — transcripts under `~/.claude/projects/`, live processes from `~/.claude/sessions/`. Stores no state of its own. Also the `--is-live` / `--slug` helper the two shell scripts call. Its only mutating function is the transcript delete.                                                                                                                                                                                                                        |
| `agent-box/scripts/agent_name.sh`         | Resolves what this box is called, once at boot: `AGENT_NAME` if the operator set one, else the container's name from `docker inspect` over the mounted socket, else the hostname. Never fails — an unnamed box costs a tab title, not a boot.                                                                                                                                                                                                                                                                                       |
| `agent-box/scripts/session_title.py`      | Names the browser tab. Started per session by `start_claude.sh`, it writes `Agent: <session name>` to the terminal as an OSC title and rewrites it whenever the session renames itself, then ends with the Claude process it was started from.                                                                                                                                                                                                                                                                                      |
| `agent-box/scripts/sessions_page.html`    | The admin page's UI. `sessions.py` bakes `AGENT_TABS_PUBLIC_PORT` into it at startup so the rows can link to the agent tabs, and `AGENT_NAME` as the page's browser-tab title.                                                                                                                                                                                                                                                                                                                                                      |
| `agent-box/scripts/update_claude_code.sh` | Optional boot-time CLI upgrade, gated by `STARTUP_AUTO_UPDATE` (only `Yes` enables it; fail-closed like the other flags). Runs as root before ttyd starts, so no session is holding the install it replaces. Never fails the boot — an unreachable registry leaves the box on the version its image shipped. |
| `agent-box/scripts/install_plugins.sh`    | Idempotently installs **and enables** the plugins from `plugins.txt`.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `agent-box/scripts/statusline.js`         | Default Claude Code status line (model, git branch, context usage, plan usage, session cost). Wired into `settings.json` by `ep.sh` unless a `statusLine` is already configured.                                                                                                                                                                                                                                                                                                                                                    |
| `agent-box/scripts/git-guard.js`          | `PreToolUse` hook enforcing `ALLOW_GIT_WRITE` (`No`/`Yes`; fail-closed): denies git write subcommands (`commit`, `push`, `pull`, `merge`, `rebase`, `reset`, `stash`, `checkout`/`switch`, branch/tag creation, ...) `restore`/`clean`, remote/config changes, ...) and any `gh` command outside a read allowlist (`view`/`list`/`status`/`diff`/`checks`/`watch`, `search`, clone/download, GET-only `gh api`, GraphQL queries). Always denies printing the token (`gh auth token`, `--show-token`). Registered idempotently in `settings.json` by `ep.sh`. |
| `agent-box/scripts/terraform-guard.js`    | `PreToolUse`/`PostToolUse` hook enforcing `ALLOW_TERRAFORM_MODIFY` (`No`/`Ask`/`Yes`; fail-closed) for infrastructure- or state-mutating Terraform (`apply`, `destroy`, `import`, `state rm`/`mv`, `taint`, ...); read-only commands (`plan`, `validate`, `show`, ...) pass through. Recognizes the `tf` wrapper as terraform. In `Ask` mode it prompts once per terraform directory and remembers it (so stage vs prod ask separately), persisting approvals in `~/.claude/terraform-approvals.json`. Registered for both events idempotently in `settings.json` by `ep.sh`. |
| `agent-box/bin/`                          | Commands copied onto `PATH` (`/usr/local/bin`): `secrets` (an environment's sops-encrypted secrets under `/workspace/infrastructure/<env>`) and `tf` (Terraform with those secrets loaded). See [Infrastructure secrets](#infrastructure-secrets-secrets-and-tf). |
| `agent-box/CLAUDE.md`                     | The agent's global operating manual + guardrails, refreshed into the volume on every start.                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `agent-box/skills/`                       | Skills baked into the image and synced to `~/.claude/skills/` on every start, so every deployment has them offline. Project-specific skills belong in the workspace at `/workspace/.claude/skills/` instead, where Claude Code reads them in place.                                                                                                                                                                                                                                                                                 |
| `agent-box/scripts/sync_claude_home.sh`   | Mirrors the baked `~/.claude` content (manual, skills) into the `claude-data` volume on every start. Needed because a named volume is pre-populated from the image only while empty — afterwards the volume wins, so a `COPY` alone would never reach an existing box. Image wins for what it ships; anything else in the volume is untouched; content dropped from a later image is removed.                                                                                                                                       |
| `.github/workflows/publish-agent-box.yml` | Builds the image on pushes to `main` touching `agent-box/**` and pushes `latest` + `sha-<commit>` tags to ghcr.io. Resolves the newest published Claude Code version and passes it as `CLAUDE_CODE_VERSION`, so the build cache cannot keep republishing a stale CLI.                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `.github/workflows/release-agent-box.yml` | On a `vX.Y.Z` tag push, tags the image main already published for that commit (`sha-<commit>`) as `vX.Y.Z` — a retag, never a rebuild, so a version is byte-for-byte the image main shipped (a rebuild could bake a newer Claude Code CLI). Never overwrites a published version. Waits up to 45 minutes for main's build, so tag right after pushing. |

### Startup lifecycle

1. The container starts `ep.sh` as **root** (PID 1).
2. It syncs the image-owned `~/.claude` content — the operating manual and any baked
   skills — into the `claude-data` volume via `sync_claude_home.sh`. It `chown`s
   `/home/claude` and `/workspace`, seeds onboarding-skip config (only files that
   don't already exist — `settings.json` lives in the volume and accumulates runtime
   state like plugin enablement, so it is never overwritten), and **grants the `claude`
   user access to the mounted Docker socket** by adding it to a group that matches the
   socket's GID (it never `chmod`s the socket itself, which would alter the host's
   inode). It registers the safety hooks (`terraform-guard.js`, `git-guard.js`) and
   regenerates the `claude` user's `~/.gitconfig` from `GH_TOKEN` / `GIT_USER_*`.
3. If `STARTUP_AUTO_UPDATE` is `Yes`, it updates the Claude Code CLI to the newest
   published version (`update_claude_code.sh`). This is the one safe moment: it runs as
   root, which the root-owned npm prefix needs, and before ttyd exists, so no session is
   holding the install being replaced. A failure here is a warning, never a failed boot.
4. It installs and enables plugins from `plugins.txt` as the `claude` user (idempotent),
   using whichever CLI version step 3 left in place.
5. It launches **ttyd** on port `8091` with `-a` (so a tab can name its session as
   `?arg=<session-id>`) and no client limit. Every connection runs
   `launch_session.sh` (through its `agent-session` alias), which validates that id and
   then starts one Claude Code process as `claude`. There is no global "one client" cap
   any more — the rule is per session: one tab per conversation, enforced by the
   launcher. ttyd runs with **no fixed title**, so each session can name its own browser
   tab.
6. It launches the **session administration server** on port `8090` as `claude`,
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
- Git write operations only when `ALLOW_GIT_WRITE` is `Yes` (enforced by a hook), and
  then only as a branch plus pull request — no force-pushes, no pushing to the
  default branch. Otherwise changes stay in the working tree for you to commit.
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
- **`GH_TOKEN` is visible to the agent.** It sits in the session environment, so
  anything the agent runs can use it. Scope it to the repositories and permissions the
  box needs, give it an expiration, and revoke it on GitHub if the box is compromised.
  The box never writes it anywhere itself and keeps it off command lines; the manual
  tells the agent never to print it, and the git guard blocks `gh auth token` /
  `--show-token`. It is still readable in `.credentials` and through `docker inspect`, so
  treat anything the agent can reach as able to use it.
- The `claude-data` volume holds your live credentials and conversation history. Remove
  it (`docker volume rm`) only if you intend to wipe the login and all transcripts.

---

## Common tasks & troubleshooting

- **Apply a change to `Dockerfile`/`ep.sh`/scripts:** these are baked into the image, so
  rebuild and recreate — `docker compose up --build -d`. A plain `restart` reuses the old
  image.
- **Update Claude Code:** the in-session auto-updater is disabled (the npm global dir is
  root-owned, sessions run as `claude`, and every session in the box shares the one
  install). Three ways to move the version:
  - **Pull a newer image** — CI resolves the newest published CLI on every build and
    bakes that exact version in, so `latest` always carries a current CLI.
  - **Rebuild locally** — `docker compose up --build -d`. The install layer is cached per
    version string, so pass the version to move a cached build:
    `CLAUDE_CODE_VERSION=$(npm view @anthropic-ai/claude-code version) docker compose up --build -d`.
    The same variable pins an older version.
  - **Let the box update itself** — set `STARTUP_AUTO_UPDATE: "Yes"` on the `agent`
    service. Every container start then installs the newest published CLI before any
    session launches. Costs a large download per start, and the box's version no longer
    follows its image tag.
- **Change the model:** set `CLAUDE_MODEL` on the `agent` service and recreate it. The
  next session launch picks it up.
- **"Refusing to resume: session ... is already open in another tab":** that session is
  running. Find the tab that holds it and close it, then reopen the session from the
  admin page.
- **"Refusing to resume: no transcript for ...":** the session list handed out a link to
  a port that is not this box's agent tabs — usually another agent-box, which is asked
  for a session it has never heard of. `AGENT_TABS_PUBLIC_PORT` doesn't match the host
  port the tabs are published on in `ports:`. Set the two to the same value and recreate
  the service — the port is baked into the page when
  the server starts. (The same message is genuine when the transcript really is gone,
  e.g. deleted from the session list or aged out by Claude Code's own cleanup.)
- **Add a plugin:** add a line to `agent-box/plugins.txt`, then rebuild (or rerun
  `install_plugins.sh` inside the container as the `claude` user).
- **Update the image in a consuming project:** `docker compose pull agent`, then
  `docker compose up -d agent`. Pin a release version (`v1.8.0`) or a `sha-<commit>` tag instead of `latest`
  for reproducibility. **Coming from an image published before the port change**, also
  update the service's `ports:` and `environment:` to the block shown in
  [Option A](#option-a--prebuilt-image-from-ghcrio-recommended): the servers now listen
  on `8090`/`8091` inside the container, and `TTYD_PUBLIC_PORT` / `ADMIN_PUBLIC_PORT`
  are no longer read. An un-updated mapping publishes to a container port nothing
  listens on, so the box fails to answer rather than serving wrong links.
- **Start over with a fresh login/history:** stop the stack and remove the project's
  `claude-data` volume (this deletes credentials _and_ all transcripts).
- **Check what's running:** `docker compose -f /workspace/docker-compose.yml ps`.
