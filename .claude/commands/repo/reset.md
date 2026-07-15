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

## Steps

### 1. Working tree safety check

```bash
git status --porcelain
```

If the tree is dirty, stop and resolve it **first** — everything after this
step assumes no work can be lost. Show the changes and ask:
- **Commit** them (offer to draft the commit)
- **Stash** them with a descriptive message (`git stash push -m "..."`)
- **Abort** the reset and leave everything as is

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
[[branches]]' **permanent-loss check** applies — a branch with commits found
nowhere else, or a worktree with uncommitted changes, is never removed
automatically, so nothing here can permanently destroy work.

### 4. Sync with remote

```bash
git fetch --all --prune
```

Then return to the default branch and fast-forward it:

```bash
default=$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's|origin/||')
git checkout "$default"
git pull --ff-only
```

If `--ff-only` fails, the local default branch has diverged from the remote —
report the divergence (`git log --oneline @{u}..HEAD` and `HEAD..@{u}`) and
ask how to proceed. Do not rebase or force anything on your own.

### 5. Final state report

```
RESET COMPLETE
==============
Branch:    main (up to date with origin/main)
Tree:      clean
Stashes:   1 kept (stash@{0}: "wip: quantizer experiment", 3 days old)
Branches:  4 deleted, 2 UNKNOWN kept (experiment-a, spike/cache)
Worktrees: 1 removed (../repo-wt-fix123)
```

List anything intentionally left behind so nothing is silently forgotten.

## Related

- Filesystem clutter (build artifacts, caches, temp files) is [[tidy]]'s job —
  offer to run it after the reset if the inventory looked messy
- Deep branch analysis and its safety rules live in [[branches]]
