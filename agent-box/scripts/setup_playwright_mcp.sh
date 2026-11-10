#!/bin/sh
# setup_playwright_mcp.sh — run the image's own Playwright MCP server.
#
# Run this AS THE `claude` USER from ep.sh, on every container start. Needs jq.
#
# Why: the playwright plugin's server is `npx @playwright/mcp@latest`, which
# follows every upstream release and then looks for a Chromium revision this
# image never installed ("Browser chrome-for-testing is not installed"). The
# Dockerfile installs a pinned @playwright/mcp together with the browser that
# same package expects, so this script:
#   1. registers that binary as the user-scope MCP server "playwright" in
#      ~/.claude.json — the same entry `claude mcp add --scope user playwright
#      -- /usr/bin/playwright-mcp` writes, without a ~1s CLI start. It is
#      written every boot because ~/.claude.json is not on the state volume
#      and is reseeded when the container is recreated;
#   2. switches the plugin off in ~/.claude/settings.json if an older image
#      left it enabled, so two servers are not both claiming the name.
#
# DISABLE_PLAYWRIGHT=true/1/yes removes the server instead (the plugin stays
# off either way). Clearing the flag brings the server back on the next start.
#
# A file that is missing or cannot be parsed is warned about and left exactly as
# it is: a broken browser tool is never a reason not to boot.
set -u

CLAUDE_JSON="${CLAUDE_JSON:-$HOME/.claude.json}"
CLAUDE_SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
PLAYWRIGHT_MCP_BIN="${PLAYWRIGHT_MCP_BIN:-/usr/bin/playwright-mcp}"
PLUGIN="playwright@claude-plugins-official"

case "$(printf '%s' "${DISABLE_PLAYWRIGHT:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes) disabled=1 ;;
    *)          disabled=0 ;;
esac

# Apply jq filter $2 (with any extra jq args) to file $1, replacing it only if
# jq succeeded. The temp file sits beside the target so the mv is a rename.
edit_json() {
    _file="$1"; shift
    _tmp="$_file.tmp.$$"
    if jq "$@" "$_file" > "$_tmp" 2>/dev/null; then
        mv "$_tmp" "$_file"
    else
        rm -f "$_tmp"
        echo "[playwright-mcp] WARN: could not update $_file (not valid JSON?) — left unchanged"
        return 1
    fi
}

# 1. The MCP server entry.
if [ ! -f "$CLAUDE_JSON" ]; then
    echo "[playwright-mcp] WARN: $CLAUDE_JSON does not exist — server not registered"
elif [ "$disabled" = 1 ]; then
    edit_json "$CLAUDE_JSON" 'if .mcpServers then del(.mcpServers.playwright) else . end' \
        && echo "[playwright-mcp] server not registered (DISABLE_PLAYWRIGHT is set)"
else
    edit_json "$CLAUDE_JSON" --arg cmd "$PLAYWRIGHT_MCP_BIN" \
        '.mcpServers.playwright = {type: "stdio", command: $cmd, args: [], env: {}}' \
        && echo "[playwright-mcp] registered $PLAYWRIGHT_MCP_BIN as MCP server 'playwright'"
fi

# 2. The plugin's own copy. Only rewritten when it is actually enabled, so a
# box that is already right costs one read and no write.
if [ -f "$CLAUDE_SETTINGS" ] \
    && jq -e --arg p "$PLUGIN" '.enabledPlugins[$p] == true' "$CLAUDE_SETTINGS" >/dev/null 2>&1; then
    edit_json "$CLAUDE_SETTINGS" --arg p "$PLUGIN" '.enabledPlugins[$p] = false' \
        && echo "[playwright-mcp] disabled plugin $PLUGIN (its npx @latest server is superseded)"
fi

exit 0
