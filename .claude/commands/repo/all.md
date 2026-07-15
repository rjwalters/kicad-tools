---
name: "all"
description: "The whole hygiene pass in order — audit, docs, tidy, update-tools, reset — safe fixes by default, destructive steps gated"
domain: repo
type: command
user-invocable: true
---

# /repo:all — The Whole Hygiene Pass

Run the full sequence of sensible repo work in one go: scan for problems, bring
the docs back in line with reality, tidy filesystem clutter, refresh installed
tool packages, and land back on a clean baseline. This is the umbrella command
— it orchestrates the other `/repo:*`
commands in a deliberate order, applying each one's safe fixes by default and
keeping the same safety gates on destructive steps that each uses on its own.

It deliberately does **not** launch cloud dev sessions ([[remote]]) — that
provisions paid infrastructure and is never part of a routine hygiene pass.

## Usage

```
/repo:all                      # Full repo — apply safe fixes across stages, report as you go
/repo:all --ask                # Confirm findings before applying at every stage
/repo:all packages/core        # Scope the read-only scans to one subtree
/repo:all --prune              # Also delete confirmed-safe branches/worktrees (passed to reset, after the loss check)
```

The optional path argument scopes the scanning stages ([[audit]]) the same way
it does for those commands. Stages that act on global git or filesystem state
([[tidy]], [[reset]]) always operate on the whole repo.

## Stages

Run these in order. Each stage applies its safe, reversible fixes by default
and reports them; irreversible removals (Tidy's ASK items, Reset's
branch/worktree/stash deletion) still require explicit approval and pass the
permanent-loss check — they are never chained silently. Under `--ask`,
every stage reverts to report-first: show what was found and get a yes before
acting. If the user declines a stage, note it and continue to the next.

### 1. Audit (see [[audit]])

Run the full read-only health sweep: README accuracy, orphaned files, broken
links, gitignore issues, branch & worktree hygiene. Produce the combined audit
report. This surfaces everything before anything is touched.

Offer to fix gitignore findings here. Leave README, link, and documentation
fixes for the Docs stage next — don't apply them twice.

### 2. Docs (see [[docs]])

Bring the documentation back in line with reality: content accuracy (stale
prose, out-of-date command/feature tables, CHANGELOG drift), README structure,
and internal cross-references. This is the explicit, named home for the doc
fixes the audit surfaced — apply the ones the user approves.

### 3. Tidy (see [[tidy]])

Inventory filesystem clutter — build artifacts, caches, temp files, empty dirs
— present it grouped with sizes, and remove what the user approves.

### 4. Update tools (see [[update-tools]])

Check installed tool packages (Loom, Anvil, Repo itself, …) against their
sources. Report what's behind and offer to update.

### 5. Reset (see [[reset]])

Last, because it changes branch state and syncs with the remote. Run the
end-of-task baseline ritual: working-tree safety check, stash review, branch &
worktree pruning, `git fetch --prune`, and return to the default branch. Pass
`--prune` and `--ask` through if either was given to `/repo:all`.

Do this stage last so the earlier scans and cleanup happen while you're still on
the working branch, and you finish on a clean default branch.

## Final Summary

After all stages, print one consolidated report so nothing is silently
forgotten:

```
REPO:ALL COMPLETE
=================
Audit:        3 findings surfaced (gitignore rule fixed)
Docs:         2 fixed (README table, CHANGELOG entry), 1 deferred: docs/analysis/ missing README
Tidy:         freed 240 MB (build/, .cache/, 3 empty dirs)
Tools:        Anvil updated 1.4.0 → 1.5.1; Loom current
Reset:        on main (up to date), tree clean, 4 branches deleted, 1 stash kept
Skipped:      remote (never part of /repo:all)
```

List anything intentionally left behind — deferred findings, kept stashes,
UNKNOWN branches — so the user knows exactly what state the repo is in.

## Principles

Same as every hygiene command: **apply safe fixes, gate destructive ones**
(reversible fixes apply by default, `--ask` to confirm first, irreversible
removals always require explicit opt-in); **general by design**; **don't be
noisy**. `/repo:all` adds only sequencing — each stage keeps its own safety
gate, and no stage is skipped or its destructive actions auto-approved just
because it runs under the umbrella.
