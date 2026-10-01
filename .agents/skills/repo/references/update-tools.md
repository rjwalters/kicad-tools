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
/repo:update-tools --branch      # Force step 5's branch + PR landing path unconditionally
```

An update runs the tool's own installer or updater (executing code from its
source repo and rewriting `.claude/`), so unlike the safe-fix hygiene commands
this one is **not** auto-applied — it reports and confirms before updating.
`--check` is the report-only form.

Once an update is confirmed, it is committed and landed on the default branch
(`main`) by default — it does **not** push, and it never folds a pre-existing
dirty working tree into the update commit. Pass `--no-commit` (alias
`--stage-only`) to restore the old behavior of leaving the changes uncommitted
for manual review. `<this-repo>`'s own default branch is sometimes PR-protected
(a ruleset or legacy branch protection requiring a pull request), in which case
landing on local `main` is a dead end — the commit cannot be pushed straight to
`main` — so step 5 detects that case and lands on a `tooling/…` branch instead,
offering to push it and open a PR. `--branch` forces that same path when
detection itself cannot run (a non-GitHub remote, or Gitea). See step 5 and the
Safety Rules for details.

## Steps

### 1. Discover installed tools

Tools in this family record their install in a metadata file. Sweep for it
rather than listing known paths — a fixed list structurally cannot find a
family member added after this doc was last updated, which is exactly how
`.kct/install-metadata.json` went undiscovered before (repo#165):

```bash
find . -maxdepth 4 -name "install-metadata.json" \
  -not -path "*/node_modules/*" -not -path "*/.venv/*" 2>/dev/null
```

`-maxdepth 4` reaches every known tool root (`.loom/`, `.anvil/`, `.kct/` at
depth 2, and the two-levels-deeper `.claude/skills/*/` at depth 4) and any
future tool that follows the same shallow layout, without needing a doc edit
when one is added.

Key names vary by tool (`version` vs `loom_version` / `anvil_version` /
`kct_version`, `source` vs `loom_source` / `anvil_source`) — read whichever
variant is present. Each file gives: installed version, installed commit,
install date, and (for the "prefer local source clone" fast path) the path of
the local source clone it was installed from — **except kicad-tools**, whose
shape differs; see below.

**The metadata layout and the source-resolution order are normative in the
[tool-package installer contract][contract]** — requirements **C5** (tracked
metadata: `version`, `commit`, `layout_version`, never a path or timestamp) and
**C6** (the gitignored `.install-local.json` sidecar, and the sidecar → legacy
inline → unknown resolution order, including the repo#96 "sidecar deleted by a
pull" signature and the exact suggestion to append). Read C6 before implementing
this step; do not re-derive the rules from what a given tool happens to do.

Two things are *this command's* behavior rather than the contract's, so they
stay here: an unresolved source means **skip to the GitHub check in step 2**
(not an error), and the repo#96 signature gets its own distinct report row —
see step 3.

[contract]: https://github.com/rjwalters/repo/blob/main/INSTALLER-CONTRACT.md

**kicad-tools does not conform to C5/C6 — resolve its shape directly instead
of via the sidecar ladder.** `.kct/install-metadata.json` carries `kct_version`
/ `kct_commit` (not `version` / `commit`), and it never writes a sidecar: it
always records `source_mode` (`"path"` or `"git"`) and `source_ref` inline in
the tracked file.

- `source_mode: "path"` — `source_ref` **is** the local source clone path; use
  it directly as `<source>` in step 2, no sidecar lookup needed.
- `source_mode: "git"` (kicad-tools' default) — there is no local clone at all;
  `source_ref` is a `<git-url>@<tag-or-rev>` string, not a path. This is the
  normal/default install shape, not a degraded case — treat it exactly like
  "source unknown" above and skip straight to the GitHub check in step 2. Never
  report it as a missing/broken source repo.

**Loom's sidecar is named differently, but still conforms to C6's ladder.**
Where C6 step 1 names the sidecar `<tool-root>/.install-local.json`, Loom's
actual sidecar is the plain-text `.loom/loom-source-path` (a single path, not
JSON) — check that file, not `.install-local.json`, when resolving Loom's
source in step 2, and treat its presence as a resolved sidecar for step 3's
`sidecar missing` status below.

Known family members: Loom (`.loom/`), Anvil (`.anvil/`), Repo Skills
(`.claude/skills/repo/`), kicad-tools, and anything else that follows the same
metadata pattern. Report any metadata file found even if the tool is
unrecognized.

**A tool's top-level `version` can be a partial claim — read the per-component
fields too where the tool records them.** `install-anvil.sh` deliberately
declines to overwrite consumer-modified skill bodies (its documented `--force`
skip) while still stamping the installer-run version into `anvil_version`. The
result is a tree at a mix of versions whose top-level field names only one of
them — reported as a clean `current` by a version-only comparison, over a tree
hundreds of files behind (anvil#1320: a 0.10.1 → 0.11.6 upgrade where 467 files
differed and the manifest read `0.11.6`).

For `.anvil/install-metadata.json` specifically, three more fields decide this:

- `skipped_overrides` — skills the last run declined to overwrite. Non-empty
  means the tree is **not** uniformly at `anvil_version`.
- `skill_versions` — per skill, the version of the run that last *actually*
  installed that skill's body (anvil#633). This is how far behind each frozen
  skill is. A pre-#633 manifest has no such block: record those skills as
  frozen at an **unknown** version, never as `anvil_version`. Borrowing the
  top-level number there would re-introduce the exact false `current` this
  check exists to remove.
- `skill_hashes` — the as-installed content hash per skill; the installer's own
  input for deciding modified-vs-unmodified. Not needed for the report, but it
  is what makes the skip decision above reproducible.

**Read those three fields directly out of the tracked manifest — that is the
primary path, and it always works.** The data is in the manifest, so the check
needs neither a source clone nor a remote fetch.

**Optional fast path, only when the script is actually present.** Anvil ships
an anvil-owned reader that performs the same parse (no jq, writes nothing), but
it postdates most installs — it landed in anvil#1324, so a clone or install
older than that will not have it. Gate on its existence; never assume it:

```bash
if [[ -x "<source>/scripts/check-install-staleness.sh" ]]; then
  <source>/scripts/check-install-staleness.sh <this-repo>          # human report
  <source>/scripts/check-install-staleness.sh <this-repo> --json   # machine-readable
  # exit 0 = uniformly current, 1 = N skill(s) frozen behind, 2 = could not evaluate
fi
```

Its absence — including when `<source>` itself never resolved (the
`source unknown` path above) — is **not** a "could not evaluate": fall back to
the direct manifest read, which is unconditional. Carry the frozen count and
the per-skill frozen versions into step 2 as a third drift dimension alongside
version drift and commit drift.

**Detect dev installs before comparing versions.** `install.sh --dev .`
**symlinks** a tool's files directly into `.claude/` instead of copying them —
the installed surface *is* the source clone, so stamped-version comparison is
meaningless by construction: it cannot be stale, and it cannot become stale by
the source moving ahead, because the "installed" files and the source files
are the same inode. Detect this per tool, checking both signals (either one is
sufficient — don't require both):

- **Flag**: read `dev` (or the tool's equivalent field) from the metadata
  file, e.g. Repo Skills' `install-metadata.json` carries `"dev": true`.
- **Structural fallback**, for metadata predating the flag: test whether the
  tool's primary installed file is a symlink, `[[ -L <path> ]]` — e.g. one of
  the paths in the metadata's own `commands`/`installed_files` list. This is
  the same check `resync-installed.sh` already uses to recognize a dev
  destination and skip overwriting it (`scripts/repo/resync-installed.sh:317`,
  `... symlinked (dev-mode install) ...`).

A tool flagged dev by either check is **dev-mode** for the rest of this
command: it skips the STALE/current comparison in step 2, gets its own report
status in step 3, and is never offered an update in step 4.

### 2. Determine the latest version of each

**Dev-mode tools (step 1) skip this comparison entirely** — do not run the
stamped-version-vs-latest check below for them. Instead, check only whether
the **source clone itself** is behind its own remote — that's the one
meaningful staleness a dev install can have, since the install *is* the
source:

```bash
git -C <source> fetch origin --quiet
git -C <source> log --oneline HEAD..origin/HEAD | wc -l    # source clone itself behind?
```

A non-zero count is reported in step 3 as the *source clone* being behind
(fixed with a plain `git -C <source> pull` on that clone) — never as the
*install* being STALE, and never as grounds to offer step 4's update flow.

For every non-dev tool, prefer the local source clone recorded in the
metadata:

```bash
git -C <source> fetch origin --quiet
git -C <source> log --oneline HEAD..origin/HEAD | wc -l    # source clone itself behind?
# Version at origin: VERSION file, package.json, or pyproject.toml on origin/HEAD
git -C <source> show origin/HEAD:VERSION 2>/dev/null
# How far the INSTALLED commit is behind the source's origin/HEAD
git -C <source> log --oneline <installed-commit>..origin/HEAD | wc -l
```

**Record three numbers per non-dev tool, not one — version drift, commit
drift, and (for a tool that records per-component versions) its
frozen-component count (step 1).** Version equality alone systematically
under-reports staleness: upstream routinely merges a day of work without
bumping VERSION, so a tool
whose stamped version equals the VERSION at `origin/HEAD` can still be dozens
of commits behind the code that will eventually ship under that same version
number. In a real run that reported two tools `current`, one was 24 and the
other 7 commits behind, and the misleading `current` stood for a whole working
day until upstream happened to cut a release (repo#291). So carry all three
into step 3:

1. **Version drift** — the metadata's installed `version` vs the VERSION at
   `origin/HEAD` (the comparison already shown above).
2. **Commit drift** — `git -C <source> log --oneline <installed-commit>..origin/HEAD | wc -l`,
   where `<installed-commit>` is the metadata's `commit` field. That field is
   tracked, per-tool, install-time metadata required by the [contract][contract]
   (**C5**), so it is already available from step 1 — no new metadata is needed.
   Use the tool's key variant where it differs (`kct_commit` for kicad-tools,
   `loom_commit` / `anvil_commit` for legacy inline shapes).
3. **Frozen-component drift** — the count of per-component entries the tool
   itself records as not-at-the-stamped-version. Today only Anvil reports this
   (`skipped_overrides` + `skill_versions`, step 1); every other family member
   (Loom, Repo Skills, kicad-tools) has no per-component surface, so its frozen
   count is structurally `0`, **not** `unknown` — this dimension never widens
   the "could not evaluate" surface for a tool that simply does not have it.
   This dimension needs **no source clone and no remote fetch** — it is a
   self-consistency check on the tracked manifest, so it is computable for
   every Anvil install including the `source unknown` / `source_mode: "git"`
   cases where both dimensions above come back unknown. It is also the one
   dimension the GitHub fallback below does not lose.

**Commit drift is only computable when both inputs exist.** It needs a local
source clone *and* a recorded installed commit that the clone can actually
resolve. Record the distance as **unknown** — and report the tool on the
version comparison alone — whenever any of these holds:

- the source clone is missing, or was never resolved (the `source_mode: "git"`
  and "source unknown" paths in step 1), so the GitHub fallback below is the
  only comparison available;
- the metadata predates the `commit` field, or the field is empty;
- the clone cannot resolve the recorded commit (upstream force-pushed or
  rebased it away).

Verify resolvability before measuring, rather than trusting `wc -l` on a failed
`git log`:

```bash
git -C <source> cat-file -e <installed-commit>^{commit} 2>/dev/null \
  || echo "installed commit not in source clone — commit drift unknown"
```

Never report an uncomputable distance as `0 commits behind`, and never drop the
caveat silently: a bare `current` that was never actually checked against the
source HEAD is the exact failure this comparison exists to remove.

If the source clone no longer exists, fall back to the GitHub API — **read the
version file on the default branch first, tags/releases only as a last
resort.** Tags routinely lag the version file by a wide margin (observed on
`rjwalters/loom`: latest tag `v0.18.0` against `VERSION` `0.18.121` at
`origin/HEAD` — 121 patch versions of drift) because installers read
`VERSION` / `package.json` / `pyproject.toml`, never tags, so a tags-first
comparison would confidently report a version dozens of releases stale as
"latest."

1. **Preferred: the version file at `origin/HEAD` via the Contents API** — try
   the same file list step 2's local-clone path already checks, in order,
   stopping at the first that resolves:

   ```bash
   gh api repos/<owner>/<repo>/contents/VERSION --jq .content 2>/dev/null | base64 -d
   # or, if VERSION doesn't exist in that repo:
   gh api repos/<owner>/<repo>/contents/package.json --jq .content 2>/dev/null | base64 -d
   gh api repos/<owner>/<repo>/contents/pyproject.toml --jq .content 2>/dev/null | base64 -d
   ```

2. **Last resort, only if none of those files exist: tags or the latest
   release** — `gh api repos/<owner>/<repo>/tags --jq '.[0].name'` or the
   latest release. Tags are **not authoritative** on this path: if the tag is
   *older* than the tool's installed version, that means the tag lags, not
   that the install is ahead. Report that case as `UNKNOWN` — never `STALE` —
   since a naive string/semver comparison against a lagging tag would falsely
   flag an up-to-date install as behind.
3. **If neither works**, mark the tool UNKNOWN rather than guessing.

**Commit drift stays unknown on this path.** There is no local source clone to
diff against `<installed-commit>`, so this fallback only ever produces the
version-drift number — never attempt to compute or report a commit-drift
count here (see the "Commit drift is only computable" rule above, which
already lists "the source clone is missing" as one of the unknown cases).

**Frozen-component drift does not stay unknown on this path** — it reads the
tracked manifest, not the clone, so keep computing and reporting it here. It is
the only one of the three dimensions that survives a missing source clone
intact.

### 3. Report

```
TOOL PACKAGES
=============
| Tool        | Installed        | Latest  | Status      |
|-------------|------------------|---------|-------------|
| loom        | 0.9.1 (Jun 4)    | 0.10.6  | STALE       |
| anvil       | 0.11.6 (Jul 1)   | 0.11.6  | current, but 14 skill(s) frozen behind (memo 0.10.1, deck 0.10.1, …) |
| other-tool  | 1.2.0 (commit abc1234) | 1.2.0 | current, but 24 commits behind source HEAD |
| repo-skills | 0.8.0 (Aug 9)    | —       | dev (symlinked to /Users/you/GitHub/repo) |
| kicad-tools | 2.3.0 (May 20)   | ?       | source repo missing — clone it? |
| some-tool   | 1.2.0 (Jun 30)   | ?       | sidecar missing — re-run installer? |
```

**A non-dev tool gets one of four statuses, not two.** Version equality by
itself is not "current" — resolve the status from *all three* numbers step 2
recorded, in this precedence order:

| Version vs `origin/HEAD` | Frozen components (step 2.3) | Installed commit vs `origin/HEAD` | Status |
|--------------------------|------------------------------|-----------------------------------|--------|
| behind | not consulted | not consulted — version drift already decides it | `STALE` |
| equal | N > 0 | M > 0 commits behind (still computed) | `current, but N skill(s) frozen behind (<name> <ver>, …); also M commits behind source HEAD` |
| equal | N > 0 | 0 or not computable | `current, but N skill(s) frozen behind (<name> <ver>, …)` |
| equal | 0 | 0 commits behind | `current` |
| equal | 0 | N > 0 commits behind | `current, but N commits behind source HEAD` |
| equal | 0 | not computable (step 2) | `current (commit drift unknown — <why>)` |

**Never report a bare `current` for a tool with a non-zero frozen count.** That
is the failure anvil#1320 reported: the released version genuinely matches, so
every version-only and commit-only check says `current`, while the tree's actual
*content* sits at an older release — the version marker answering confidently
and wrongly, over a tree 467 files behind. It ranks **above** commit drift in the
precedence table because it is a strictly larger, already-confirmed gap: commit
drift is unreleased work the install has not yet taken, frozen components are
*released* work the install explicitly declined. Name the frozen components and
their versions inline (truncate with `…` past three) so the operator can see the
scale without re-reading the manifest; a component whose version is unrecorded
(pre-anvil#633 manifest, step 1) is named with `unknown` rather than the
top-level number.

A frozen count of zero is a real `0`, not an `unknown` — the manifest's
`skipped_overrides` is authoritative and requires no remote lookup — so this row
never degrades into the "could not evaluate" wording the commit-drift row needs.
For a tool family member with no per-component surface at all (Loom, Repo
Skills, kicad-tools today), the count is `0` by construction and this row simply
never fires; do not invent a per-component check for a tool that records none.

The `N commits behind source HEAD` row is what the *earlier* (repo#291)
distinction exists for. It is **not** a claim that the install is broken — the
released version really does match — it says the
source has merged work that has not been cut into a release yet, which the
operator can take right now (step 4) because the installers are idempotent.
Reporting it as a plain `current` is what hid 24 unreleased commits for a full
working day (repo#291), and it is why "is this tool even included in the check?"
was a reasonable question to ask of a report that *had* included it.

When the commit distance is non-zero, put the installed short SHA in the
`Installed` cell (`1.2.0 (commit abc1234)`) instead of the install date, so the
operator can run the `git -C <source> log` range themselves without re-reading
the metadata file. `STALE` rows do not need a commit count appended: the version
bump is already the actionable signal there. The `<why>` in the unknown row is
the specific reason from step 2 (`no source clone`, `no recorded commit`,
`commit not in source clone`) — never an unqualified `current`.

The last two rows are **different** failure modes, so report them distinctly:
`source repo missing` means the recorded source clone path no longer exists on
disk, while `sidecar missing` is the signature check above (installed here once,
but the machine-local pointer is gone — typically deleted by pulling an
untracking commit, repo#96). For Loom, that pointer is `.loom/loom-source-path`
(step 1's C6 exception), not `.install-local.json` — report `sidecar missing`
for Loom only when `.loom/loom-source-path` is also absent, never on the
absence of `.install-local.json` alone (Loom never writes that file).

**A dev-mode tool (step 1) always gets its own `dev (symlinked to <source>)`
status row — never `current`, never `STALE`, and never the commit-drift or
frozen-component statuses above.** The four-status table is the *non-dev* path
only: a dev install has no stamped copy to be behind its source, so its source
clone's own distance from
its remote is reported inside the `dev (…)` row (below), not as
`current, but N commits behind source HEAD`. `current` would only be a
coincidence for a symlinked install (the files are the source, not a copy that
happens to match it), and reporting it that way hides from the operator that
this install is not an ordinary copy. Leave `Latest` as `—`: there is no
"latest for this install" to compare against, only the source clone's own
position relative to its remote. If step 2 found the source clone itself
behind its remote, fold that into the same row instead of a separate STALE
row, e.g.:

```
| repo-skills | 0.8.0 (Aug 9)    | —       | dev (symlinked; source clone 3 commits behind origin) |
```

That is actionable (`git -C <source> pull`) without implying the *install*
needs — or can receive — an update.

Where a changelog exists in the source repo, summarize what changed between
the installed and latest versions for non-dev tools.

### 4. Update (with confirmation)

**Never offer a dev-mode tool (step 1/3) an update here — not even when its
source clone reports behind in step 3.** There is nothing to update: the
installed files already are the source clone. Running an installer/updater
over a dev install anyway would replace its symlinks with rendered copies,
silently ending the live-editing setup `--dev` exists to provide, with no
signal to the operator that it happened — exactly the harm this whole check
exists to prevent. If the report showed the source clone behind its remote,
the fix is a plain `git -C <source> pull` on that clone, run by the operator
directly — outside this update flow, not through it.

**Commit drift is offered on exactly the same terms as version drift.** A tool
reported `current, but N commits behind source HEAD` (step 3) goes into the same
confirmation prompt as a `STALE` one, with its distance shown, and is updated
only if the user approves it. The update mechanism does not care which signal
triggered it: the resync/installer paths below are idempotent and re-runnable
(Safety Rule 2) and they install whatever is at the source clone's
`origin/HEAD` — which *is* the unreleased work the commit-drift row is
reporting. Two things do not change: it is never auto-applied just because an
unchanged version number makes it look like a no-op (Safety Rule 1 applies
unchanged — it is a real code change), and a tool whose commit drift came back
**unknown** is never offered an update on that basis, because nothing was
actually compared.

**Frozen components are the one status an ordinary re-run does NOT fix, so
report the trade-off instead of quietly escalating.** A skill lands in
`skipped_overrides` precisely because the consumer edited it; the installer's
default is to preserve that edit, and re-running the installer without
`--force` skips it again and reports the identical status next pass. Only
`<source>/scripts/install-anvil.sh --force <this-repo>` overwrites it — and `--force` is
**global, not per-skill**: it overwrites *every* consumer-modified skill body
plus the lib override assets, with no undo. So for a tool reported
`current, but N skill(s) frozen behind`:

- Offer the plain (non-`--force`) re-run only if the status line also
  reports commit drift (`; also M commits behind source HEAD`) — that still upgrades the unmodified components, and leaves the
  frozen ones exactly where they were.
- Never add `--force` on your own initiative, and never treat a frozen count as
  license to escalate. Surface the choice explicitly, with the per-skill frozen
  versions from step 3 and the preview command, and let the operator decide
  which local edits they are willing to lose:

  ```bash
  # Preview exactly what --force would overwrite, per frozen skill:
  diff -ru <source>/anvil/skills/<name> <this-repo>/.anvil/skills/<name>
  ```

- A frozen component the operator intends to keep edited is a legitimate steady
  state, not a defect. Say so rather than re-reporting it as an action item
  every pass: the value of the row is that the gap is *visible*, not that it
  must be closed. It belongs in the report unconditionally, but in the
  confirmation prompt only as information unless the operator asks for
  `--force`.

For each non-dev tool the user approves — whether it was reported `STALE`
(version drift) or `current, but N commits behind source HEAD` (commit drift) —
update the source clone first, then run that tool's own update mechanism — its
dedicated updater where it ships one, otherwise its installer. Never hand-copy
files:

```bash
git -C <source> pull --ff-only
# Loom:        <this-repo>/.loom/scripts/resync-installed.sh --dry-run   # preview drift
#              <this-repo>/.loom/scripts/resync-installed.sh             # apply once confirmed
# Repo Skills: <this-repo>/.claude/skills/repo/scripts/resync-installed.sh --dry-run
#              <this-repo>/.claude/skills/repo/scripts/resync-installed.sh
# Anvil:       <source>/scripts/install-anvil.sh <this-repo>
# kicad-tools: <source>/scripts/install-kct.sh <this-repo>
# Unknown tools: look for <tool-root>/scripts/resync-installed.sh first (contract
#                C7); failing that, install.sh / scripts/install-*.sh in the source
```

**Prefer a tool's C7 resync over its installer.** [Contract][contract] C7 gives
every conforming tool the same consumer-side entry point —
`<tool-root>/scripts/resync-installed.sh`, with `--dry-run` / `--quiet` and the
same exit codes (`0` in sync, `2` drift found, `1` error) — so the same two
commands drive any tool that ships one. Run `--dry-run` first, report it, then
apply. Loom and Repo Skills ship one today; Anvil and kicad-tools do not, so
their rows re-run the installer (see the verified note below).

**The resync rows resolve their target from cwd, not from an argument.** Note
the path: a resync script lives in the **target** repo's tool root, not in the
source clone, unlike every other row above. That `<this-repo>/` prefix documents
**which copy of the script to run**, not a target argument the script consumes —
the asymmetry with the sibling rows is deliberate. In every other row the
trailing `<this-repo>` is a positional argument that **selects** the repo the
installer acts on. So do **not** "fix" a resync row to look like its siblings by
appending a bare target path: Loom's script rejects a positional with exit `1`
(its arg loop matches only `--dry-run`/`-n`, `--quiet`/`-q`, `--allow-worktree`,
`--help`/`-h`), and so does Repo Skills' (which takes an explicit `--target
<path>` instead) — in both cases with an error that does not obviously point back
to the cause.

What guarantees cwd is the target repo at this point is that `/repo:update-tools`
runs in the target repo's working directory and nothing earlier in step 4 changes
it — the source clone is only ever reached through `git -C <source> …`, never a
`cd`. Any future refactor that moves these lines must preserve that invariant, or
they will silently resync whichever repo cwd happens to be.

- **Loom** resolves its target via `git rev-parse --git-common-dir`, which points
  at the **primary** checkout even from a linked worktree — so running it from a
  worktree writes to the main checkout. It re-stamps `loom_version` /
  `loom_commit` / `last_resync` into `.loom/install-metadata.json` on a
  successful non-dry-run. `<source>/install.sh --quick -y <this-repo>` is **not**
  an update command for it: Loom's installer refuses a non-interactive reinstall
  over an existing `.loom/` and exits with an error, which is the only situation
  this step ever runs in.
- **Repo Skills** resolves its target via `git rev-parse --show-toplevel`, so
  writes land in the worktree you are standing in; there is no `--allow-worktree`
  because there is nothing to escape. It re-stamps `version` / `commit` into the
  tracked `install-metadata.json` and `last_resync` into the gitignored sidecar
  (the C5/C6 split), and resolves its source clone with the same sidecar → legacy
  inline order documented in step 1.

#### A `layout_version` bump needs the installer re-run, not resync

**Resync only refreshes file contents at their existing destinations — it
cannot move a file to a new destination or rewire a new hook.** C5's tracked
metadata carries `layout_version` alongside `version`/`commit` for exactly
this reason: content and placement/wiring drift independently, and only the
former is resync's job. `resync-installed.sh` says so itself when it hits
this case: "only refreshes file contents. Re-run install.sh to pick up moved
destinations or changed wiring." So before trusting a `0`/`2` resync exit
code as sufficient, compare the installed `layout_version` against the
source's: if the source has bumped it, resync alone is not enough — the
installer has to run so it can move files, add new surfaces, and rewire
hooks. This is a separate trigger from "resync cannot resolve the drift"
below; check `layout_version` first.

**That installer re-run is not automatically the destructive path — its
safety differs per tool, and "fallback" should not be read as "destructive"
across the board:**

- **Repo Skills**: safe/idempotent. Verified going 0.10.0 → 0.11.2 across a
  `layout_version` bump (1 → 2) — the re-run added the new
  `.agents/skills/repo/` surface and respected existing hook wiring, with no
  uninstall step and no confirmation flag needed.
- **Anvil** and **kicad-tools**: already verified safe/idempotent (issue
  #135, below), independent of `layout_version` — a second installer run
  succeeds cleanly with no duplication.
- **Loom**: the one tool here where the installer re-run is *not* the plain
  path — its installer refuses a non-interactive reinstall over an existing
  `.loom/` and exits with an error instead, so a Loom `layout_version` bump
  falls back to resync (which will warn it cannot fully resolve the drift)
  rather than to a bare installer re-run. `--confirm-reinstall` (below) is
  Loom's genuinely destructive path — it is a different action from "the
  installer re-run" that Repo Skills, Anvil, and kicad-tools all perform
  safely, and the two must not be conflated under one "fallback" label.

#### Between the dry-run and the apply: flag repo-local modifications

**A `would update` path that upstream never touched is a repo-local
modification about to be destroyed — diff the dry-run against upstream before
applying.** A resync rewrites managed files wholesale, so any repo-local patch
to one of them is silently reverted. That has already eaten the same
`.loom/roles/guide.md` patch three times in this repo, and the third time the
only thing that caught it was a session memory note — nothing in this flow
(repo#405). The signal was in the dry-run output every time, and reading it
costs one `git diff --name-only` per candidate file:

1. **Capture the dry-run's `would update` set.** Exit `2` means drift was
   found; the preview lines have the shape `  would update <rel>` (alongside
   `would create` and `would remove`). Only `would update` can destroy local
   content — a `would create` has nothing to overwrite yet.

   ```bash
   <this-repo>/.loom/scripts/resync-installed.sh --dry-run \
     | sed -n 's/^[[:space:]]*would update[[:space:]]*//p' \
     > /tmp/update-tools-would-update.txt
   ```

   Redirecting to a file already disables the script's ANSI colors, so the
   captured lines are plain paths; strip escapes yourself if you teed the run
   through a TTY.

2. **Map each `<rel>` back to a path in the source clone.** The two trees
   mirror each other, but the prefix is per tool and is documented in that
   tool's own resync script header — do not guess it:
   - **Loom** — `<rel>` is relative to `defaults/`: installed `roles/guide.md`
     <- `defaults/roles/guide.md`, installed `scripts/x.sh` <-
     `defaults/scripts/x.sh`.
   - **Repo Skills** — the header comment table in
     `scripts/repo/resync-installed.sh` gives the mapping, e.g. installed
     `.claude/commands/repo/<cmd>.md` <- `commands/repo/<cmd>.md`.

3. **Ask whether upstream actually changed that file** across the
   installed→HEAD range. `<installed-commit>` is the metadata `commit` field
   from step 1 (`loom_commit` for Loom's legacy inline shape), and the source
   clone was already brought to `origin/HEAD` by the `git -C <source> pull
   --ff-only` at the top of this step:

   ```bash
   git -C <source> diff --name-only <installed-commit>..origin/HEAD -- <source-path>
   ```

4. **Empty output means LOCAL MODIFICATION.** Upstream has not touched that
   file since the installed commit, yet the resync still wants to rewrite it —
   the only way both can be true is that the *installed* copy diverged
   locally. **Non-empty output is an ordinary upstream update**: leave it in
   the normal `would update` list and do not flag it. This is the distinction
   the whole check turns on; a genuine upstream change must never be reported
   as a local modification, or the flag becomes noise the operator learns to
   click through.

Report the flagged set as its own block **before** the confirmation prompt,
naming what would be lost:

```
LOCAL MODIFICATIONS — will be overwritten by resync
===================================================
  roles/guide.md   (upstream unchanged since <installed-commit>)
```

```bash
# Per flagged file, show exactly what the resync would destroy — the source
# copy that would be written vs the installed copy that would go away. The
# installed path is the resync's own destination for that <rel> (for Loom,
# <this-repo>/.loom/<rel>; for Repo Skills, the header table's destination):
git diff --no-index -- <source>/<source-path> <this-repo>/<installed-path>
```

**The confirmation prompt must list these files by name and offer three
choices for the affected tool**, not a bare yes/no:

- **apply and re-patch** — run the resync, then re-apply the local
  modification immediately (recipe below) and confirm in step 5 that it
  survived;
- **skip this tool** — leave it stale for this run and keep the local patch;
- **abort** — stop the whole update run.

Never fold a flagged tool into a blanket "update all?" confirmation: the
operator cannot consent to losing a patch they were never shown.

**Former recurring case in this repo — `.loom/roles/guide.md` (resolved).**
Through Loom 0.18.121 this file carried a repo-local
`preflight_refresh_docs_pr_exclude()` block (repo#280, restored by repo#391 and
again at the 0.18.121 resync) that upstream's `defaults/roles/guide.md` lacked,
and every resync stripped it. loom#6627 landed in Loom 0.18.130: upstream now
ships the equivalent `refresh_docs_pr_exclude_from_origin()` (same behavior,
different contract — it reads and rewrites `GUIDE_DOCS_PR_EXCLUDE` in place
instead of taking a positional argument), and
`commands/repo/tests/test-work-log-docs-pr-self-loop.sh` targets that name.
**Do not re-apply the old patch from history** — it would duplicate upstream's
logic. A flagged `roles/guide.md` from this point on means a *new* local
divergence and should be investigated, not re-patched. The recipe it replaces
is kept here only as the shape to use for any future genuine local patch:

```bash
git log --oneline -- .loom/roles/guide.md          # find <last-restoring-commit>
git show <last-restoring-commit> -- .loom/roles/guide.md | git apply
bash commands/repo/tests/test-work-log-docs-pr-self-loop.sh   # must pass again
```

**Two blind spots, so an empty flagged set is not proof nothing will be lost:**

- A file that upstream changed **and** carries a repo-local patch has a
  non-empty diff, so it is reported as an ordinary update. Step 5's
  net-negative-lines cross-check is the backstop for that case.
- `would remove` lines (a payload file retired upstream) delete the installed
  copy outright rather than rewriting it. If such a file holds repo-local
  content, save it before applying — the diff check above says nothing about
  it.

For **Loom specifically**, `--confirm-reinstall` is the **destructive
fallback**, used only when resync cannot resolve the drift (including a
`layout_version` bump — see above, since Loom's plain installer re-run is not
available as a middle option the way it is for the other three tools):

```bash
# Destructive — uninstalls the existing Loom payload before writing the new version.
# Inventory and back up project-owned Loom hooks, scripts, and agent configuration first.
<source>/install.sh --quick -y --confirm-reinstall <this-repo>
```

Confirm that separately with the user; do not escalate to it just because a
resync pass exited non-zero — see the re-run caveat first. This flag, and the
uninstall-then-reinstall it performs, is what "destructive" refers to
throughout this doc — it is **not** a description of the plain installer
re-run that Repo Skills, Anvil, and kicad-tools perform for a `layout_version`
bump (above), which is a normal, non-destructive, idempotent update for those
three.

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
uncommitted diff. It **never** pushes the default branch — pushing is
outward-facing and stays a separate, explicit action (Safety Rule 5). The one
narrow exception is the `tooling/…` branch item 3 below uses when the default
branch turns out to be PR-protected: pushing *that* branch (never the default
branch itself) to open a PR is the only way to land anything there at all, and
even then only on explicit operator confirmation. Pass `--no-commit` (alias
`--stage-only`) to skip this step and leave the working-tree changes
uncommitted for manual review instead (the old behavior).

Land each tool's bump as its own commit:

1. **Isolate the installer's footprint.** Snapshot the working tree *before*
   running the installer so a pre-existing dirty tree is never folded into the
   update commit. **Use a literal, spelled-out scratch path — never a
   `$(mktemp)`-assigned shell variable — as the `>` redirect target.** In a
   Loom-managed repo the destructive-write guard denies a write whose target
   is an unexpanded shell variable outright, because it cannot statically
   resolve where the write lands (fail-closed, #4921/#4178); a literal path
   sidesteps that ambiguity entirely, so do not "clean this back up" into
   `$pre`/`$post`/`$changed` variables for readability. Prefer the session's
   own scratchpad directory when the agent has one; otherwise spell out a
   `/tmp/...` path directly:

   ```bash
   git -C <this-repo> status --porcelain | sed 's/^...//' | sort > /tmp/update-tools-pre.txt
   # ... run the tool's installer (step 4, above) ...
   git -C <this-repo> status --porcelain | sed 's/^...//' | sort > /tmp/update-tools-post.txt
   # Paths the installer actually changed = post minus pre:
   comm -13 /tmp/update-tools-pre.txt /tmp/update-tools-post.txt > /tmp/update-tools-changed.txt
   ```

   Stage **only** those paths
   (`git -C <this-repo> add -- $(cat /tmp/update-tools-changed.txt)`), never
   `git add -A`. If `/tmp/update-tools-changed.txt` is empty the installer was
   a no-op — report "already current" and skip the commit for that tool.

   **This recipe is unchanged whether step 4 ran a resync or a full installer
   re-run for a `layout_version` bump** — `post − pre` captures whatever the
   installer actually touched either way, moved/new/rewired files included; a
   real Repo Skills `layout_version` re-run staged 39 files this way with no
   change to the recipe itself.

2. **Cross-check the flagged set before committing.** Staging is the last point
   at which a dropped repo-local patch is still cheap to recover, so show the
   per-file diffstat and the net line delta of everything the installer
   touched:

   ```bash
   git -C <this-repo> diff --stat --cached -- $(cat /tmp/update-tools-changed.txt)
   # Net line delta per changed file (added - removed), most-negative first —
   # a resync that dropped a local patch shows up as a large negative number:
   git -C <this-repo> diff --numstat --cached -- $(cat /tmp/update-tools-changed.txt) \
     | awk '{printf "%+d\t%s\n", $1 - $2, $3}' | sort -n
   ```

   **Any file from step 4's `LOCAL MODIFICATIONS` set whose net delta is
   negative must be called out by name in the landing summary** — e.g.
   `roles/guide.md: -81 lines — flagged local modification was overwritten;
   re-apply before committing`. Never let it land silently. If the operator
   chose *apply and re-patch*, re-apply the patch **now**, before the commit,
   and re-run the two commands above until that file is no longer
   net-negative; if the patch will not re-apply cleanly, stop and report
   rather than committing the loss.

   A net-negative file that was **not** in the flagged set is normally a
   genuine upstream trim — mention it in the summary, but it needs no action.
   If step 4 flagged nothing, say so explicitly ("no repo-local modifications
   flagged") so the absence is a reported result rather than an unasked
   question.

3. **Detect whether `<this-repo>`'s own default branch is push-protected, then
   commit + land accordingly — without ever committing straight to it.**

   A confirmed bump is safe to fast-forward-merge into *local* `$DEFAULT` on an
   unprotected repo (the existing behavior, unchanged below). But when
   `<this-repo>`'s default branch carries a PR-required ruleset or legacy
   branch protection, that local merge is a dead end: the resulting commit
   cannot be pushed straight to `$DEFAULT`, and the only way out by hand is to
   cherry-pick it onto a fresh branch off `origin/$DEFAULT`, open a PR, and
   reset local `$DEFAULT` afterwards (repo#530). Detect this **before**
   deciding which of the two landing shapes below to use, mirroring
   `.loom/scripts/land-resync-commit.sh`'s `default_branch_requires_pr()`
   exactly — same fail-closed-on-error / fail-open-when-undetectable split,
   same `PROTECTION_SOURCE` side effect (lines 598–625 as of commit
   `082176e`). Reuse the *pattern* here; this command does not source that
   script:

   ```bash
   DEFAULT=$(git -C <this-repo> symbolic-ref --quiet --short refs/remotes/origin/HEAD | sed 's#^origin/##')
   DEFAULT=${DEFAULT:-main}
   CUR=$(git -C <this-repo> symbolic-ref --short HEAD)

   # Try the rulesets API, fall back to legacy branch protection, and treat
   # ANY API failure or unparseable answer as protected — never assume
   # unprotected on ambiguity. {owner}/{repo} here is gh's own placeholder for
   # *this* repo's `origin` remote (the consumer being updated) — resolved
   # from cwd, exactly like step 1's tool discovery. Do NOT substitute the
   # tool-source-repo slug step 2's version check reads; that is a different
   # repo answering a different question.
   #
   # Records WHY in PROTECTION_SOURCE at each protected return, so item 4's
   # report can name the reason instead of just "protected" (same side effect,
   # same strings as the mirrored function).
   PROTECTION_SOURCE=""
   default_branch_requires_pr() {
     if [ "$FORCE_BRANCH" = "1" ]; then
       PROTECTION_SOURCE="--branch forced the branch + PR path (detection skipped)"
       return 0
     fi
     # Fail OPEN in the two cases where detection cannot run *at all*, exactly
     # as the mirrored function does — a Gitea forge, or no `gh` on PATH.
     # This is deliberate, not an oversight: these are the scenarios `--branch`
     # exists for, and failing closed here would both diverge silently from
     # land-resync-commit.sh and make the flag redundant (every non-GitHub repo
     # would take the branch + PR path whether or not it needed one).
     case "$(printf '%s' "${LOOM_FORGE_TYPE:-}" | tr '[:upper:]' '[:lower:]')" in
       gitea) return 1 ;;                         # fail open — pass --branch if it does need a PR
     esac
     command -v gh >/dev/null 2>&1 || return 1    # fail open — same reason
     # From here on the forge *was* asked, so an unhelpful answer fails CLOSED.
     local rule_types
     if ! rule_types="$(cd <this-repo> && gh api "repos/{owner}/{repo}/rules/branches/$DEFAULT" --jq '.[].type' 2>/dev/null)"; then
       PROTECTION_SOURCE="rules API call failed — assumed protected (fail closed)"
       return 0
     fi
     if grep -qvE '^[A-Za-z0-9_]*$' <<< "$rule_types"; then
       PROTECTION_SOURCE="rules API answer unparseable — assumed protected (fail closed)"
       return 0
     fi
     if grep -qxE 'pull_request|required_status_checks' <<< "$rule_types"; then
       PROTECTION_SOURCE="ruleset rule(s): $(grep -xE 'pull_request|required_status_checks' <<< "$rule_types" | tr '\n' ' ' | sed 's/ $//')"
       return 0
     fi
     local legacy
     legacy="$(cd <this-repo> && gh api "repos/{owner}/{repo}/branches/$DEFAULT/protection" \
       --jq '[.required_pull_request_reviews, .required_status_checks] | map(select(. != null)) | length' 2>/dev/null || true)"
     if [[ "$legacy" =~ ^[0-9]+$ && "$legacy" -gt 0 ]]; then
       PROTECTION_SOURCE="legacy branch protection (PR reviews / status checks required)"
       return 0
     fi
     return 1   # the forge definitively answered "no rules"
   }
   ```

   `FORCE_BRANCH=1` is what `--branch` sets — the escape hatch for exactly the
   two fail-open cases above, where the detection cannot run at all (a
   non-GitHub remote with no `gh api` to answer it, or a Gitea forge). On
   those remotes the detector reports "unprotected" because it has no evidence
   either way, so `--branch` is how the operator supplies that evidence by
   hand and forces the branch + PR path.

   ```bash
   MSG="chore(tooling): update <tool> <old>→<new>"
   # Commit-drift update (version identical on both sides): <old>→<new> would
   # read "0.9.0→0.9.0" and say nothing. Use the installed commits instead:
   #   chore(tooling): update <tool> 0.9.0 (abc1234→def5678)
   ```

   **Unprotected, and already on the default branch — the original flow,
   unchanged:**

   ```bash
   if [ "$CUR" = "$DEFAULT" ] && ! default_branch_requires_pr; then
     # On the default branch, and it takes a plain push: commit on a
     # short-lived branch, then fast-forward merge it in — lands on the
     # default branch without a straight-to-main commit.
     tmp="tooling/update-<tool>-<new>"
     git -C <this-repo> checkout -b "$tmp"
     git -C <this-repo> commit -m "$MSG"
     git -C <this-repo> checkout "$DEFAULT"
     git -C <this-repo> merge --ff-only "$tmp"
     git -C <this-repo> branch -d "$tmp"
   fi
   ```

   **Already on a feature branch — unchanged regardless of protection status**
   (protection only changes what happens to *local* `$DEFAULT`, and this arm
   never touches it):

   ```bash
   if [ "$CUR" != "$DEFAULT" ]; then
     # Commit here and report where it landed — do NOT switch branches
     # mid-session and disturb the user's working state.
     git -C <this-repo> commit -m "$MSG"
     echo "Landed the update on '$CUR' (not '$DEFAULT') — you are on a feature branch."
   fi
   ```

   **On the default branch, and it is protected (or `--branch` forced it) —
   land on a `tooling/…` branch in a worktree off `origin/$DEFAULT` instead of
   local `$DEFAULT`, and offer to push + open a PR:**

   ```bash
   if [ "$CUR" = "$DEFAULT" ] && default_branch_requires_pr; then
     WANT_BRANCH="tooling/update-<tool>-<new>"   # requested name; may be rewritten below
     git -C <this-repo> fetch origin "$DEFAULT" --quiet
     if [ -x <this-repo>/.loom/scripts/worktree.sh ] && [ -n "${ISSUE_NUMBER:-}" ]; then
       # A Loom-managed repo, and this run is already attached to a real issue
       # number: use the repo's own worktree helper — never a bare
       # `git worktree add` — it is what writes the `.loom-managed` sentinel
       # that authorizes later cleanup. Do not invent an issue number to
       # unlock this arm; the ordinary ad hoc invocation has none.
       #
       # NOTE: worktree.sh ALWAYS rewrites a custom branch argument to
       # `feature/<arg>` (BRANCH_NAME="feature/$CUSTOM_BRANCH", and it prints
       # "Custom branch '…' resolved to '…' (feature/ prefix applied)"), so the
       # branch this creates is `feature/tooling/update-<tool>-<new>`, NOT the
       # name passed in. The worktree directory is always
       # `issue-$ISSUE_NUMBER` regardless of the branch name.
       <this-repo>/.loom/scripts/worktree.sh "$ISSUE_NUMBER" "$WANT_BRANCH"
       WT="<this-repo>/.loom/worktrees/issue-$ISSUE_NUMBER"
     else
       # No issue number in scope (the normal case for an ad hoc
       # /repo:update-tools run): a plain worktree following the same
       # conventions the helper would use — off origin/$DEFAULT, not off
       # local $DEFAULT, under .loom/worktrees/ when this is a Loom-managed
       # repo, otherwise a sibling scratch directory. `git worktree add -b`
       # takes the name literally — no prefix is applied on this path.
       WT_ROOT="<this-repo>/.loom/worktrees"; [ -d "<this-repo>/.loom" ] || WT_ROOT="/tmp"
       WT="$WT_ROOT/tooling-update-<tool>-<new>"
       git -C <this-repo> worktree add -b "$WANT_BRANCH" "$WT" "origin/$DEFAULT"
     fi
     # The two paths above therefore end up with differently-shaped branch
     # names (`feature/tooling/update-…` vs. `tooling/update-…`). Never reuse
     # $WANT_BRANCH for the push or the PR — ask the worktree what it is
     # actually on, which also covers worktree.sh's "directory already exists"
     # fast path (it reuses that worktree's current branch, typically
     # `feature/issue-$ISSUE_NUMBER`, and creates nothing new):
     BRANCH="$(git -C "$WT" symbolic-ref --short HEAD)"
     # Re-run step 4's installer/updater inside $WT (its target resolution is
     # cwd-based — see step 4's note — so cd into $WT first), then repeat
     # item 1's isolate-the-footprint recipe and item 2's cross-check there
     # before committing, exactly as above:
     git -C "$WT" commit -m "$MSG"
     echo "$DEFAULT is protected — landed on '$BRANCH' in $WT instead of local $DEFAULT."
   fi
   ```

   Fast-forward-merging into local `$DEFAULT` here would be worse than useless
   — the commit would exist only where the operator cannot push it, and
   recovering means exactly the manual cherry-pick-and-reset repo#530
   describes. Skipping straight to a branch avoids ever creating that
   unpushable commit.

   **Pushing the `tooling/…` branch and opening the PR happens only on
   explicit operator confirmation — never automatically:**

   ```bash
   # $BRANCH here is the name re-derived from $WT above — the actual local
   # branch — so this pushes the right refspec on both worktree paths. Using
   # the requested `tooling/update-…` name on the worktree.sh path would fail
   # with `src refspec … does not match any`, since the branch there is
   # `feature/tooling/update-…`.
   git -C "$WT" push -u origin "$BRANCH"
   gh pr create --head "$BRANCH" --base "$DEFAULT" --title "$MSG" \
     --body "Automated tool bump via /repo:update-tools (Safety Rule 5 carve-out)."
   ```

   This is a **narrow carve-out of Safety Rule 5** ("never push"), scoped to
   the `tooling/…` branch alone, confirmed by the operator each time — not a
   repeal of the rule. Rule 5's prohibition on pushing `$DEFAULT` itself is
   unchanged: nothing in this path ever pushes `$DEFAULT`, protected or not.

4. **Report** the resulting commit (`git -C <this-repo> log --oneline -1` or,
   on the protected path, `git -C "$WT" log --oneline -1`), the flagged-set
   outcome from item 2, which of the three landing shapes above was taken and
   why (unprotected default / feature branch / protected default — on the
   protected path, quote `$PROTECTION_SOURCE` as the detector set it, e.g.
   "ruleset rule(s): pull_request", and name the branch actually created,
   which the `worktree.sh` path `feature/`-prefixes), and — for the two branches that stop at
   a local commit — a reminder that it has **not** been pushed (run `git push`
   explicitly to share it). The protected path's PR, once opened, is already
   shared; report its URL instead of a push reminder.

## Safety Rules

1. **Never update without confirmation** — show installed → latest per tool
   first, including commit-drift tools, whose prompt shows installed commit →
   source `origin/HEAD` rather than a version bump. The same prompt must name
   every repo-local modification the resync would overwrite (step 4's
   `LOCAL MODIFICATIONS` block) — an operator cannot consent to losing a patch
   they were never shown. **Never pass `install-anvil.sh --force` on your own
   initiative** to close a frozen-component row (step 4): it is global, not
   per-skill, and has no undo — report the trade-off and the `diff -ru` preview
   and let the operator choose
2. **Always use the tool's own installer or update mechanism** — where a tool
   ships a dedicated non-destructive updater (e.g. Loom's
   `.loom/scripts/resync-installed.sh`), prefer it over re-running the
   installer for ordinary content drift. A `layout_version` bump needs the
   installer re-run regardless (step 4) — that re-run is a safe, idempotent
   update for Repo Skills, Anvil, and kicad-tools; only Loom's
   `--confirm-reinstall` flag is the genuinely destructive fallback, so
   "installer re-run" and "destructive" are not synonyms across tools. Either
   way, never hand-copy files — the installer/updater owns the write
   footprint and marker blocks, and hand-copying breaks reinstall idempotency
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
   **This has one narrow, explicit carve-out**, not a repeal: when step 5's
   detection finds `<this-repo>`'s own default branch is PR-protected (or
   `--branch` forces that path), landing on *local* `$DEFAULT` is not just
   "also outward-facing" — it is actively unpushable, because the operator
   would still have to push it and the ruleset would reject that push exactly
   as it would reject this command doing so. In that case, and only that case,
   step 5 commits on a `tooling/update-<tool>-<new>` branch instead
   (`feature/tooling/update-<tool>-<new>` when `worktree.sh` created it — it
   prefixes custom branch names) and may
   push *that branch* (never `$DEFAULT`) to open a PR, still gated on explicit
   operator confirmation each time. Every other rule above, and this rule's
   prohibition on pushing `$DEFAULT` itself, are unchanged.
