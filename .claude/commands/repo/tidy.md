---
name: "tidy"
description: "Tidy up the repository — build artifacts, caches, temp files, empty dirs"
domain: repo
type: command
user-invocable: true
---

# /repo:tidy — Tidy Up

Sweep the working tree for clutter and clean it up. Inventory everything,
categorize by confidence, then by default delete the SAFE category (clutter
that is regenerable with certainty and holds no unique work) and report what
was freed. Items that could be real work (ASK) are always presented for a human
call, never auto-deleted.

## Usage

```
/repo:tidy                    # Inventory, delete the SAFE category, report; ASK items presented
/repo:tidy --ask              # Walk every category interactively before deleting anything
/repo:tidy packages/core      # Scope to one subtree
```

(`--apply` is accepted as a synonym for the default, for muscle memory.)

## Steps

### 1. Inventory

Gather candidates without deleting anything:

```bash
# Ignored files that exist on disk (usually build output/caches)
git clean -ndX

# Untracked files (may include work-in-progress — treat carefully)
git clean -nd

# Empty directories
find . -type d -empty -not -path './.git/*'

# Large files in the working tree (>10 MB, tracked or not)
find . -type f -size +10M -not -path './.git/*' -not -path './node_modules/*'

# Stale worktrees
git worktree list
```

Also look for junk by pattern, wherever it lives:
- OS/editor droppings: `.DS_Store`, `Thumbs.db`, `*~`, `*.swp`, `.#*`
- Python: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
- JS: `node_modules/` outside package roots, stale `dist/`, `.turbo/`, coverage output
- Logs and temp files: `*.log`, `*.tmp`, `tmp/` contents older than a week
- Merge/patch leftovers: `*.orig`, `*.rej`, `*.BACKUP.*`

### 2. Categorize

- **SAFE** — regenerable with certainty: gitignored build output and caches,
  OS/editor droppings, `__pycache__`, empty directories. Nothing in this
  category may be tracked by git or match a source-code extension.
- **ASK** — probably junk but needs a human call: untracked files that aren't
  gitignored (could be unsaved work!), large files, stale-looking logs, old
  `tmp/` contents. Stale worktrees, branches, and stashes are [[reset]]'s
  job — point there instead of handling them here.
- **KEEP** — flagged only as information: tracked files that look like they
  don't belong (build output that got committed — point to [[gitignore]]).

### 3. Report

```
## Repo Clean — inventory

SAFE (would free 412 MB):
  .DS_Store × 14
  __pycache__/ × 22 dirs
  dist/ (gitignored, 380 MB)
  6 empty directories

ASK:
  notes-scratch.md         untracked, 3 KB, modified today  ← might be real work
  sim-output-old/          untracked, 1.2 GB, untouched 60 days
  worktree: ../repo-wt-fix123 (branch merged)

KEEP (informational):
  assets/build.min.js      tracked but looks generated — see /repo:gitignore
```

### 4. Apply

- Default: delete the SAFE category immediately, then present ASK items for a
  decision. Never auto-delete anything in ASK, no matter the flags.
- With `--ask`: walk through every category with the user, including SAFE;
  delete only what they approve.

Use `git clean -fdX -- <paths>` for gitignored artifacts and plain `rm` only
for pattern-matched junk you listed in the report. After deleting, re-run the
inventory to confirm and report bytes freed.

## Safety Rules

1. **Never delete tracked files** — that's a git operation the user does deliberately
2. **Never touch `.git/`** internals
3. **Untracked ≠ junk** — an untracked file modified recently is presumed to be
   unsaved work and always lands in ASK
4. **Everything deleted must have appeared in the report first**
5. When scoped to a subtree, do not delete anything outside it
