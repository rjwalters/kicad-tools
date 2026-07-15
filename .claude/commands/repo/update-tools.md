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
/repo:update-tools             # Report, then offer updates
/repo:update-tools --check     # Report only
/repo:update-tools loom        # Only check/update one tool
```

An update runs the tool's own installer (executing code from its source repo
and rewriting `.claude/`), so unlike the safe-fix hygiene commands this one is
**not** auto-applied — it reports and confirms before updating. `--check` is
the report-only form.

## Steps

### 1. Discover installed tools

Tools in this family record their install in a metadata file. Look for:

```bash
ls .loom/install-metadata.json .anvil/install-metadata.json 2>/dev/null
ls .claude/skills/*/install-metadata.json 2>/dev/null
```

Key names vary by tool (`version` vs `loom_version` / `anvil_version`, `source`
vs `loom_source` / `anvil_source`) — read whichever variant is present. Each
file gives: installed version, installed commit, install date, and the path of
the local source clone it was installed from.

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

After updating, re-read each metadata file to confirm the new version, and
show a summary of what changed in the repo (`git status --short`) so the user
can review and commit the update.

## Safety Rules

1. **Never update without confirmation** — show installed → latest per tool first
2. **Always use the tool's own installer** — it owns its write footprint and
   marker blocks; hand-copying breaks reinstall idempotency
3. **Never resolve source-repo git problems silently** (diverged clone, dirty
   tree) — report and skip
4. **The update touches the working tree** — leave the changes uncommitted for
   the user to review
