---
name: "branches"
description: "Audit local branches and worktrees — find merged PRs, orphaned worktree branches, and stale worktrees"
domain: repo
type: command
user-invocable: true
---

# /repo:branches — Branch & Worktree Hygiene

Find stale local branches and worktrees that can be safely removed. Reports
findings and waits for confirmation before deleting anything.

## Usage

```
/repo:branches                   # Full audit
/repo:branches --prune           # Delete confirmed-safe branches after reporting
```

## Steps

### 1. Inventory

Gather current state:

```bash
# Default branch
git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|origin/||'

# Count local branches
git branch --list | wc -l

# List worktrees
git worktree list

# Identify active worktree branches (these are PROTECTED)
git worktree list --porcelain | grep '^branch ' | sed 's|branch refs/heads/||'
```

### 2. Categorize branches

For every local branch, classify it into one of these buckets:

#### PROTECTED (never delete)
- The default branch (`main`/`master`) and the currently checked-out branch
- Any branch currently checked out by a worktree
- Any branch with an **open** PR (`gh pr list --head <branch> --state open`)
- Long-lived branches the repo's own docs (CLAUDE.md, CONTRIBUTING.md) name as
  release/project branches — if such a list exists, honor it

#### MERGED PR BRANCHES
- Branches matching common PR patterns: `feature/*`, `fix/*`, `feat/*`, `pr-*`
- Check if a PR exists and is merged:
  `gh pr list --head <branch> --state merged --json number --jq length`
- If the PR is merged, the branch is safe to delete
- Also safe: any branch fully merged into the default branch
  (`git branch --merged <default>`)

#### CLOSED ISSUE BRANCHES
- Branches whose names embed an issue number (e.g. `feature/issue-123`,
  `loom/issue-123`)
- Check the linked issue: `gh issue view <number> --json state --jq .state`
- If the issue is CLOSED and no open PR exists for the branch, it's safe to delete

#### ORPHANED AUTOMATION BRANCHES
- Ephemeral branches created by tooling and abandoned — e.g. `worktree-agent-*`,
  `sync/*`, `wt/*` (Loom and similar orchestrators create these)
- Safe to delete when no active worktree uses them

#### UNKNOWN
- Any branch that doesn't match the above patterns
- Report these for manual review, do NOT auto-delete

### 3. Check worktrees for active automation

If the repo uses Loom (a `.loom/` directory exists), check each worktree's
linked issue for active labels before treating it as stale:

```bash
issue=$(echo "$branch" | grep -oE 'issue-[0-9]+' | grep -oE '[0-9]+')
gh issue view "$issue" --json state,labels --jq '[.state, (.labels[].name)] | join(",")'
```

Active labels (`loom:building`, `loom:review-requested`,
`loom:changes-requested`) mean a builder is mid-work — do NOT remove.

### 4. Present findings

```
BRANCH AUDIT
============

Local branches: 53
Worktrees: 4

SAFE TO DELETE (32 branches):
  Merged PR branches: 15
    fix/123-parser-crash (PR #150, merged 2026-06-28)
    ...
  Closed issue branches: 3
  Orphaned automation branches: 14

PROTECTED (10 branches):
  main
  feature/issue-462 (worktree active)
  ...

UNKNOWN (11 branches):
  experiment-quantizer — no PR found, no issue linked
  ...

STALE WORKTREES (0):
  (none)
```

### 5. If `--prune` flag is set

**Before deleting anything, run the permanent-loss check.** No branch or
worktree is removed until it passes — irreversible removal of work that exists
nowhere else is never acceptable, `--prune` or not.

The check runs in two stages, plus one fixed rule about which way it fails.

#### 5a. Ancestry — does the branch carry commits that exist nowhere else?

For every branch about to be deleted, list commits that live *only* on that
branch — not reachable from the default branch and not present on any remote:

```bash
git log --oneline <branch> --not <default> --remotes
```

- **Empty** → the branch carries no commits of its own; safe to delete.
- **Non-empty** → this proves nothing yet. Go to 5b before concluding.

`--not` is a **toggle**, not a per-argument negation: it flips the sense of every
ref that follows it, up to the next `--not`. Both exclusions must therefore sit
after a **single** `--not`. Writing it as
`<branch> --not --remotes --not <default>` flips the sense back to positive and
folds all of `<default>`'s own commits into the output, so the exclusion silently
stops working — the check then reports commits the branch never had, and returns
empty only when unrelated refs happen to cover the tip. **Never add a second
`--not`.** If `--remotes` is too broad for the repo, spell the exclusions out
instead: `git log --oneline <branch> --not <default> origin/<default>`.

#### 5b. Content — does `<default>` already contain this work?

A non-empty 5a result does **not** mean the work would be lost. A squash-merge
replays the branch's changes as one brand-new commit whose parent is
`<default>`'s prior tip, so the branch's original commits never become ancestors
of `<default>` and *always* appear in 5a — even when every line of the work is
already merged. Decide with **content containment**, not SHA ancestry:

```bash
# Would merging the branch into <default> change anything at all?
# `--write-tree` requires git >= 2.38; on older git this errors and you take the fallback below.
[ "$(git merge-tree --write-tree <default> <branch>)" = "$(git rev-parse '<default>^{tree}')" ]
```

Equal trees → `<default>` already holds the branch's content → safe to delete.
If `git merge-tree` is unsupported (git < 2.38) or reports a conflict, fall back
to the exact-match form `git diff --quiet <default> <branch>`: exit 0 (identical
trees) is also proof of containment, while a non-zero exit proves nothing either
way. Failing both, ask the forge whether the branch's PR was merged:

```bash
gh pr list --head <branch> --state merged --json number --jq length
```

A count `>= 1` means the work landed through that PR; safe to delete.

If none of these establish containment, the 5a commits would be **permanently
lost**. Do NOT auto-delete even under `--prune`. Reclassify the branch as
UNKNOWN, show the count and the commit subjects, and require an explicit
per-branch confirmation.

**Do NOT "simplify" 5b to `git branch --merged <default>`.** That lists only
branches whose commits are literal ancestors of `<default>`, which is never true
after a squash-merge — every squash-merged branch would be classified unsafe and
`--prune` would silently stop pruning anything, defeating the feature with no
error to notice. `--merged` answers "are these exact commits on `<default>`";
pruning needs "does `<default>` already have this work". Different questions.

#### 5c. Failure direction — ambiguity and errors always mean KEEP

If any command in 5a or 5b exits non-zero unexpectedly, emits output that cannot
be parsed, or cannot run at all — `gh` missing, unauthenticated, or rate-limited;
no network; an unknown or ambiguous ref; `git merge-tree` unsupported — the
branch is classified **UNKNOWN / KEEP**. **Never SAFE.** Ambiguity is never
resolved in favour of deletion, and this holds under `--prune` exactly as it does
without it. A branch wrongly kept costs one line of report noise; a branch
wrongly deleted costs the work.

For each worktree about to be removed, refuse if it has uncommitted changes —
that work exists nowhere else:

```bash
if [ -n "$(git -C <path> status --porcelain)" ]; then
  echo "SKIP <path>: uncommitted changes — resolve before removing"
else
  git worktree remove <path>          # no --force; only remove clean worktrees
fi
```

Then delete the branches that passed the loss check:

```bash
git branch -d <branch_name>           # -d (safe): refuses if not merged
# escalate to -D only for a branch the loss check proved is pushed, or whose
# content 5b proved is already contained in <default> (the squash-merge case,
# where -d refuses because the original SHAs are not ancestors of <default>)
```

Report what was deleted, what was skipped for potential data loss, and what
remains.

### 6. If no `--prune` flag

End with:
```
To delete safe branches, run: /repo:branches --prune
To investigate unknown branches: git log --oneline -5 <branch>
```

## Safety Rules

1. **NEVER delete a branch that has an active worktree** — `git worktree remove` first
2. **NEVER delete branches with open PRs** — even if the issue is closed
3. **NEVER delete branches named as long-lived by the repo's own docs**
4. **Always report before deleting** — the user must see the full list before `--prune` acts
5. **When in doubt, classify as UNKNOWN** — let the user decide
6. **Never destroy unique work** — run the permanent-loss check (step 5) before
   any deletion; a branch with commits found nowhere else, or a worktree with
   uncommitted changes, is never removed automatically, regardless of flags

## Notes

- PR/issue lookups need the `gh` CLI and GitHub auth; without them, fall back
  to `git branch --merged` analysis only and say so in the report
- Rate limiting: if there are hundreds of branches, batch `gh` calls
- Remote branch pruning is NOT done by this command; to prune stale remote
  tracking refs: `git fetch --prune` (safe, only removes local refs to deleted
  remote branches)
