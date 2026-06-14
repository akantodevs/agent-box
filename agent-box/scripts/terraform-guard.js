#!/usr/bin/env node
// PreToolUse(Bash) guard for Terraform.
//
// Reads the hook payload on stdin and, when the Bash command would mutate real
// infrastructure or remote state, returns permissionDecision "ask" so Claude
// Code prompts for confirmation BEFORE running it — even under
// --dangerously-skip-permissions. Read-only commands (plan, validate, fmt,
// show, output, `state list`, `state show`, `workspace list/select`, ...) pass
// straight through. This enforces the Terraform guardrail documented in the
// operating manual (agent-box/CLAUDE.md); keep the two in sync.
//
// Anything we can't confidently classify is allowed (fail-open): the guard is a
// safety net around the obviously-destructive verbs, not a parser for every
// possible shell construction. A non-zero/garbled exit must never wedge Bash.
'use strict';

// Subcommands that change infrastructure or remote state.
const MUTATING = new Set(['apply', 'destroy', 'import', 'taint', 'untaint', 'force-unlock']);
// `terraform state <sub>` variants that rewrite state.
const STATE_MUTATING = new Set(['rm', 'mv', 'push', 'replace-provider']);
// `terraform workspace <sub>` variants that delete a workspace.
const WORKSPACE_MUTATING = new Set(['delete']);

function findMutatingSubcommand(command) {
  // Examine each shell segment (split on && || ; | & and newlines) on its own,
  // so `terraform plan && terraform apply` is caught on the apply.
  const segments = command.split(/&&|\|\||[;|&\n]/);
  for (const seg of segments) {
    const tokens = seg.trim().split(/\s+/).filter(Boolean);
    for (let i = 0; i < tokens.length; i++) {
      // Match the terraform binary by basename so /usr/bin/terraform and a bare
      // `terraform` both hit. Leading `ENV=val` assignments and `cd x` simply
      // don't match and are skipped.
      if (tokens[i].split('/').pop() !== 'terraform') continue;
      // The subcommand is the first following token that isn't a global flag
      // (-chdir=..., -help, ...).
      let j = i + 1;
      while (j < tokens.length && tokens[j].startsWith('-')) j++;
      const sub = tokens[j];
      if (!sub) break;
      if (MUTATING.has(sub)) return sub;
      if (sub === 'state' || sub === 'workspace') {
        let k = j + 1;
        while (k < tokens.length && tokens[k].startsWith('-')) k++;
        const second = tokens[k];
        if (sub === 'state' && STATE_MUTATING.has(second)) return `${sub} ${second}`;
        if (sub === 'workspace' && WORKSPACE_MUTATING.has(second)) return `${sub} ${second}`;
      }
    }
  }
  return null;
}

let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { raw += c; });
process.stdin.on('end', () => {
  let command = '';
  try {
    const data = JSON.parse(raw || '{}');
    if (data.tool_name && data.tool_name !== 'Bash') process.exit(0);
    command = (data.tool_input && data.tool_input.command) || '';
  } catch (_) {
    process.exit(0); // unparseable payload — never block
  }
  if (!command) process.exit(0);

  const sub = findMutatingSubcommand(command);
  if (!sub) process.exit(0);

  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'ask',
      permissionDecisionReason:
        `Terraform "${sub}" can change real infrastructure or state. The ` +
        `agent-box guardrails require explicit user confirmation before running ` +
        `infrastructure-mutating Terraform commands — ask the user to approve.`,
    },
  }));
  process.exit(0);
});
