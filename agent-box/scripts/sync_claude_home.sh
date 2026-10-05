#!/bin/sh
# sync_claude_home.sh — mirror image-owned Claude Code content into the state volume.
#
# Run this AS ROOT from ep.sh, before the chown of /home/claude, so synced files end
# up owned by the claude user.
#
# Why this exists at all: ~/.claude is the claude-data volume. Docker pre-populates a
# named volume from the image only while that volume is EMPTY; afterwards the volume
# wins and image content at that path is invisible. So a Dockerfile COPY reaches a
# fresh box exactly once and never updates an existing one — silently. Everything
# Claude Code reads (CLAUDE.md, skills/, plugins/) lives under that path, so image
# content has to be copied in on every boot.
#
# Rules:
#   * The image wins for anything it ships.
#   * Nothing else in the state directory is ever touched.
#   * Content dropped from a later image is removed from existing volumes, tracked
#     through the manifest below. Only paths we previously wrote are eligible, so a
#     user-created skill can never be removed by this script.
set -eu

SRC="${AGENT_BOX_CLAUDE_HOME_SRC:-/opt/agent-box/claude-home}"
DEST="${CLAUDE_STATE_DIR:-/home/claude/.claude}"
MANIFEST="$DEST/.agent-box-synced"

if [ ! -d "$SRC" ]; then
    echo "[sync-claude-home] no baked tree at $SRC — nothing to do"
    exit 0
fi

mkdir -p "$DEST"

# --- guard ------------------------------------------------------------------
# Paths the image must never ship: they hold credentials, runtime state, or the
# user's own conversations, all of which live in the volume and would be destroyed
# on every boot. Fail before copying anything, so a build mistake is caught the
# first time the container starts rather than after it has eaten someone's state.
for _guarded in settings.json .credentials.json .claude.json projects plugins \
                history.jsonl sessions session-env tasks; do
    if [ -e "$SRC/$_guarded" ]; then
        echo "[sync-claude-home] refusing to sync: baked tree contains stateful path '$_guarded'" >&2
        echo "[sync-claude-home] remove it from the image — the volume owns that path" >&2
        exit 1
    fi
done

# Relative paths synced this run, newline-delimited with a leading newline so the
# stale-removal check below can match whole lines (see the case statement).
synced="
"

# Replace one entry wholesale. $1 is a path relative to both SRC and DEST.
sync_entry() {
    _rel="$1"
    _src="$SRC/$_rel"
    _dst="$DEST/$_rel"
    [ -e "$_src" ] || return 0

    mkdir -p "$(dirname "$_dst")"
    rm -rf "$_dst"
    cp -R "$_src" "$_dst"
    synced="$synced$_rel
"
}

# --- sync units -------------------------------------------------------------
# Granularity is declared, not inferred. CLAUDE.md is one file. Under skills/ the
# unit is each CHILD entry, never the skills/ directory itself: replacing that
# wholesale would delete skills the user created in their own box.

sync_entry CLAUDE.md

if [ -d "$SRC/skills" ]; then
    mkdir -p "$DEST/skills"
    for _path in "$SRC"/skills/*; do
        [ -e "$_path" ] || continue     # unmatched glob when skills/ is empty
        sync_entry "skills/$(basename "$_path")"
    done
fi

# --- stale removal ----------------------------------------------------------
# Anything we synced on a previous boot that the image no longer ships is removed.
# The manifest is the whole safety mechanism: a path the user created was never
# written by us, so it never appears here, so it can never be removed.
if [ -f "$MANIFEST" ]; then
    while IFS= read -r _old; do
        [ -n "$_old" ] || continue
        case "$synced" in
            *"
$_old
"*) continue ;;                 # still shipped by the image
        esac
        rm -rf "$DEST/$_old"
        echo "[sync-claude-home] removed $_old (no longer in the image)"
    done < "$MANIFEST"
fi

printf '%s' "$synced" > "$MANIFEST"

echo "[sync-claude-home] synced image content into $DEST"
