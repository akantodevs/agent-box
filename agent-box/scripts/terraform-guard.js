#!/usr/bin/env node
// Pre/PostToolUse(Bash) guard for Terraform, driven by ALLOW_TERRAFORM_MODIFY.
//
// One script, registered for BOTH PreToolUse and PostToolUse (matcher Bash); it
// branches on hook_event_name. It governs only infrastructure/state-mutating
// terraform subcommands (apply, destroy, import, state rm|mv|push|
// replace-provider, taint, untaint, force-unlock, workspace delete). Read-only
// commands (plan, validate, fmt, show, output, `state list`, ...) always pass.
//
// ALLOW_TERRAFORM_MODIFY (case-insensitive) selects the PreToolUse decision for
// a mutating command:
//   Yes  -> allow (runs without asking)
//   Ask  -> ask ONCE per terraform directory, then remember that dir
//   No   -> deny (blocked)
//   unset/unrecognized -> deny (fail-closed)
//
// Path-scoped approval (Ask mode): PreToolUse computes the terraform working dir
// (payload cwd, adjusted for `-chdir=DIR` and a leading `cd DIR &&`) and allows
// it if already approved, else asks. PostToolUse fires only when the command
// actually ran (i.e. was approved) and records that dir, so the next mutating
// command in the same dir is silent while a sibling dir (stage vs prod) still
// asks. This keeps the guardrail in the operating manual (agent-box/CLAUDE.md);
// keep the two in sync.
//
// Approvals persist in ~/.claude/terraform-approvals.json (the claude-data
// volume). Remove an entry (or the file) to re-arm prompting for that path.
//
// Fail-open on anything we can't classify (non-Bash, no command, unparseable
// payload): the guard is a safety net around recognized mutating verbs, never a
// reason to wedge Bash. The fail-CLOSED default applies only once a mutating
// terraform command has been positively identified.
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');

// Subcommands that change infrastructure or remote state.
const MUTATING = new Set(['apply', 'destroy', 'import', 'taint', 'untaint', 'force-unlock']);
// `terraform state <sub>` variants that rewrite state.
const STATE_MUTATING = new Set(['rm', 'mv', 'push', 'replace-provider']);
// `terraform workspace <sub>` variants that delete a workspace.
const WORKSPACE_MUTATING = new Set(['delete']);

const APPROVALS_FILE = path.join(os.homedir(), '.claude', 'terraform-approvals.json');

function stripQuotes(s) {
  return s.replace(/^['"]|['"]$/g, '');
}

// Find the first mutating terraform invocation and the directory it acts on.
// Returns { sub, dir } or null. `dir` is resolved from `cwd`, carrying a `cd`
// across `&&`-style chains and honoring a `-chdir=` global flag.
function findMutating(command, cwd) {
  let base = cwd || process.cwd();
  // Examine each shell segment (split on && || ; | & and newlines) on its own,
  // so `terraform plan && terraform apply` is caught on the apply.
  const segments = command.split(/&&|\|\||[;|&\n]/);
  for (const seg of segments) {
    const tokens = seg.trim().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) continue;
    // A `cd X` segment changes cwd for everything after it in the chain, so
    // `cd infra/prod && terraform apply` keys on infra/prod.
    if (tokens[0] === 'cd' && tokens[1] && !tokens[1].startsWith('-')) {
      base = path.resolve(base, stripQuotes(tokens[1]));
      continue;
    }
    for (let i = 0; i < tokens.length; i++) {
      // Match the terraform binary by basename so /usr/bin/terraform and a bare
      // `terraform` both hit. Leading `ENV=val` assignments simply don't match.
      if (tokens[i].split('/').pop() !== 'terraform') continue;
      // Walk global flags before the subcommand, capturing -chdir=DIR.
      let chdir = null;
      let j = i + 1;
      while (j < tokens.length && tokens[j].startsWith('-')) {
        const m = tokens[j].match(/^-chdir=(.+)$/);
        if (m) chdir = stripQuotes(m[1]);
        j++;
      }
      const sub = tokens[j];
      if (!sub) break;
      const dir = chdir ? path.resolve(base, chdir) : base;
      if (MUTATING.has(sub)) return { sub, dir };
      if (sub === 'state' || sub === 'workspace') {
        let k = j + 1;
        while (k < tokens.length && tokens[k].startsWith('-')) k++;
        const second = tokens[k];
        if (sub === 'state' && STATE_MUTATING.has(second)) return { sub: `${sub} ${second}`, dir };
        if (sub === 'workspace' && WORKSPACE_MUTATING.has(second)) return { sub: `${sub} ${second}`, dir };
      }
    }
  }
  return null;
}

function readApprovals() {
  try {
    const arr = JSON.parse(fs.readFileSync(APPROVALS_FILE, 'utf8'));
    return Array.isArray(arr) ? arr : [];
  } catch (_) {
    return []; // missing/garbled cache -> nothing approved yet
  }
}

function addApproval(dir) {
  const arr = readApprovals();
  if (arr.includes(dir)) return;
  arr.push(dir);
  try {
    fs.writeFileSync(APPROVALS_FILE, JSON.stringify(arr, null, 2) + '\n');
  } catch (_) { /* best-effort; never wedge the tool over a cache write */ }
}

// Normalize ALLOW_TERRAFORM_MODIFY to yes|ask|no; unknown -> no (fail-closed).
function mode() {
  const v = (process.env.ALLOW_TERRAFORM_MODIFY || '').trim().toLowerCase();
  return (v === 'yes' || v === 'ask' || v === 'no') ? v : 'no';
}

function emitDecision(decision, reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: decision,
      permissionDecisionReason: reason,
    },
  }));
  process.exit(0);
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

  const hit = findMutating(command, data.cwd);
  if (!hit) process.exit(0); // not a mutating terraform command — pass

  const m = mode();
  const event = data.hook_event_name || 'PreToolUse';

  if (event === 'PostToolUse') {
    // The command ran (it wasn't denied). In ask mode, remember its directory.
    if (m === 'ask') addApproval(hit.dir);
    process.exit(0);
  }

  // PreToolUse: decide.
  if (m === 'yes') process.exit(0); // allow, no prompt

  if (m === 'ask') {
    if (readApprovals().includes(hit.dir)) process.exit(0); // already approved here
    return emitDecision('ask',
      `Terraform "${hit.sub}" in ${hit.dir} can change real infrastructure or state. ` +
      `ALLOW_TERRAFORM_MODIFY=Ask requires explicit confirmation before running it. ` +
      `Approving remembers this directory for future runs; other paths (e.g. stage vs ` +
      `prod) still ask separately.`);
  }

  // m === 'no': either explicitly set, or unset/unrecognized (fail-closed).
  const setVal = (process.env.ALLOW_TERRAFORM_MODIFY || '').trim();
  return emitDecision('deny', setVal
    ? `ALLOW_TERRAFORM_MODIFY=${setVal} — infrastructure-mutating Terraform ("${hit.sub}") ` +
      `is disabled for this deployment.`
    : `ALLOW_TERRAFORM_MODIFY is unset — infrastructure-mutating Terraform ("${hit.sub}") is ` +
      `blocked by default (fail-closed). Set ALLOW_TERRAFORM_MODIFY to Ask or Yes to enable.`);
});
