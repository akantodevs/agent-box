// Default agent-box status line. Claude Code runs this command on each render,
// pipes session JSON on stdin, and displays the first line of stdout (ANSI
// colors supported). Shows: model | git branch | context | plan usage | cost.
// (No directory segment: in agent-box the working dir is always /repo.)
const { execSync } = require("child_process");

let raw = "";
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  let s = {};
  try {
    s = JSON.parse(raw);
  } catch {}

  const dim = "\x1b[2m";
  const cyan = "\x1b[36m";
  const green = "\x1b[32m";
  const yellow = "\x1b[33m";
  const red = "\x1b[31m";
  const reset = "\x1b[0m";

  const model = (s.model && s.model.display_name) || "Claude";
  const dir = (s.workspace && s.workspace.current_dir) || s.cwd || "";

  let branch = "";
  try {
    branch = execSync("git branch --show-current", {
      cwd: dir || undefined,
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
  } catch {}

  const parts = [`${cyan}${model}${reset}`];
  if (branch) parts.push(`${green}${branch}${reset}`);

  const cw = s.context_window;
  if (cw && cw.used_percentage != null) {
    const pct = Math.round(cw.used_percentage);
    const color = pct >= 80 ? red : pct >= 50 ? yellow : green;
    const u = cw.current_usage;
    const used = u
      ? (u.input_tokens || 0) +
        (u.cache_creation_input_tokens || 0) +
        (u.cache_read_input_tokens || 0)
      : null;
    const kilo = (n) => `${Math.round(n / 1000)}k`;
    const detail =
      used != null && cw.context_window_size
        ? ` ${dim}(${kilo(used)}/${kilo(cw.context_window_size)})${reset}`
        : "";
    parts.push(`${color}ctx ${pct}%${reset}${detail}`);
  }

  // Plan usage (rate limits). Sent for Claude.ai Pro/Max subscribers after the
  // first API response; each window may be independently absent. The 5h window
  // also shows time until it resets (the 7d one moves too slowly to matter).
  const rl = s.rate_limits || {};
  for (const [label, win, showReset] of [
    ["5h", rl.five_hour, true],
    ["7d", rl.seven_day, false],
  ]) {
    if (win && win.used_percentage != null) {
      const pct = Math.round(win.used_percentage);
      const color = pct >= 80 ? red : pct >= 50 ? yellow : green;
      let seg = `${dim}${label}${reset} ${color}${pct}%${reset}`;
      if (showReset && win.resets_at) {
        const mins = Math.round((win.resets_at * 1000 - Date.now()) / 60000);
        if (mins > 0) {
          const t = mins >= 60 ? `${Math.floor(mins / 60)}h${String(mins % 60).padStart(2, "0")}m` : `${mins}m`;
          seg += ` ${dim}(resets ${t})${reset}`;
        }
      }
      parts.push(seg);
    }
  }

  const cost = s.cost && s.cost.total_cost_usd;
  if (cost) parts.push(`${dim}$${cost.toFixed(2)}${reset}`);

  process.stdout.write(parts.join(` ${dim}|${reset} `));
});
