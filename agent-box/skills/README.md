# Baked skills

Skills in this directory ship **inside the agent-box image** and are synced into
`~/.claude/skills/` on every container start by `scripts/sync_claude_home.sh`.
They are available to every deployment of the image, offline, with no download.

Put a skill here when it is useful to *any* agent-box deployment. A skill specific
to one project belongs in that project's workspace instead, at
`/workspace/.claude/skills/<name>/SKILL.md`, where it needs no plumbing and travels
with the repo.

## Adding a skill

    agent-box/skills/<name>/SKILL.md

`SKILL.md` needs YAML frontmatter with `name` and `description`; the description is
what Claude Code loads into context to decide whether the skill is relevant, so make
it specific about when to use it.

## Rules

- **The image wins.** On every boot each baked skill replaces `~/.claude/skills/<name>/`
  wholesale. A user-created skill of the same name will be overwritten; a skill with
  any other name is never touched.
- **Deletions propagate.** Removing a skill here removes it from existing volumes on
  the next start, via the `.agent-box-synced` manifest.
- **Rebuild required.** Changes here reach a running box only after the image is
  rebuilt and the container recreated.
