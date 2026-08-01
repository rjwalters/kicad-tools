---
name: "update-tools"
description: "Check installed tool packages (Loom, Anvil, Repo Skills, …) against their source repos and offer to update"
domain: repo
type: command
user-invocable: true
---

# /repo:update-tools — Tool Package Updates

Find every tool package installed into this repo by an Anvil/Loom-style
installer, compare each against the latest version of its source, and offer
to update the stale ones.

## Usage

```
/repo:update-tools               # Report, then offer updates (commit + land on the default branch)
/repo:update-tools --check       # Report only, never writes
/repo:update-tools loom          # Only check/update one tool
/repo:update-tools --no-commit   # Update the working tree but leave it uncommitted for review
```

An update runs the tool's own installer (executing code from its source repo
and rewriting `.claude/`), so unlike the safe-fix hygiene commands this one is
**not** auto-applied — it reports and confirms before updating. `--check` is
the report-only form.

Once an update is confirmed, it is committed and landed on the default branch
(`main`) by default — it does **not** push, and it never folds a pre-existing
dirty working tree into the update commit. Pass `--no-commit` (alias
`--stage-only`) to restore the old behavior of leaving the changes uncommitted
for manual review. See step 5 and the Safety Rules for details.

## Steps

### 1. Discover installed tools

Tools in this family record their install in a metadata file. Look for:

```bash
ls .loom/install-metadata.json .anvil/install-metadata.json 2>/dev/null
ls .claude/skills/*/install-metadata.json 2>/dev/null
```

Key names vary by tool (`version` vs `loom_version` / `anvil_version`, `source`
vs `loom_source` / `anvil_source`) — read whichever variant is present. Each
file gives: installed version, installed commit, install date, and (for the
"prefer local source clone" fast path) the path of the local source clone it
was installed from.

**Locating the local source path (`source`).** The absolute source path and
install timestamp are machine-local — they mean nothing in another clone — so
newer installers keep them out of the tracked metadata file and write them to a
gitignored sidecar instead. Resolve `source` in this order, and treat every
step failing as "source clone unknown" rather than an error:

1. **Sidecar first.** For Repo Skills, read
   `.claude/skills/repo/.install-local.json` (generally
   `.claude/skills/*/.install-local.json`); it holds `source` and
   `installed_at`. Loom uses the plain-text `.loom/loom-source-path` sidecar for
   the same purpose. A sidecar is gitignored, so it is present only on the
   machine that ran the install — a fresh clone elsewhere legitimately has none.
2. **Legacy inline fallback.** Older (pre-split) installs still embed `source` /
   `installed_at` directly in `install-metadata.json` — read them from there if
   no sidecar exists, so existing installs keep their fast path.
3. **Unknown → GitHub.** If neither yields a usable path, the local source clone
   is simply unknown; skip to the GitHub check in step 2. This is normal (fresh
   clone on a different machine), not a failure.

Known family members: Loom (`.loom/`), Anvil (`.anvil/`), Repo Skills
(`.claude/skills/repo/`), kicad-tools, and anything else that follows the same
metadata pattern. Report any metadata file found even if the tool is
unrecognized.

### 2. Determine the latest version of each

For each tool, prefer the local source clone recorded in the metadata:

```bash
git -C <source> fetch origin --quiet
git -C <source> log --oneline HEAD..origin/HEAD | wc -l    # source clone itself behind?
# Version at origin: VERSION file, package.json, or pyproject.toml on origin/HEAD
git -C <source> show origin/HEAD:VERSION 2>/dev/null
```

If the source clone no longer exists, fall back to GitHub:
`gh api repos/<owner>/<repo>/tags --jq '.[0].name'` or the latest release.
If neither works, mark the tool UNKNOWN rather than guessing.

### 3. Report

```
TOOL PACKAGES
=============
| Tool        | Installed        | Latest  | Status      |
|-------------|------------------|---------|-------------|
| loom        | 0.9.1 (Jun 4)    | 0.10.6  | STALE       |
| anvil       | 0.9.0 (Jul 1)    | 0.9.0   | current     |
| repo-skills | 0.1.0 (Jul 14)   | 0.1.0   | current     |
| kicad-tools | 2.3.0 (May 20)   | ?       | source repo missing — clone it? |
```

Where a changelog exists in the source repo, summarize what changed between
the installed and latest versions.

### 4. Update (with confirmation)

For each stale tool the user approves, update the source clone first, then
re-run that tool's own installer — never hand-copy files:

```bash
git -C <source> pull --ff-only
# Loom:        <source>/install.sh --quick -y <this-repo>
# Anvil:       <source>/scripts/install-anvil.sh <this-repo>
# Repo Skills: <source>/install.sh -y <this-repo>
# kicad-tools: <source>/scripts/install-kct.sh <this-repo>
# Unknown tools: look for install.sh / scripts/install-*.sh in the source repo
```

If the source clone has local modifications or `--ff-only` fails, report it
and skip that tool rather than resolving on your own.

After updating, re-read each metadata file to confirm the new version and show
a summary of what changed (`git status --short`).

### 5. Land the update (default)

A confirmed tool bump is a safe, reversible, version-controlled change (the
installer is idempotent and re-runnable), so by default `update-tools` **commits
it and lands it on the default branch** rather than stopping at an uncommitted
diff. It **never** pushes — pushing is outward-facing and stays a separate,
explicit action (Safety Rule 5). Pass `--no-commit` (alias `--stage-only`) to
skip this step and leave the working-tree changes uncommitted for manual review
instead (the old behavior).

Land each tool's bump as its own commit:

1. **Isolate the installer's footprint.** Snapshot the working tree *before*
   running the installer so a pre-existing dirty tree is never folded into the
   update commit:

   ```bash
   pre=$(mktemp); post=$(mktemp)
   git -C <this-repo> status --porcelain | sed 's/^...//' | sort > "$pre"
   # ... run the tool's installer (step 4, above) ...
   git -C <this-repo> status --porcelain | sed 's/^...//' | sort > "$post"
   # Paths the installer actually changed = post minus pre:
   comm -13 "$pre" "$post" > changed.txt
   ```

   Stage **only** those paths (`git -C <this-repo> add -- $(cat changed.txt)`),
   never `git add -A`. If `changed.txt` is empty the installer was a no-op —
   report "already current" and skip the commit for that tool.

2. **Commit + land on the default branch, without committing straight to it:**

   ```bash
   DEFAULT=$(git -C <this-repo> symbolic-ref --quiet --short refs/remotes/origin/HEAD | sed 's#^origin/##')
   DEFAULT=${DEFAULT:-main}
   CUR=$(git -C <this-repo> symbolic-ref --short HEAD)
   MSG="chore(tooling): update <tool> <old>→<new>"

   if [ "$CUR" = "$DEFAULT" ]; then
     # On the default branch: commit on a short-lived branch, then fast-forward
     # merge it in — lands on the default branch without a straight-to-main commit.
     tmp="tooling/update-<tool>-<new>"
     git -C <this-repo> checkout -b "$tmp"
     git -C <this-repo> commit -m "$MSG"
     git -C <this-repo> checkout "$DEFAULT"
     git -C <this-repo> merge --ff-only "$tmp"
     git -C <this-repo> branch -d "$tmp"
   else
     # Already on a feature branch: commit here and report where it landed —
     # do NOT switch branches mid-session and disturb the user's working state.
     git -C <this-repo> commit -m "$MSG"
     echo "Landed the update on '$CUR' (not '$DEFAULT') — you are on a feature branch."
   fi
   ```

3. **Report** the resulting commit (`git -C <this-repo> log --oneline -1`) and
   remind the user it has **not** been pushed (run `git push` explicitly to
   share it).

## Safety Rules

1. **Never update without confirmation** — show installed → latest per tool first
2. **Always use the tool's own installer** — it owns its write footprint and
   marker blocks; hand-copying breaks reinstall idempotency
3. **Never resolve source-repo git problems silently** (diverged clone, dirty
   tree) — report and skip
4. **Land the update, don't just stage it** — by default commit the installer's
   changes and land them on the default branch with a per-tool
   `chore(tooling): update <tool> <old>→<new>` message. Stage **only** the paths
   the installer actually changed — never fold a pre-existing dirty working tree
   into the update commit. `--no-commit` / `--stage-only` restores the old
   leave-it-uncommitted-for-review behavior.
5. **Never push** — landing on the local default branch is reversible; pushing is
   outward-facing and stays a separate, explicit action the user runs themselves.
