---
name: "reset"
description: "Return the repo to a clean baseline — review stale worktrees/branches/stashes, sync with remote, land back on the default branch"
domain: repo
type: command
user-invocable: true
---

# /repo:reset — Back to Baseline

The end-of-task ritual: review and prune stale git state, sync with the
remote, and land back on the default branch with a clean working tree. The
reversible steps (fetch, land on the default branch) run by default; nothing
irreversible — dropping a stash, deleting a branch or worktree — ever happens
without an explicit opt-in and the permanent-loss check.

## Usage

```
/repo:reset                    # Run the reversible baseline steps; keep all stashes, land on default
/repo:reset --ask              # Confirm each step before acting
/repo:reset --prune            # Also delete confirmed-safe branches/worktrees (after the loss check)
```

## Two halves

The steps below split cleanly in two, and naming the split matters because
callers can schedule the halves separately:

- **Sync-and-switch** (reversible — steps 1 and 4): the working-tree safety
  check, `git fetch --all --prune`, and landing on an up-to-date default
  branch. Nothing is removed.
- **Pruning** (gated — steps 2 and 3): stash review and branch/worktree
  review. These can permanently remove work, so they keep the explicit opt-in
  and the [[branches]] permanent-loss check.

Run standalone, `/repo:reset` always runs all four steps in order, exactly as
documented — this split changes nothing here. It exists because [[all]] may run
the **sync-and-switch half early** (before its Docs stage) when the current
branch is fully pushed and behind the default branch, so that its doc stages
don't edit a stale checkout; the pruning half still runs last there, with its
gates intact.

## Steps

### 1. Working tree safety check

Refresh the remote-tracking refs **before** looking at the tree, so the one
judgment call this command asks for is made against the current remote rather
than a picture up to three steps stale:

```bash
git fetch --all --prune
git status --porcelain
```

The fetch is read-only: it updates remote-tracking refs and drops the ones that
no longer exist on the remote. It moves no local branch, touches no index, and
rewrites no working-tree file, so it is safe ahead of the safety check and
changes none of this command's ordering guarantees. `--prune` now runs once,
here, instead of at step 4 — which also means the branch & worktree review in
step 3 sees already-pruned remote-tracking refs rather than pruning them
afterwards.

If `git status --porcelain` **exits non-zero**, stop and report the error. A
failed status prints nothing, which is neither clean nor dirty — do not go on to
step 2 on the strength of empty output. Check the exit code, not just the text.

If the tree is clean (exit 0, empty output), this step is a no-op — go on to
step 2.

If the tree is dirty, stop and resolve it **first** — everything after this
step assumes no work can be lost. Before presenting the choice, gather what the
remote already knows about the dirty paths:

```bash
# The full dirty set — unstaged, staged, and untracked. `git diff --name-only`
# alone would miss the staged and untracked files that `git status --porcelain`
# reports as dirty, so an overlap check built on it under-reports silently.
dirty=$( { git diff --name-only
           git diff --cached --name-only
           git ls-files --others --exclude-standard; } | sort -u )

# Upstream context — only if the branch has one. A branch that was never
# pushed has no @{u}; skip these lines rather than erroring on it.
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  ahead=$(git rev-list --count '@{u}..HEAD')
  behind=$(git rev-list --count 'HEAD..@{u}')
  # Incoming commits that touch one of those paths. Feed the pathspecs via
  # NUL-delimited xargs rather than an unquoted `-- $dirty`: bare word
  # splitting doesn't happen at all in zsh (the whole list becomes one
  # pathspec, matching nothing — a silent "no overlap") and breaks on any
  # path containing a space in every shell.
  overlap=""
  [ -n "$dirty" ] && overlap=$(printf '%s\n' "$dirty" | tr '\n' '\0' \
    | xargs -0 git log --oneline 'HEAD..@{u}' --)
fi
```

Show the changes **and** that upstream context, then ask:
- **Commit** them (offer to draft the commit)
- **Stash** them with a descriptive message (`git stash push -m "..."`)
- **Abort** the reset and leave everything as is

Report it like this, so the state that should drive the choice is on screen
when the choice is made:

```
Dirty tree: 1 file (mcp-loom/package-lock.json)
Upstream:   ahead 0, behind 7 (origin/main)
Incoming commits touching your dirty paths:
  880a4de  fix(version): sync mcp-loom/package-lock.json in version.sh bump/check
```

- **If `overlap` is non-empty**, say so explicitly and **steer to abort or
  inspect, not commit** — the remote may already carry this change, and
  committing it locally lands a redundant commit that then blocks the
  `--ff-only` pull at step 4. Offer to show the incoming commit
  (`git show <sha>`) so the two can be compared before deciding.
- **If `behind` is 0 or `overlap` is empty**, say that too — "no incoming
  commit touches your dirty paths" is exactly what makes commit or stash the
  safe answer.
- **If the branch has no upstream**, report that the fetch and prune ran but
  that there is nothing to compare against, and omit the ahead/behind and
  overlap lines entirely.

NEVER discard changes. `git checkout --`, `git reset --hard`, and `git clean`
on tracked modifications are off the table unless the user explicitly asks.

### 2. Stash review

```bash
git stash list --format='%gd %cr %gs'
```

For each stash, show what's in it (`git stash show --stat <ref>`) and its age.
A stash is unique work and dropping it is irreversible, so **keep every stash
by default** — just report them (flagging any older than 30 days as likely
droppable). Only under `--ask` ask per stash whether to **apply**, **drop**,
or **keep**. Never auto-drop, regardless of flags.

### 3. Branch & worktree review

Run the full [[branches]] classification (PROTECTED / merged-PR / closed-issue
/ orphaned-automation / UNKNOWN, plus stale worktrees). With `--prune`, delete
the SAFE TO DELETE category after presenting it; otherwise ask. Either way,
[[branches]]' **permanent-loss check** applies — a branch whose *content* is
found nowhere else, or a worktree with uncommitted changes, is never removed
automatically, so nothing here can permanently destroy work.

Carry [[branches]]' per-branch **tag** through into the report below: it names
which check cleared each branch, so "landed (squash)" stays visibly distinct
from "no unique commits" and from work kept because it exists nowhere else. Two
consequences worth stating plainly, both from [[branches]] step 5:

- On a squash-merging repo `git branch -d` refuses every landed branch, so the
  deletion of a `landed (...)` branch legitimately uses `-D`.
- Never reach for `-D` to work around a refusal that step 5 did **not** already
  clear — `-D` deletes unmerged work just as readily, and a `unique work` or
  `unverifiable` tag means the refusal was right.

### 4. Sync with remote

Remote-tracking refs were already refreshed and pruned in step 1, and `git pull
--ff-only` fetches again on its own, so no standalone `git fetch --all --prune`
runs here. Return to the default branch and fast-forward it:

```bash
default=$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's|origin/||')
git checkout "$default"
git pull --ff-only
```

If `git checkout` fails with `fatal: '<default>' is already used by worktree at
'<path>'`, the default branch is checked out in another worktree (exit 128, HEAD
unchanged) — the ordinary case in a Loom-managed repo, which keeps a worktree
per issue. Nothing moved and nothing is at risk: name that cause and the
worktree holding the branch, and finish the run from where you are rather than
reporting a generic failure.

If `--ff-only` fails, the checkout already landed, so the run is now *on* the
local default branch and it has diverged from the remote — say so, report the
divergence (`git log --oneline @{u}..HEAD` and `HEAD..@{u}`) and ask how to
proceed. Do not rebase or force anything on your own.

### 5. Final state report

```
RESET COMPLETE
==============
Branch:    main (up to date with origin/main)
Tree:      clean
Stashes:   1 kept (stash@{0}: "wip: quantizer experiment", 3 days old)
Branches:  4 deleted, 2 UNKNOWN kept
             fix/123-parser-crash — landed (squash), merged PR #150 (2026-06-28)
             fix/88-null-deref — landed (squash), content-verified (merge-tree)
             feature/issue-77 — landed (squash), patch-id equivalent (git cherry)
             wt/agent-3 — no unique commits
             experiment-a — KEPT: unique work: 3 commits found nowhere else
             spike/cache — KEPT: unverifiable: forge lookup failed (rate limited)
Worktrees: 1 removed (../repo-wt-fix123)
```

List anything intentionally left behind so nothing is silently forgotten. A
deleted branch with no tag, or a bare `N deleted` count, is a reporting bug: the
tag is how an operator confirms after the fact that squash-landed work — not
unique work — is what went away.

## Related

- Filesystem clutter (build artifacts, caches, temp files) is [[tidy]]'s job —
  offer to run it after the reset if the inventory looked messy
- Deep branch analysis and its safety rules live in [[branches]]
