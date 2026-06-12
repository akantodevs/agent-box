# agent-box — self-development mode

This workspace is the **agent-box project itself**: the source of the very container
you are running in. (The general operating manual is in your global CLAUDE.md; this
file adds what's specific to developing agent-box.)

## Project links

- **Public repository:** https://github.com/akantodevs/agent-box
- **Published image:** `ghcr.io/akantodevs/agent-box` — built and pushed by
  `.github/workflows/publish-agent-box.yml` on every push to `main` touching
  `agent-box/**`.

## Working with GitHub issues

Planned work and ideas are tracked as GitHub issues. The `gh` CLI is not installed
and git operations are out of bounds, but the repo is public — read issues through
the API or web:

- List open issues:
  `curl -s "https://api.github.com/repos/akantodevs/agent-box/issues?state=open"`
- One issue / its discussion:
  `curl -s https://api.github.com/repos/akantodevs/agent-box/issues/<n>` and
  `.../issues/<n>/comments`
- When asked to "look at the issues" or pick up work, start from the open-issues
  list, and reference issue numbers (`#<n>`) in your summaries so work can be traced
  back. You cannot close or comment on issues — note in your summary when an issue
  is addressed so the user can close it.

## Self-development cautions

- Changes under `agent-box/` (Dockerfile, ep.sh, scripts) only take effect after an
  image rebuild, which must happen **outside** this container — never rebuild or
  restart your own container. Say so in your summary when a change needs a rebuild.
- `docker build` with a throwaway tag is fine for verifying the Dockerfile; clean up
  test tags and containers afterwards.
- Keep `README.md` and the baked manual (`agent-box/CLAUDE.md`) in sync with
  behavior changes — they are the product's documentation.
