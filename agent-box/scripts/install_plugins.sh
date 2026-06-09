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

PLUGINS_FILE="${PLUGINS_FILE:-/repo/agent-box/plugins.txt}"

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
        echo "[plugins] $spec already installed — skipping"
    else
        echo "[plugins] installing $spec"
        if claude plugin install "$spec"; then
            installed="$(claude plugin list 2>/dev/null || true)"
        else
            echo "[plugins] WARN: failed to install $spec"
        fi
    fi
done < "$PLUGINS_FILE"

echo "[plugins] done"
