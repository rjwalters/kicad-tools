---
name: "tidy"
description: "Tidy up the repository — build artifacts, caches, temp files, empty dirs"
domain: repo
type: command
user-invocable: true
---

# /repo:tidy — Tidy Up

Sweep the working tree for clutter and clean it up. Inventory everything,
categorize by confidence, then by default delete the SAFE category (pure junk
that holds no unique work) and report what was freed. Regenerable **caches**
(compilation/tool/build output) are kept by default and only cleared when you
pass `--caches` — deleting them is harmless but forces a costly rebuild, so it
is opt-in. Items that could be real work (ASK) are always presented for a human
call, never auto-deleted.

## Usage

```
/repo:tidy                    # Inventory, delete SAFE junk (caches kept), report; ASK items presented
/repo:tidy --caches           # Also clear regenerable caches (__pycache__/, dist/, .mypy_cache/, …)
/repo:tidy --ask              # Walk every category interactively before deleting anything
/repo:tidy packages/core      # Scope to one subtree
```

(`--apply` is accepted as a synonym for the default, for muscle memory. `--caches`
composes with `--ask`: `--ask` still walks every category, `--caches` just moves
the cache tier into the auto-delete set for the non-interactive default.)

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

**Gitignored ≠ safe to delete.** `git clean -ndX` lists *every* gitignored file
on disk — including secrets (`.env`) and expensive-to-rebuild trees (`.venv/`),
which are gitignored *precisely because* they're precious and local. Do not
treat "gitignored" as a synonym for "regenerable." SAFE and CACHE are
**allowlists** of recognized clutter (SAFE = pure junk, auto-deleted; CACHE =
regenerable build output, kept unless `--caches`); a **never-delete denylist**
overrides both; everything else gitignored falls through to ASK.

Apply these tests in order — **denylist first, then the SAFE and CACHE
allowlists, then fall through to ASK**:

**Never-delete denylist (always ASK, never SAFE or CACHE — checked first,
overrides everything below, regardless of gitignore status):**
- Secrets / credentials: `.env`, `.env.*` (but **not** `.env.example` /
  `.env.sample`, which are templates safe to keep), `*.pem`, `*.key`,
  `*.keystore`, `*.p12`, `*.pfx`, `id_rsa*`
- Expensive-to-rebuild environments: `.venv/`, `venv/`, `env/`, and
  `node_modules/` — reinstalling them costs time and network, so they are never
  auto-deleted and `--caches` does **not** reach them (they are environments, not
  caches). Surface them under ASK for an explicit human call.
- Anything else that looks credential-like or holds unique local state
  (local SQLite DBs, local-only config, sample-data caches)

A denylist match routes to **ASK** (never auto-deleted) — not KEEP, which is
reserved for tracked files.

- **SAFE** — pure junk, regenerable with certainty and holding no unique work,
  matched by an explicit **allowlist** (never "everything `git clean -ndX` lists
  minus a couple of exclusions"). Auto-deleted by default. A file is SAFE only if
  it does **not** match the denylist above **and** matches one of:
  - OS/editor droppings: `.DS_Store`, `Thumbs.db`, `*~`, `*.swp`, `.#*`
  - Merge/patch leftovers: `*.orig`, `*.rej`, `*.BACKUP.*`
  - Empty directories

  Nothing in this category may be tracked by git or match a source-code
  extension.
- **CACHE** — regenerable compilation/tool/build output. Same certainty as SAFE
  (definitely regenerable, no unique work), but deleting it forces a potentially
  slow rebuild, so it is **kept by default** and cleared **only** when `--caches`
  is passed (see Apply). A file is CACHE if it does **not** match the denylist
  and matches one of:
  - Python caches: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`,
    `.ruff_cache/`
  - Build output: stale `dist/`, `.turbo/`, `.astro/`, `htmlcov/`, `.coverage`,
    coverage output, `site/dist`

  Like SAFE, nothing here may be tracked by git or match a source-code extension.
  (`node_modules/` and virtualenvs are **not** CACHE — they are denylisted
  environments and stay in ASK even with `--caches`.)
- **ASK** — probably junk but needs a human call. This covers:
  - Untracked files that aren't gitignored (could be unsaved work!), large
    files, stale-looking logs, old `tmp/` contents.
  - **Any gitignored file that matches the never-delete denylist** (secrets,
    virtualenvs) — surfaced here, never auto-deleted.
  - **Any gitignored file that does not match the SAFE or CACHE allowlist** (a
    novel/unrecognized cache dir, unrecognized local state) — when in doubt, it
    lands here, not in SAFE or CACHE.

  Stale worktrees, branches, and stashes are [[reset]]'s job — point there
  instead of handling them here.
- **KEEP** — flagged only as information: tracked files that look like they
  don't belong (build output that got committed — point to [[gitignore]]).

### 3. Report

```
## Repo Clean — inventory

SAFE (would free 32 MB — deleted by default):
  .DS_Store × 14
  3 *.orig merge leftovers
  6 empty directories

CACHE (would free 402 MB — kept by default; pass --caches to clear):
  __pycache__/ × 22 dirs
  .mypy_cache/ (gitignored, 22 MB)
  dist/ (gitignored, 380 MB)

ASK:
  .env                     gitignored, 1 KB  ← credentials, never auto-deleted
  .venv/                   gitignored, 240 MB  ← virtualenv, expensive to rebuild
  node_modules/            gitignored, 310 MB  ← environment, reinstall via npm; not a --caches target
  notes-scratch.md         untracked, 3 KB, modified today  ← might be real work
  sim-output-old/          untracked, 1.2 GB, untouched 60 days
  worktree: ../repo-wt-fix123 (branch merged)

KEEP (informational):
  assets/build.min.js      tracked but looks generated — see /repo:gitignore
```

### 4. Apply

- Default: delete the SAFE category immediately, report the CACHE tier as kept
  (with the bytes `--caches` would free), then present ASK items for a decision.
  Never auto-delete anything in ASK, no matter the flags.
- With `--caches`: the CACHE tier joins the auto-delete set — delete SAFE **and**
  CACHE immediately, then present ASK. `--caches` never widens what counts as
  deletable beyond the CACHE allowlist; denylisted paths (secrets, virtualenvs,
  `node_modules/`) stay in ASK regardless.
- With `--ask`: walk through every category with the user, including SAFE and
  CACHE; delete only what they approve. (`--ask` already surfaces caches for a
  decision, so `--caches` is redundant with it — the flag only affects the
  non-interactive default.)

The default auto-delete is scoped to **SAFE-allowlisted paths only** (plus the
CACHE allowlist when `--caches` is passed). Never pass a denylisted path
(secrets, virtualenvs, `node_modules/`) or an unrecognized gitignored path to
`git clean -fdX` — those are ASK items and require an explicit human call. Build
the explicit `<paths>` list from the SAFE category (and CACHE under `--caches`)
and nothing else; do **not** run a blanket `git clean -fdX` that would sweep
whatever `git clean -ndX` lists.

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
6. **Gitignored ≠ safe to delete** — the never-delete denylist (secrets like
   `.env`/`*.pem`/`*.key`, and environments like `.venv/`/`venv/`/`env/` and
   `node_modules/`) always overrides SAFE and CACHE and routes to ASK, regardless
   of what `git clean -ndX` lists. Unrecognized gitignored files fall through to
   ASK, never SAFE or CACHE.
7. **Caches are opt-in** — the CACHE tier (`__pycache__/`, `dist/`, `.mypy_cache/`,
   and the other compilation/tool/build patterns) is never auto-deleted by
   default; it is cleared only when `--caches` is passed (or approved item-by-item
   under `--ask`). Deleting a cache is safe but forces a rebuild, so the default
   keeps it.
