---
name: "readme"
description: "Check README accuracy against actual directory contents and offer to update"
domain: repo
type: command
user-invocable: true
---

# /repo:readme — README Check

Validate that READMEs accurately describe the directories they live in. Catches
stale file trees, incorrect descriptions, and missing READMEs in significant
directories.

This is the README-structure layer of [[docs]]. Use it directly when that's all
you want to check; use [[docs]] for the full documentation sweep.

## Usage

```
/repo:readme                    # Scan all READMEs in repo
/repo:readme packages/core      # Scope to one subtree
/repo:readme docs/README.md     # Check a specific README
/repo:readme --ask              # Review findings and confirm before applying
```

By default, apply the safe, reversible fixes as you find them — updating stale
listings, correcting wrong annotations — and report each change (they're all
git-reversible). Run with `--ask` to review first: present the findings and
confirm before touching anything. Writing a brand-new README is a judgment
call, so it's always proposed, never written unattended, in either mode.

## What It Checks

### 1. File Tree Accuracy
If a README contains an ASCII file tree (```...``` block with `├──` or `└──`),
compare it against the actual directory listing:
- Files/dirs listed in README but missing on disk
- Files/dirs on disk but not listed in README
- Descriptions that are factually wrong (e.g., "gitignored" when it's tracked)

### 2. Missing READMEs
GitHub renders a directory's `README.md` right below its file listing, so a
README at each level a person browses to makes the repo read as documented
instead of a bare file dump. Flag directories that a human would actually
navigate to in the web UI and that lack one, scaled by how significant the
directory is:

- **warn** — top-level directories and package/project roots (the first thing a
  visitor clicks into)
- **info** — meaningful mid-level groupings: source packages, `docs/`
  subsections, grouped assets, anything with several files and a clear purpose
- **skip** — directories where a README would be noise: generated/build output,
  `node_modules/`, `.git/`, `__pycache__/`, virtualenvs, and trivial leaf dirs
  that are self-evident from their name and contents (e.g. a flat `images/`)

**Depth cutoff:** limit the browsability check to directories within **two
levels of the repo root** — the levels a visitor actually clicks through in the
web UI. Deeper nesting is skipped by default, since nobody browses there
casually; the one exception is a package/project root found deeper (e.g. a
monorepo `packages/<name>/`), which is browsable in its own right and still
warrants a README.

The goal is a clean browsing experience at every level someone would land on —
not a README in literally every folder. When in doubt on a small directory,
prefer **info** over **warn**, and don't flag the obviously self-explanatory.

### 3. Stale Content
- References to removed files or directories
- "TODO" or "WIP" markers older than 30 days (check git blame)
- References to tools or workflows that no longer exist in the repo

## Interaction

By default, fix each factual discrepancy (add/remove entries, correct wrong
annotations) and report what changed. Under `--ask`, show each finding
first and ask whether to **update**, **skip**, or **show the README** before
deciding.

Whether applied or confirmed, preserve the README's existing style and voice —
just fix the factual inaccuracies, never rewrite prose to "improve" it.

### Verify after write

Applying a README fix is not proof it survived. A concurrent writer — another
agent working in the same clone, a background `git stash` or `git checkout --`,
a pre-commit hook, a Loom sweep quarantining the primary clone's working tree —
can revert a file between the moment you fix it and the moment you report it,
leaving this command claiming a fix that is no longer on disk.

So immediately after applying each fix, and **before counting it as applied**,
re-read the changed region of the file and confirm your specific edit is
present. `git diff -- <path>` / `git status --porcelain -- <path>` is a cheap
first pass, but only proves the path differs from HEAD — it cannot distinguish
your edit from someone else's, so it must not be the sole check when the file
may carry other uncommitted changes.

This check is **unconditional** — run it whether or not you have any reason to
suspect a concurrent writer. Detecting a daemon first would be racy (one can
start right after the check), and in a repo with no concurrent writer the check
always finds the edit still applied, so nothing about the reported output
changes.

If a fix is gone on re-check, report it on its own line as **reverted after
apply — needs re-run**. Do not silently re-apply it, and do not count it in the
fixed total — that total must only ever include edits confirmed still on disk.
