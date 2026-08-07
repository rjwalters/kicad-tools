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

Scope is *installer-managed tool packages* only. Third-party dependency
currency — npm/cargo/pip packages and GitHub Actions, i.e. Dependabot setup and
Dependabot PR triage — is [[deps]], not this command: there is no local source
clone to diff against, so it needs a different comparison model.

## Usage

```
/repo:update-tools               # Report, then offer updates (commit + land on the default branch)
/repo:update-tools --check       # Report only, never writes
/repo:update-tools loom          # Only check/update one tool
/repo:update-tools --no-commit   # Update the working tree but leave it uncommitted for review
```

An update runs the tool's own installer or updater (executing code from its
source repo and rewriting `.claude/`), so unlike the safe-fix hygiene commands
this one is **not** auto-applied — it reports and confirms before updating.
`--check` is the report-only form.

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

**Signature check: distinguish "never installed here" from "sidecar was deleted
by a pull."** Step 3 collapses two different situations into one "unknown"
outcome, so before reporting it, check for this signature: `install-metadata.json`
exists (proof a successful install previously ran in *this* checkout) but no
sidecar is present **and** no legacy inline `source` / `installed_at` fields
exist in `install-metadata.json` either. That combination is also what you get
when a previously-tracked `.install-local.json` was untracked upstream and this
checkout later pulled that commit — git deletes the untracked file's
working-tree copy in every checkout except the one that ran `git rm --cached`
(repo#96). Still report "source unknown" for the version-comparison purpose
(there is no path to read), but append a distinct one-line suggestion instead of
treating it identically to a fresh clone:

```
sidecar missing but install-metadata.json present — if this was previously
installed, re-run <tool>'s installer to regenerate the sidecar.
```

A genuinely fresh clone (no `install-metadata.json` at all) gets no such
suggestion — it was simply never installed here.

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
| some-tool   | 1.2.0 (Jun 30)   | ?       | sidecar missing — re-run installer? |
```

The last two rows are **different** failure modes, so report them distinctly:
`source repo missing` means the recorded source clone path no longer exists on
disk, while `sidecar missing` is the signature check above (installed here once,
but the machine-local pointer is gone — typically deleted by pulling an
untracking commit, repo#96).

Where a changelog exists in the source repo, summarize what changed between
the installed and latest versions.

### 4. Update (with confirmation)

For each stale tool the user approves, update the source clone first, then run
that tool's own update mechanism — its dedicated updater where it ships one,
otherwise its installer. Never hand-copy files:

```bash
git -C <source> pull --ff-only
# Loom:        <this-repo>/.loom/scripts/resync-installed.sh --dry-run   # preview drift
#              <this-repo>/.loom/scripts/resync-installed.sh             # apply once confirmed
# Anvil:       <source>/scripts/install-anvil.sh <this-repo>
# Repo Skills: <source>/install.sh -y <this-repo>
# kicad-tools: <source>/scripts/install-kct.sh <this-repo>
# Unknown tools: look for install.sh / scripts/install-*.sh in the source repo
```

**Loom updates go through `resync-installed.sh`, not `install.sh`.** Note the
path: the resync script lives in the **target** repo's `.loom/scripts/`, not in
the source clone, unlike every other row above. That `<this-repo>/` prefix
documents **which copy of the script to run**, not a target argument the script
consumes — the asymmetry with the sibling rows is deliberate. In every other row
the trailing `<this-repo>` is a positional argument that **selects** the repo the
installer acts on; `resync-installed.sh` takes **no positional target** and
rejects one with exit `1` (its arg loop matches only `--dry-run`/`-n`,
`--quiet`/`-q`, `--allow-worktree`, `--help`/`-h`). It resolves its target from
the **current working directory** via `git rev-parse --git-common-dir`
(worktree-safe — this points at the primary checkout even from a linked
worktree), never from its own path on disk. So do **not** "fix" the Loom row to look like its siblings by appending a
target path: the script would reject the command with an error that does not
obviously point back to the cause. What guarantees cwd is the target repo at this
point is that `/repo:update-tools` runs in the target repo's working directory
and nothing earlier in step 4 changes it — the source clone is only ever reached
through `git -C <source> …`, never a `cd`. Any future refactor that moves this
line must preserve that invariant, or it will silently resync whichever repo cwd
happens to be. It is the non-destructive,
idempotent update path — it reports per-file updated/created/unchanged/skipped,
never clobbers a symlinked install target, and re-stamps `loom_version` /
`loom_commit` / `last_resync` into `.loom/install-metadata.json` on a successful
non-dry-run. Run `--dry-run` first (exit `2` means drift was found and would be
synced, `0` means already in sync, `1` is an error), report it, then apply.
`<source>/install.sh --quick -y <this-repo>` is **not** an update command: Loom's
installer refuses a non-interactive reinstall over an existing `.loom/` and exits
with an error, which is the only situation this step ever runs in.

Reinstall is the **destructive fallback**, used only when resync cannot resolve
the drift:

```bash
# Destructive — uninstalls the existing Loom payload before writing the new version.
# Inventory and back up project-owned Loom hooks, scripts, and agent configuration first.
<source>/install.sh --quick -y --confirm-reinstall <this-repo>
```

Confirm that separately with the user; do not escalate to it just because a
resync pass exited non-zero — see the re-run caveat first.

**Anvil and kicad-tools rows verified correct as written (issue #135) — do not
re-investigate.** Unlike Loom, neither installer refuses a non-interactive
reinstall over an existing install, so `<source>/scripts/install-anvil.sh
<this-repo>` and `<source>/scripts/install-kct.sh <this-repo>` both succeed on
a second run and need no resync-equivalent or destructive-fallback split:

- **Anvil** (`rjwalters/anvil` `scripts/install-anvil.sh`, checked at `8302890`):
  Stage 3's "active-install guard" only sets `UPGRADE=true` when `.anvil/`
  already exists and proceeds — no exit, no confirmation gate bypassed by
  `-y`. The installer's own `--help` text tells consumers to "re-run
  `install-anvil.sh .` from the anvil checkout" to upgrade.
- **kicad-tools** (`rjwalters/kicad-tools` `scripts/install-kct.sh`, checked at
  `87561cf`): the header comment states outright "Re-running the installer is
  the upgrade/idempotency path: a second run with the same args adds no
  duplicate CLAUDE.md block and no duplicate dependency" — Stage 5 explicitly
  no-ops when the dependency is already present and up to date.

**Re-run caveat: `resync-installed.sh` syncs itself.** The script is part of the
`.loom/scripts/` payload it updates, so the copy that starts the run is the
*old* one. A stale copy carrying a bug can die partway through (observed going
0.16.0 → 0.18.0: `line 509: verb_past: unbound variable`) after it has already
written the newer script to disk. Re-running it once is expected to pick up the
freshly-synced copy and complete cleanly (in that case, 70 further files
updated). Treat a single failed pass as "retry once", not as a broken update or
a reason to reach for `--confirm-reinstall`.

If the source clone has local modifications or `--ff-only` fails, report it
and skip that tool rather than resolving on your own.

After updating, re-read each metadata file to confirm the new version and show
a summary of what changed (`git status --short`).

### 5. Land the update (default)

A confirmed tool bump is a safe, reversible, version-controlled change (the
installer/updater is idempotent and re-runnable), so by default `update-tools`
**commits it and lands it on the default branch** rather than stopping at an
uncommitted diff. It **never** pushes — pushing is outward-facing and stays a separate,
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
2. **Always use the tool's own installer or update mechanism** — where a tool
   ships a dedicated non-destructive updater (e.g. Loom's
   `.loom/scripts/resync-installed.sh`), prefer it over re-running the
   installer; installer reinstall is the destructive fallback, not the default
   update path. Either way, never hand-copy files — the installer/updater owns
   the write footprint and marker blocks, and hand-copying breaks reinstall
   idempotency
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
