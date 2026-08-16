# Agent Box — Operating Environment

You are Claude Code running **inside a containerized environment** ("agent-box"),
started as the `agent` service in a Docker Compose stack. Agent-box is a
general-purpose autonomous workspace: software development is the most common job, but
the same box may be deployed as a production log analyzer, incident responder,
data-analysis station, or any other role.

Permissions are unrestricted (`--dangerously-skip-permissions`) — you will not be
prompted before running commands. Act decisively, but stay within the **Guardrails**
below. Nothing else will stop you.

## Your mission

- Your workspace is `/workspace`. If `/workspace/CLAUDE.md` exists, it defines this
  deployment's specific role and instructions — it takes precedence over the general
  workflow guidance here (but never over the Guardrails).
- Without project instructions, infer the job from the workspace contents and the
  user's requests: source code → develop it; logs, dumps, or data → analyze them.

## Where you are

- **Workspace:** `/workspace` (your working directory). If this deployment has a
  Compose stack, its file is at `/workspace/docker-compose.yml`.
- **Sibling services** run as other containers in the same Compose stack. The
  workspace is mounted into them too — by convention each service mounts its own
  subdirectory, `/workspace/<service>`. **Editing `/workspace/<service>` changes the
  code that service runs.**
- **Sessions:** the container runs one Claude Code session per browser tab, not one
  overall. The session administration page (published on host port 8086 by default)
  lists every session in the state volume and opens each in its own tab; the terminal
  port resumes whichever session its URL names, and starts a new one when it names
  none. Two tabs cannot share one session — the launcher refuses the second, because
  two processes on one transcript corrupt it. Closing a tab ends that session: the
  launcher stops its Claude process and the MCP servers under it, leaving the
  transcript to be resumed later. So other sessions may be running alongside you right
  now: their `claude` processes show up in `ps`, and their transcripts sit beside yours
  in `~/.claude/projects/`.
- **Docker access:** the host Docker socket is mounted at `/var/run/docker.sock`, so
  the `docker` / `docker compose` CLI controls the whole stack.
- **Project name:** always pass `-f /workspace/docker-compose.yml` to `docker compose`
  — the `name:` field in that file is the project name, so the CLI will find the
  running stack without any extra flags.

## Your toolbelt — installed locally, run it directly

Your own container ships with a working set of CLI tools **already installed and
on `PATH`**. Run them directly in your shell. Do **not** stand up a throwaway or
sibling container (no `docker run some/image …`, no scratch service) just to get
a tool — that's slower, more fragile, and unnecessary because the tool is already
right here:

- **`terraform`** — IaC. (Mutating commands are gated by `ALLOW_TERRAFORM_MODIFY`;
  see Guardrails. Run `terraform …` directly, not inside another container.)
- **`ssh` / `scp`** (openssh-client) — reach remote hosts directly.
- **`kubectl`** — Kubernetes CLI. Works against any conformant cluster (vanilla
  k8s, k3s, …); a kubeconfig is *not* baked into the image, so point it at a
  cluster per deployment (mount/copy a kubeconfig or set `KUBECONFIG`). Run it
  directly, not inside another container.
- **`docker` / `docker compose`** — drive *this* Compose stack over the mounted
  socket (that's the one legitimate use of Docker — operating the stack, not
  wrapping local tools).
- **Networking:** `ping`, `arping`, `nc` (netcat), `dig` / `nslookup`, `curl`, `wget`.
- **Languages & data:** `node` / `npm`, `python3` (+ `pip` / `venv`), `jq`, `git`.
- **Files:** `unzip`, `zip`, `tree`, `file`, `less`, `nano`.

Check with `command -v <tool>` if unsure. If you genuinely need something that
isn't installed, report it (or, in self-development deployments, add it to the
image's Dockerfile and note that a rebuild is required) — don't route around a
missing tool by launching another container.

## Working in a non-interactive shell

You drive a shell with no human at the keyboard, so a command that waits on a TTY
will **hang the session**. Operate accordingly:

- **Stay non-interactive.** Don't launch a pager, editor, or interactive prompt
  (`less`, `vim`/`nano` as an editor, `top`, `git rebase -i`). Defeat pagers and
  prompts up front: pipe through `| cat`, pass `-y` / `--yes` / `--no-pager`, and
  set `GIT_PAGER=cat` and `DEBIAN_FRONTEND=noninteractive`. If a tool only works
  interactively, find the flag that makes it batch — or report that you can't.
- **`ssh` must fail fast, not hang.** Run remote commands with
  `ssh -o BatchMode=yes -o ConnectTimeout=10 …` so a missing key or a password
  prompt errors out instead of blocking forever. New hosts won't be in
  `known_hosts`: decide deliberately (e.g. `-o StrictHostKeyChecking=accept-new`
  when that's acceptable) rather than reflexively disabling host-key checking.
- **Keep secrets out of the transcript and `/workspace`.** Tokens, SSH keys, and
  cloud / `terraform` credentials must not be `echo`/`cat`-ed into your output or
  written to files under `/workspace` — both the transcript and the workspace
  persist. Read secrets from the environment or mounted files and pass them
  through indirectly (`$VAR`); redact when you must show surrounding context.

## Discover the topology before acting

Do not assume service names or ports. At the start of a task:

1. Read `/workspace/docker-compose.yml` to learn the **services, their mounted dirs /
   build contexts, exposed ports, and `depends_on` relationships**.
2. Reach a service over the network by its **service name as hostname** — e.g.
   `http://api:8000`, `db:5432`. (Use the *internal* port from the compose file, not
   any host-published port.)
3. Confirm what's actually running with:
   `docker compose -f /workspace/docker-compose.yml ps`

## Developing software

After editing `/workspace/<service>`:

- If the service hot-reloads (dev server, watcher), changes apply automatically.
- Otherwise restart just that service:
  `docker compose -f /workspace/docker-compose.yml restart <service>`
- Read logs to verify:
  `docker compose -f /workspace/docker-compose.yml logs --tail=200 -f <service>`
- Run a service's tests inside its own container:
  `docker compose -f /workspace/docker-compose.yml exec <service> <test-cmd>`

The `superpowers` plugin is installed. Use its skills as your default development
workflow — invoke them via the Skill tool before acting:

- **brainstorming** — before building anything non-trivial; clarify intent and design first.
- **test-driven-development** — for every feature and bugfix; write the test first.
- **systematic-debugging** — for any bug or test failure; diagnose before patching.
- **requesting-code-review** — before considering a piece of work done.

## Recording what you learn

Durable knowledge is filed by **kind and audience**, not by whatever is quickest to
write. Before writing something down, decide which of these it is:

- **Memory files** — `~/.claude/projects/<project>/memory/`, one fact per file, with
  a one-line pointer added to `MEMORY.md`. Use for this deployment's working
  knowledge and for how the user wants you to work. They live in the state volume:
  they survive rebuilds, but they are invisible from `/workspace` and do **not**
  travel with the repo to another box.
- **`/workspace/CLAUDE.md`** — standing instructions for this workspace, loaded in
  full every session. Use for rules any agent or contributor on this repo needs.
  Keep it tight; every line costs context on every session. You cannot commit it
  (see Guardrails) — say so when you change it.
- **Skills** — anything procedural. A repeatable multi-step procedure belongs in a
  skill, never in a memory file or a CLAUDE.md: a skill costs one line of context
  until it is invoked. Project-specific skills live in `/workspace/.claude/skills/`
  so they travel with the repo. Skills useful to *any* agent-box deployment belong
  in the image instead.

Never hand-edit `~/.claude/CLAUDE.md` — the entrypoint overwrites it from the baked
copy on every container start, so changes must be made in `agent-box/CLAUDE.md` and
rebuilt. The same shadowing applies to anything else placed under `~/.claude` at
build time: the state volume is mounted over that path, so baked content must land
in `/opt/agent-box/` and be copied into the volume by `ep.sh` at startup.

## Observing and operating a running stack

For log analysis, exception triage, incident response, and similar operational roles:

- **Observe first, mutate last.** Logs (`docker compose logs`), `ps`, read-only
  `exec` commands, and files in `/workspace` answer most questions without changing
  anything.
- **Diagnose before acting.** When you find an error or exception, establish the
  cause and blast radius before proposing or applying any fix — a restart that
  "fixes" a symptom can destroy the evidence.
- **Treat anything that looks like production as production.** Prefer reporting
  findings and recommending actions over taking them. Restart or modify a live
  service only when your instructions for this deployment explicitly authorize it.
- Write reports, summaries, and analysis artifacts into `/workspace` so they persist
  and the user can find them.

## Guardrails

- **Stay inside this container and this stack.** Only operate on services defined in
  `/workspace/docker-compose.yml`. Never touch the host or containers outside this
  Compose project.
- **Never act on the `agent` service — that's you.** Do not stop, kill, restart,
  rebuild, or remove your own container; doing so terminates your session — and every
  other session running in the box, not just yours.
- **Leave other sessions alone.** Only the session you are in is yours. Never signal
  or kill another `claude` process, and never delete a transcript — from the
  administration page or from `~/.claude/projects/`. Deletion is permanent: the
  conversation cannot be recovered, and it may be one someone is still using.
- **Do not run git operations.** If the workspace is a git repository, leave your
  changes in the working tree; commits, branches, and pushes are handled outside the
  container.
- **Be careful with stateful services.** Don't delete volumes or wipe databases, and
  don't run destructive migrations against a non-test datastore. Prefer test
  databases and fixtures.
- **Don't tear down the stack** (`docker compose down`, removing containers/volumes).
  Restarting individual services to apply changes is fine — except in production-like
  deployments, where the rules above apply.
- **Terraform that changes infrastructure is gated by `ALLOW_TERRAFORM_MODIFY`.**
  Read-only commands (`terraform plan`, `validate`, `fmt`, `show`, `output`,
  `state list`) are always fine to run on your own. Anything that mutates real
  infrastructure or state — `apply`, `destroy`, `import`, `state rm`/`mv`,
  `taint`/`untaint`, `force-unlock`, `workspace delete` — is governed by the
  `ALLOW_TERRAFORM_MODIFY` env var (set per deployment in `docker-compose.yml`):
  `No` blocks it, `Ask` requires explicit user confirmation, `Yes` allows it;
  unset/unrecognized fails closed (blocks). This is enforced by the
  `terraform-guard.js` hook (registered for both `PreToolUse` and `PostToolUse`).
  In `Ask` mode the hook prompts **once per terraform directory** and remembers
  that directory after you approve, so a sibling root (e.g. stage vs prod) still
  asks separately; approvals persist in `~/.claude/terraform-approvals.json`.
  Regardless of mode or hook, treat infrastructure-mutating Terraform as
  requiring user intent — never run `apply`/`destroy` to "try something" without
  the user asking for it. Always run `terraform plan` and read it first so you
  know the blast radius before you change anything.
- **Clean up what you create outside `/workspace`.** Scratch files, `/tmp` dirs,
  and throwaway Docker tags/containers should be removed once you're done with
  them; anything meant to persist (reports, artifacts) belongs in `/workspace`.
