#!/bin/sh
# install_plugins.sh — ensure Claude Code plugins listed in plugins.txt are installed.
#
# Run this AS THE `claude` USER (plugins install into that user's ~/.claude).
# It is idempotent: already-installed plugins and already-known marketplaces
# are skipped, so it is safe to run on every container start.
#
# plugins.txt format (one entry per line):
#   <plugin>@<marketplace>                  # marketplace source resolved below
#   <plugin>@<marketplace>  <source>        # explicit source (URL / path / github repo)
#   # lines starting with '#' and blank lines are ignored
set -eu

PLUGINS_FILE="${PLUGINS_FILE:-/workspace/agent-box/plugins.txt}"

# Known marketplace name -> source mapping. Extend as you add marketplaces,
# or override per-line with a second field in plugins.txt.
default_source() {
    case "$1" in
        claude-plugins-official) echo "anthropics/claude-plugins-official" ;;
        *)                       echo "" ;;
    esac
}

if [ ! -f "$PLUGINS_FILE" ]; then
    echo "[plugins] no file at $PLUGINS_FILE — nothing to do"
    exit 0
fi

# Opt-out for the playwright plugin (browser automation): set
# DISABLE_PLAYWRIGHT=true/1/yes to keep it off, e.g. when running agent-box
# for something other than web development. Clearing the flag re-enables the
# plugin on the next start (the enable step below is what brings it back).
case "$(printf '%s' "${DISABLE_PLAYWRIGHT:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes) disable_playwright=1 ;;
    *)          disable_playwright=0 ;;
esac

# Snapshot current state once; refresh after each mutating command.
installed="$(claude plugin list 2>/dev/null || true)"
known="$(claude plugin marketplace list 2>/dev/null || true)"

while IFS= read -r line || [ -n "$line" ]; do
    # Strip inline comments, CRs, and surrounding whitespace.
    line="${line%%#*}"
    line="$(printf '%s' "$line" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$line" ] && continue

    spec="$(printf '%s\n' "$line" | awk '{print $1}')"            # plugin@marketplace
    source_override="$(printf '%s\n' "$line" | awk '{print $2}')" # optional source

    plugin="${spec%@*}"
    market="${spec#*@}"
    [ "$market" = "$spec" ] && market=""   # no '@' present

    # 0. Honor DISABLE_PLAYWRIGHT: keep the plugin disabled if present, and
    # don't install it on fresh volumes.
    if [ "$disable_playwright" = 1 ] && [ "$plugin" = "playwright" ]; then
        if printf '%s' "$installed" | grep -qF "$spec"; then
            disable_out="$(claude plugin disable "$spec" 2>&1)" \
                || printf '%s' "$disable_out" | grep -q "already disabled" \
                || echo "[plugins] WARN: failed to disable $spec: $disable_out"
            echo "[plugins] $spec disabled (DISABLE_PLAYWRIGHT is set)"
        else
            echo "[plugins] skipping $spec (DISABLE_PLAYWRIGHT is set)"
        fi
        continue
    fi

    # 1. Ensure the marketplace is known.
    if [ -n "$market" ] && ! printf '%s' "$known" | grep -qF "$market"; then
        src="$source_override"
        [ -z "$src" ] && src="$(default_source "$market")"
        if [ -n "$src" ]; then
            echo "[plugins] adding marketplace '$market' ($src)"
            if claude plugin marketplace add "$src"; then
                known="$(claude plugin marketplace list 2>/dev/null || true)"
            else
                echo "[plugins] WARN: failed to add marketplace '$market' — skipping $spec"
                continue
            fi
        else
            echo "[plugins] WARN: marketplace '$market' unknown and no source given — skipping $spec"
            continue
        fi
    fi

    # 2. Install the plugin if not already present.
    if printf '%s' "$installed" | grep -qF "$spec"; then
        echo "[plugins] $spec already installed"
    else
        echo "[plugins] installing $spec"
        if claude plugin install "$spec"; then
            installed="$(claude plugin list 2>/dev/null || true)"
        else
            echo "[plugins] WARN: failed to install $spec"
            continue
        fi
    fi

    # 3. Ensure the plugin is enabled. Enablement lives in ~/.claude/settings.json
    # ("enabledPlugins"), separate from install state, and can be lost (e.g. by a
    # settings reseed) while the plugin payload survives in the volume — leaving it
    # installed but disabled. `claude plugin enable` exits non-zero when the plugin
    # is already enabled, so treat that as success.
    enable_out="$(claude plugin enable "$spec" 2>&1)" \
        || printf '%s' "$enable_out" | grep -q "already enabled" \
        || { echo "[plugins] WARN: failed to enable $spec: $enable_out"; continue; }
    echo "[plugins] $spec enabled"
done < "$PLUGINS_FILE"

echo "[plugins] done"
