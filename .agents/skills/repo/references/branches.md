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
- Any branch with an **open** PR:
  `gh api "repos/{owner}/{repo}/pulls?state=open&head={owner}:<branch>" --jq length`
- Long-lived branches the repo's own docs (CLAUDE.md, CONTRIBUTING.md) name as
  release/project branches — if such a list exists, honor it

#### MERGED PR BRANCHES
- Branches matching common PR patterns: `feature/*`, `fix/*`, `feat/*`, `pr-*`
- Check whether a PR exists for the branch's head and is merged (the REST form
  below) — a count `>= 1` means the branch is safe to delete
- Also safe: any branch fully merged into the default branch
  (`git branch --merged <default>`). Reachability is **sufficient but not
  necessary** — a squash-merged branch is never `--merged`, so its absence from
  that list means nothing. Step 5 decides.

```bash
gh api "repos/{owner}/{repo}/pulls?state=all&head={owner}:<branch>" \
  --jq '[.[] | select(.merged_at != null)] | length'
```

REST's `pulls` endpoint has no `state=merged` value — only `open`, `closed`, and
`all` — so merged-ness is a **client-side** filter on `.merged_at != null` over
`state=all`. An older closed-but-unmerged PR on the same head is correctly
excluded by it. Step 5b arm 4 runs the same call for the final verdict.

#### CLOSED ISSUE BRANCHES
- Branches whose names embed an issue number (e.g. `feature/issue-123`,
  `loom/issue-123`)
- Check the linked issue:
  `gh api repos/{owner}/{repo}/issues/<number> --jq .state`
- REST returns a **lowercase** state (`open`/`closed`); GraphQL's enum form was
  uppercase (`OPEN`/`CLOSED`), so compare against the lowercase string
- If the issue is `closed` and no open PR exists for the branch, it's safe to delete

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
# REST, not a `gh issue`/`gh pr` subcommand with --json — see step 5b arm 4 for
# why. The state field is lowercase here (`open`/`closed`), not GraphQL's
# `OPEN`/`CLOSED`; the label names are unaffected.
gh api "repos/{owner}/{repo}/issues/$issue" --jq '[.state, (.labels[].name)] | join(",")'
```

Active labels (`loom:building`, `loom:review-requested`,
`loom:changes-requested`) mean a builder is mid-work — do NOT remove.

### 4. Present findings

Every branch line carries the **tag of the check that cleared (or failed to
clear) it** in step 5, so the operator can see which rule applied. "This work
landed as a squash commit" and "this branch never had unique commits" and "this
work exists nowhere else" are three different states, and a report that collapses
them to a bare `SAFE TO DELETE` hides the only distinction that matters. A SAFE
line with no tag is a bug — it means the branch was never run through the loss
check.

```
BRANCH AUDIT
============

Local branches: 53
Worktrees: 4

SAFE TO DELETE (32 branches):      [tag = which step-5 check cleared it]
  Merged PR branches: 15
    fix/123-parser-crash — landed (squash), merged PR #150 (2026-06-28)
    fix/88-null-deref — landed (squash), content-verified (merge-tree)
    ...
  Closed issue branches: 3
    feature/issue-77 — landed (squash), patch-id equivalent (git cherry)
  Orphaned automation branches: 14
    wt/agent-3 — no unique commits

PROTECTED (10 branches):
  main
  feature/issue-462 (worktree active)
  ...

UNKNOWN (11 branches):
  experiment-quantizer — unique work: 3 commits found nowhere else
  spike/cache — unverifiable: forge lookup failed (rate limited)
  ...

STALE WORKTREES (0):
  (none)
```

The tag vocabulary is fixed — exactly one per branch, naming the check that
fired:

| Tag | Meaning | Set by |
|-----|---------|--------|
| `no unique commits` | the branch adds nothing that isn't already reachable elsewhere | 5a (empty) |
| `landed (squash), content-verified (merge-tree)` | merging it into `<default>` would change nothing | 5b arm 1 |
| `landed, identical tree` | the branch's tree equals `<default>`'s | 5b arm 2 |
| `landed (squash), patch-id equivalent (git cherry)` | every commit's patch is already in `<default>` | 5b arm 3 |
| `landed (squash), merged PR #N (<date>)` | the forge says the branch's PR merged | 5b arm 4 |
| `landed (squash), merged PR #N via <head-ref> (<date>)` | the branch's tip commit is in a merged PR opened from a *different* head | 5b arm 4b |
| `unique work: N commits found nowhere else` | no arm could establish containment | 5b exhausted → KEEP |
| `unverifiable: <reason>` | a check errored or could not run | 5c → KEEP |

Only the first five are deletable. The last two classify UNKNOWN and are never
auto-removed, `--prune` or not.

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

- **Empty** → the branch carries no commits of its own; safe to delete, tagged
  `no unique commits`.
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

#### 5b. Content and merge state — does `<default>` already have this work?

A non-empty 5a result does **not** mean the work would be lost. A squash-merge
replays the branch's changes as one brand-new commit whose parent is
`<default>`'s prior tip, so the branch's original commits never become ancestors
of `<default>` and *always* appear in 5a — even when every line of the work is
already merged. On a squash-merging repo that describes **every merged branch**,
so a reachability answer on its own would protect all of them and `--prune`
would never delete a single one.

Decide with **content containment and forge merge state**, not SHA ancestry. Try
the arms below in order and stop at the first that proves containment; the arm
that fired is the branch's report tag (step 4). Reaching the end without a proof
is not a failure of the check — it is the check working.

**Arm 1 — tree containment** → `landed (squash), content-verified (merge-tree)`

```bash
# Would merging the branch into <default> change anything at all?
# `--write-tree` requires git >= 2.38; on older git this errors and you take arm 2.
[ "$(git merge-tree --write-tree <default> <branch>)" = "$(git rev-parse '<default>^{tree}')" ]
```

Equal trees → `<default>` already holds the branch's content → safe to delete.

**Arm 2 — exact tree match** → `landed, identical tree`

If `git merge-tree` is unsupported (git < 2.38) or reports a conflict, fall back
to the exact-match form `git diff --quiet <default> <branch>`: exit 0 (identical
trees) is also proof of containment, while a non-zero exit proves nothing either
way.

**Arm 3 — patch-id equivalence** → `landed (squash), patch-id equivalent (git cherry)`

The forge-independent squash detector, and the arm that rescues the common case
arms 1 and 2 cannot: a branch that landed *and* whose files `<default>` has since
edited, so the trees no longer agree at all.

```bash
git cherry <default> <branch>     # '-' = this patch is already in <default>, '+' = it is not
```

Containment is proven only when the output is non-empty and **every** line starts
with `-`. A single `+` means at least one commit's patch is not in `<default>` —
fall through to arm 4, do not delete. Know the limit: patch-ids are per commit,
so a branch of *several* commits squashed into one lands as a single patch that
matches none of them individually and correctly reports `+`. `git cherry` clears
the single-commit squash (much the commonest case) and stays quiet otherwise; it
is never a reason to delete on its own.

**Arm 4 — forge merge state** → `landed (squash), merged PR #N (<date>)`

Ask the forge whether the branch's head had a merged PR. This is the only arm
that can clear a branch whose content `<default>` has moved past entirely. **One**
REST call answers the containment question and supplies the number and merge date
the report tag needs — do not put a separate count query in front of it:

```bash
gh api "repos/{owner}/{repo}/pulls?state=all&head={owner}:<branch>" \
  --jq '[.[] | select(.merged_at != null)] | "\(length) \(.[0].number // "") \((.[0].merged_at // "")[0:10])"'
```

`gh` substitutes `{owner}` and `{repo}` from the current repository; `<branch>`
is the branch name. A leading count `>= 1` means the work landed through that
PR — safe to delete — and the same line carries the number and date the tag
needs. Empty output or a non-zero exit is 5c, not "no merged PR".

Use `gh api` for this and for any **new** forge call added here. Every `gh
pr`/`gh issue` subcommand invoked with `--json` is GraphQL-backed and spends the
much smaller GraphQL rate-limit budget, which on a busy multi-agent host is
routinely exhausted while the REST `core` bucket sits nearly unused — and a
rate-limited forge lookup here costs a branch its verdict (5c: KEEP), so the
cheaper bucket is the one to spend. REST's `pulls` endpoint has no
`state=merged` value — only `open`, `closed`, and `all` — so merged-ness is the
client-side `.merged_at != null` filter above, which correctly ignores an older
closed-but-unmerged PR on the same head.

**Arm 4b — commit containment via forge** → `landed (squash), merged PR #N via <head-ref> (<date>)`

Only run when arm 4 finds nothing. Arm 4 keys on the **branch's own name** as
the PR head, which misses a review/scratch branch that tracks *someone else's*
PR head — the motivating case is a local `pr-<N>-review`-style branch checked
out to review PR #N, where PR #N was itself opened from a differently-named
branch (e.g. `fix/...`). Once that PR squash-merges, the review branch matches
none of arms 1–4: `<default>` has moved on so the trees differ, `git cherry`
reports `+` (a squash never patch-id matches), and no PR was ever opened from
the review branch's own head name — yet the work is fully landed.

`GET repos/{owner}/{repo}/commits/{commit_sha}/pulls` answers this directly: it
lists every PR containing a given commit, **regardless of what head it was
opened from**. Resolve the branch's tip SHA first, then make the one REST call:

```bash
tip=$(git rev-parse <branch>)
gh api "repos/{owner}/{repo}/commits/${tip}/pulls" \
  --jq '[.[] | select(.merged_at != null)] | "\(length) \(.[0].number // "") \(.[0].head.ref // "") \((.[0].merged_at // "")[0:10])"'
```

A leading count `>= 1` means the branch's tip commit is contained in a merged
PR — safe to delete — and the same line carries the PR number, the head branch
that PR was actually opened from (`.head.ref`, not the review branch's own
name), and the merge date. The report tag is deliberately distinct from arm
4's — it adds `via <head-ref>` — so the operator can see the branch name never
matched: `landed (squash), merged PR #N via <head-ref> (<date>)`.

Same failure direction as every other arm: a non-zero exit, an unresolvable
tip SHA, or unparseable output is 5c (`unverifiable: ...`), never SAFE.

If no arm establishes containment, the 5a commits would be **permanently
lost**. Do NOT auto-delete even under `--prune`. Reclassify the branch as
UNKNOWN, tag it `unique work: N commits found nowhere else`, show the commit
subjects, and require an explicit per-branch confirmation. No PR and no
patch-id match is precisely the case the protective default exists for.

**Do NOT "simplify" 5b to `git branch --merged <default>`.** That lists only
branches whose commits are literal ancestors of `<default>`, which is never true
after a squash-merge — every squash-merged branch would be classified unsafe and
`--prune` would silently stop pruning anything, defeating the feature with no
error to notice. `--merged` answers "are these exact commits on `<default>`";
pruning needs "does `<default>` already have this work". Different questions.

#### 5c. Failure direction — ambiguity and errors always mean KEEP

If any command in 5a or 5b exits non-zero unexpectedly, emits output that cannot
be parsed, or cannot run at all — `gh` missing, unauthenticated, or rate-limited;
no network; an unknown or ambiguous ref; no containment arm able to run — the
branch is classified **UNKNOWN / KEEP** and tagged `unverifiable: <reason>`.
**Never SAFE.** (`git merge-tree` being unsupported is not by itself an error:
that is what arms 2 and 3 are for, and both run offline on any git. It becomes
5c only when no arm can answer.) Ambiguity is never resolved in favour of deletion, and this holds
under `--prune` exactly as it does without it. A branch wrongly kept costs one
line of report noise; a branch wrongly deleted costs the work.

For each worktree about to be removed, refuse if it has uncommitted changes —
that work exists nowhere else:

```bash
if [ -n "$(git -C <path> status --porcelain)" ]; then
  echo "SKIP <path>: uncommitted changes — resolve before removing"
else
  git worktree remove <path>          # no --force; only remove clean worktrees
fi
```

Then delete the branches that passed the loss check. **Which flag to use follows
from the branch's step-4 tag, never from what git happens to accept:**

```bash
# tag `no unique commits`  -> -d succeeds on its own
git branch -d <branch_name>

# tag `landed (...)`       -> -d WILL refuse: after a squash-merge the branch's
#                             original SHAs are not ancestors of <default>. 5b
#                             already proved the content is contained, so the
#                             refusal carries no information here. Escalate:
git branch -D <branch_name>
```

> **`git branch -d` is not the classifier — and `-D` is not the fix.**
> On a squash-merging repo `-d` refuses *every* landed branch, because squashing
> makes the branch's original commits unreachable from `<default>` forever. Git's
> hint text on that refusal literally suggests re-running with `-D`, and taking
> the hint is the inverse failure: `-D` ignores merge state entirely and deletes
> genuinely unmerged work just as happily. One refuses everything, the other
> refuses nothing; **neither can tell "landed" from "lost"** — that is what step 5
> is for. Escalate to `-D` only for a branch step 5 tagged `landed (...)`. If the
> tag is `unique work` or `unverifiable`, the `-d` refusal is correct: leave the
> branch alone and let a human look at it.

Report what was deleted **with each branch's tag**, what was skipped for
potential data loss, and what remains.

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
   any deletion; a branch whose *content* is found nowhere else, or a worktree
   with uncommitted changes, is never removed automatically, regardless of flags.
   "Found nowhere else" means content, not reachability: a squash-merge always
   breaks reachability, so `git branch -d`'s refusal (or the absence of a branch
   from `git branch --merged`) is not evidence of unique work

## Notes

- PR/issue lookups need the `gh` CLI and GitHub auth; without them, arms 1–3 of
  the loss check (`merge-tree`, `diff --quiet`, `git cherry`) still work fully
  offline — fall back to those, and say so in the report. Do **not** fall back to
  `git branch --merged`: it answers a different question and calls every
  squash-merged branch unsafe
- Rate limiting: if there are hundreds of branches, batch `gh` calls
- Remote branch pruning is NOT done by this command; to prune stale remote
  tracking refs: `git fetch --prune` (safe, only removes local refs to deleted
  remote branches)
