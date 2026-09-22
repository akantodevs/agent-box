#!/usr/bin/env node
// PreToolUse(Bash) guard for git and gh, driven by ALLOW_GIT_WRITE.
//
// ALLOW_GIT_WRITE (case-insensitive):
//   Yes  -> allow everything (the hook says nothing)
//   No   -> deny git/gh commands positively identified as writes
//   unset/unrecognized -> same as No (fail-closed)
//
// "Write" means anything that creates commits, moves, deletes or rewrites refs
// and history, changes remotes or git config, or discards working-tree changes:
// commit, push, pull, merge, rebase, reset, checkout, restore, clean, stash,
// branch/tag creation, and so on. Staging (add/rm/mv) is not a write in this
// sense — it records nothing and the files are the agent's to edit anyway. For gh
// it is the inverse — a small read allowlist (view/list/status/diff/checks,
// search, clone/download, GET-only `gh api`, GraphQL queries), and everything
// else is a write. This keeps the guardrail in the operating manual
// (agent-box/CLAUDE.md); keep the two in sync.
//
// Independently of the mode, printing the GitHub token (`gh auth token`,
// `gh auth status --show-token`) is always denied: the manual forbids putting
// secrets in the transcript, and a token printed once is in it for good.
//
// Fail-open on anything we can't classify (non-Bash, no command, unparseable
// payload, git hidden behind `bash -c`, `eval`, `xargs`, `sudo` or a variable):
// like terraform-guard.js this is a safety net around recognized write verbs, not
// a sandbox. The fail-CLOSED default applies only once a write has been
// positively identified. The segment split below is not quote-aware either, so a
// `;` inside a quoted argument can produce a (harmless) false deny.
'use strict';

const path = require('path');

// git subcommands that are writes whatever their arguments.
const GIT_WRITES = new Set([
  'commit', 'push', 'pull', 'merge', 'rebase', 'cherry-pick', 'revert', 'reset',
  'am', 'checkout', 'switch', 'restore', 'clean', 'update-ref', 'symbolic-ref',
  'filter-branch', 'replace',
]);
// git subcommands that are writes unless their sub-verb is one of these reads.
// An absent sub-verb is the empty string: `git stash` alone is `stash push`,
// while `git remote`, `git reflog` and `git notes` alone are listings.
const GIT_READ_SUBVERBS = {
  stash: new Set(['list', 'show']),
  worktree: new Set(['list']),
  remote: new Set(['', 'show', 'get-url']),
  reflog: new Set(['', 'show', 'exists']),
  notes: new Set(['', 'list', 'show', 'get-ref']),
};
// Wrappers that run their arguments as a command. Skipped, together with their
// own flags, to reach the command they run.
const WRAPPERS = new Set(['env', 'command', 'builtin', 'exec', 'time', 'nice', 'nohup']);
// git global options that take their value as the next token (when not `--opt=v`).
const GIT_VALUE_OPTS = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env']);
// `git branch` flags that create, delete, rename, copy or re-point a branch.
const BRANCH_WRITE_FLAGS = new Set([
  '-d', '-D', '--delete', '-m', '-M', '--move', '-c', '-C', '--copy', '-f', '--force',
  '-u', '--set-upstream-to', '--unset-upstream', '--edit-description',
]);
// `git branch` flags that make it a listing, where a positional is a pattern.
const BRANCH_LIST_FLAGS = new Set([
  '-l', '--list', '-a', '--all', '-r', '--remotes', '-v', '-vv', '--verbose',
  '--contains', '--no-contains', '--merged', '--no-merged', '--points-at',
  '--show-current', '--format', '--sort', '--column', '--no-column',
]);
// `git tag` flags that make it a listing (git implies --list for all of these).
const TAG_LIST_FLAGS = new Set([
  '-l', '--list', '-n', '--contains', '--no-contains', '--merged', '--no-merged',
  '--points-at', '--sort', '--format', '--column', '--no-column', '-i', '--ignore-case',
]);
// `git config` flags that only read, and those that write. Anything else is
// decided by the positionals: `key` alone reads, `key value` writes.
const CONFIG_READ_FLAGS = new Set([
  '--get', '--get-all', '--get-regexp', '--get-urlmatch', '--get-color',
  '--get-colorbool', '--list', '-l',
]);
const CONFIG_WRITE_FLAGS = new Set([
  '--add', '--replace-all', '--unset', '--unset-all', '--rename-section',
  '--remove-section', '--edit', '-e',
]);
// `git config` options whose value is the next token.
const CONFIG_VALUE_OPTS = new Set(['-f', '--file', '--blob', '--type', '--default', '--comment']);
// The subcommand form git 2.46 added (`git config get|set|...`).
const CONFIG_READ_VERBS = new Set(['get', 'list']);
const CONFIG_WRITE_VERBS = new Set(['set', 'unset', 'rename-section', 'remove-section', 'edit']);
// gh `<group> <verb>` verbs that only read (or only write locally).
const GH_READ_VERBS = new Set(['view', 'list', 'status', 'diff', 'checks', 'watch', 'clone', 'download']);

// Split a shell segment into words, honoring '…' and "…" quoting anywhere in a
// word (so `-f query='{ viewer }'` is two words, the second `query={ viewer }`).
// Not a shell parser — no expansions, and backslashes only escape inside "…" or
// before a character outside quotes.
function words(segment) {
  const out = [];
  let cur = null;
  let quote = null;
  for (let i = 0; i < segment.length; i++) {
    const c = segment[i];
    if (quote) {
      if (c === quote) quote = null;
      else if (c === '\\' && quote === '"' && i + 1 < segment.length) cur += segment[++i];
      else cur += c;
    } else if (c === "'" || c === '"') {
      quote = c;
      cur = cur === null ? '' : cur;
    } else if (/\s/.test(c)) {
      if (cur !== null) out.push(cur);
      cur = null;
    } else if (c === '\\' && i + 1 < segment.length) {
      cur = (cur === null ? '' : cur) + segment[++i];
    } else {
      cur = (cur === null ? '' : cur) + c;
    }
  }
  if (cur !== null) out.push(cur);
  return out;
}

function flagName(tok) {
  return tok.split('=')[0];
}

function positionals(args, valueOpts) {
  const out = [];
  for (let i = 0; i < args.length; i++) {
    if (valueOpts && valueOpts.has(args[i])) { i++; continue; }
    if (!args[i].startsWith('-')) out.push(args[i]);
  }
  return out;
}

function classifyConfig(rest) {
  const flags = rest.filter((a) => a.startsWith('-')).map(flagName);
  const pos = positionals(rest, CONFIG_VALUE_OPTS);
  if (flags.some((f) => CONFIG_WRITE_FLAGS.has(f))) return 'git config (write)';
  if (CONFIG_WRITE_VERBS.has(pos[0])) return `git config ${pos[0]}`;
  if (CONFIG_READ_VERBS.has(pos[0])) return null;
  if (flags.some((f) => CONFIG_READ_FLAGS.has(f))) return null;
  return pos.length >= 2 ? 'git config (write)' : null;
}

function classifyGit(args) {
  let i = 0;
  while (i < args.length && args[i].startsWith('-')) {
    if (GIT_VALUE_OPTS.has(args[i])) i++; // its value is the next token
    i++;
  }
  const sub = args[i];
  if (!sub) return null;
  const rest = args.slice(i + 1);
  if (GIT_WRITES.has(sub)) return `git ${sub}`;
  if (GIT_READ_SUBVERBS[sub]) {
    const verb = positionals(rest)[0] || '';
    return GIT_READ_SUBVERBS[sub].has(verb) ? null : `git ${sub}${verb ? ' ' + verb : ''}`;
  }
  if (sub === 'config') return classifyConfig(rest);
  if (sub === 'branch') {
    const flags = rest.filter((a) => a.startsWith('-')).map(flagName);
    if (flags.some((f) => BRANCH_WRITE_FLAGS.has(f))) return 'git branch (modify)';
    if (flags.some((f) => BRANCH_LIST_FLAGS.has(f))) return null;
    if (rest.some((a) => !a.startsWith('-'))) return 'git branch (create)';
    return null;
  }
  if (sub === 'tag') {
    if (rest.length === 0) return null;
    const flags = rest.filter((a) => a.startsWith('-')).map(flagName);
    // `-n<num>` is -n with its line count attached.
    const listing = flags.some((f) => TAG_LIST_FLAGS.has(f) || /^-n\d+$/.test(f));
    const deleting = flags.includes('-d') || flags.includes('--delete');
    if (listing && !deleting) return null;
    return 'git tag (create/delete)';
  }
  return null;
}

function classifyGhApi(args) {
  let method = null;
  let fields = false;
  let query = null; // the GraphQL `query` field, when given inline
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    let field = null;
    if (a === '-X' || a === '--method') method = args[++i] || '';
    else if (a.startsWith('--method=')) method = a.slice('--method='.length);
    else if (/^-X./.test(a)) method = a.slice(2);
    else if (['-f', '-F', '--field', '--raw-field'].includes(a)) field = args[++i] || '';
    else if (/^--(raw-)?field=/.test(a)) field = a.slice(a.indexOf('=') + 1);
    else if (/^-[fF]./.test(a)) field = a.slice(2);
    else if (flagName(a) === '--input') fields = true;
    if (field !== null) {
      fields = true;
      if (field.startsWith('query=')) query = field.slice('query='.length);
    }
  }
  // GraphQL is always a POST, so the method says nothing: the operation does. A
  // query read from a file (`-F query=@q.graphql`) cannot be inspected, and is
  // denied rather than assumed harmless — inline it to run it.
  if (positionals(args)[0] === 'graphql') {
    if (query === null || query.startsWith('@')) return 'gh api graphql (uninspectable)';
    return /^\s*mutation\b/.test(query) ? 'gh api graphql mutation' : null;
  }
  method = method === null ? null : method.toUpperCase();
  // gh defaults to GET, but switches to POST as soon as a field is given —
  // unless the method was pinned to GET, in which case fields are query params.
  if (method === 'GET') return null;
  if (method === null && !fields) return null;
  return `gh api (${method || 'POST'})`;
}

function classifyGh(args) {
  if (args.some((a) => a === '--help' || a === '-h')) return null;
  const positional = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '-R' || args[i] === '--repo') { i++; continue; }
    if (args[i].startsWith('-')) continue;
    positional.push(args[i]);
  }
  if (args[0] === '--version' || positional.length === 0) return null;
  const [group, verb] = positional;
  if (group === 'api') return classifyGhApi(args.slice(args.indexOf('api') + 1));
  if (['help', 'version', 'search', 'status', 'browse'].includes(group)) return null;
  if (group === 'auth' && verb === 'status') return null;
  if (GH_READ_VERBS.has(verb)) return null;
  return `gh ${group}${verb ? ' ' + verb : ''}`;
}

// Does this gh invocation print the token?
function printsToken(args) {
  const pos = positionals(args);
  if (pos[0] !== 'auth') return false;
  return pos[1] === 'token' || args.includes('--show-token') || args.includes('-t');
}

// Classify every git/gh invocation in the command. Returns the first token
// print ({ token: label }) or, failing that, the first write ({ write: label }),
// or null.
function inspect(command) {
  let write = null;
  // `$(` and backticks start a command of their own, as do the separators.
  for (const seg of command.split(/&&|\|\||[;|&\n`]|\$\(/)) {
    // A trailing `)` closes a subshell or substitution, not part of the word.
    const toks = words(seg.trim()).map((t) => t.replace(/\)+$/, '')).filter(Boolean);
    let i = 0;
    for (;;) {
      if (i >= toks.length) break;
      const t = toks[i];
      if (t === '{' || t === '(') { i++; continue; } // group or subshell
      if (t.startsWith('(')) { toks[i] = t.replace(/^\(+/, ''); continue; }
      if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(t)) { i++; continue; } // env prefix
      if (WRAPPERS.has(t)) {
        i++;
        // The wrapper's own flags (and nice's `-n <num>`).
        while (i < toks.length && (toks[i].startsWith('-') || /^\d+$/.test(toks[i]))) i++;
        continue;
      }
      break;
    }
    const bin = toks[i] && path.basename(toks[i]);
    const args = toks.slice(i + 1);
    if (bin === 'gh' && printsToken(args)) return { token: 'gh auth (token)' };
    const hit = bin === 'git' ? classifyGit(args)
      : bin === 'gh' ? classifyGh(args)
        : null;
    if (hit && !write) write = hit;
  }
  return write ? { write } : null;
}

function writesAllowed() {
  return (process.env.ALLOW_GIT_WRITE || '').trim().toLowerCase() === 'yes';
}

let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { raw += c; });
process.stdin.on('end', () => {
  let data;
  try {
    data = JSON.parse(raw || '{}');
  } catch (_) {
    process.exit(0); // unparseable payload — never block
  }
  if (data.tool_name && data.tool_name !== 'Bash') process.exit(0);
  const command = (data.tool_input && data.tool_input.command) || '';
  if (!command) process.exit(0);

  const found = inspect(command);
  if (!found) process.exit(0);
  if (found.token) {
    return deny('Printing the GitHub token is never allowed: it would land in the ' +
      'transcript for good. gh and git already authenticate with it; use ' +
      '`gh auth status` (without --show-token) to check that they do.');
  }
  if (writesAllowed()) process.exit(0);

  const setVal = (process.env.ALLOW_GIT_WRITE || '').trim();
  return deny(
    `Git write operations are disabled in this deployment (ALLOW_GIT_WRITE is ` +
    `${setVal ? `"${setVal}"` : 'unset'}, not Yes) — "${found.write}" was blocked. Leave your ` +
    `changes in the working tree; commits, branches and pushes happen outside the box.`);
});

function deny(reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason,
    },
  }));
  process.exit(0);
}
