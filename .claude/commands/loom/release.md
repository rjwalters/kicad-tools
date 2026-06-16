# Release Manager

You are preparing a release of **{{workspace}}** from the {{workspace}} repository.

## Overview

This skill guides a careful, interactive release process. Every release must:
1. Verify the main branch is in a release-ready state (CI green or clean-main if CI is absent)
2. Analyze what changed since the last release
3. Help the user decide the correct semver bump
4. Draft and refine the CHANGELOG entry
5. Update version across every version-bearing file (discovered from `./scripts/version.sh list`)
6. Commit, tag, and (with confirmation) push
7. If a release workflow is configured, create a GitHub Release to trigger it

**Do not rush. Each phase requires user confirmation before proceeding.**

**Project-specific customization**: this skill is generic. If your project needs release-time reminders (e.g., "remember to bump the protocol version when the API changes"), drop a `release.md` file in `.loom/context/topics/` — the methodology-injection hook will inject it on every invocation. For procedural overrides that must execute at a specific phase boundary (e.g., "poll workflow X before creating the GitHub Release"), reference one of the named seams documented under [Operator extension points](#operator-extension-points) at the bottom of this skill. Do NOT fork this skill.

## Phase 1: Pre-flight Checks

Before starting, verify the release is safe to cut. The exact CI gate depends on whether the repo has any GitHub Actions workflows configured.

```bash
# Detect whether CI workflows exist. The CI gate degrades gracefully
# when none are present (greenfield repos without CI yet). Uses `find`
# rather than `compgen -G` so the check works under both bash and zsh.
if [ -d ".github/workflows" ] && [ -n "$(find .github/workflows -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null | head -1)" ]; then
  echo "CI workflows detected; checking run status on main..."
  gh run list --branch main --limit 5 --json name,conclusion --jq '.[] | "\(.name): \(.conclusion)"'
else
  echo "No CI workflows detected; using git status + open-PR check as the clean-main gate"
fi

# Check for open PRs that might need to land first
gh pr list --state open --json number,title --jq '.[] | "#\(.number) \(.title)"'

# Check for uncommitted changes (always required)
git status
```

Present findings to the user:
- If CI exists and is failing, stop and fix first.
- If CI is absent, treat clean `git status` + zero blocking open PRs as the gate.
- If there are open PRs, ask if they should land before the release.

<!-- LOOM-EXTENSION-POINT: pre-changelog-style -->

## Phase 1.5: CHANGELOG Completeness Gate

Before gathering changes for the **current** release, verify that the last N shipped tags each have an entry in `CHANGELOG.md`. This catches the "we shipped v0.10.0 and v0.10.1 without adding their CHANGELOG blocks" failure mode — it's cheap to detect at release time and forensically expensive to reconstruct weeks later.

**No-op when CHANGELOG is absent.** If `CHANGELOG.md` does not exist at the repo root, skip this gate entirely — Phase 4 already handles the bootstrap path for young repos.

```bash
# Skip the gate when CHANGELOG.md is absent — Phase 4 will offer to bootstrap it.
if [ ! -f CHANGELOG.md ]; then
  echo "No CHANGELOG.md — skipping completeness gate (Phase 4 will offer bootstrap)"
else
  # Default N=5 — covers roughly a quarter of releases at weekly cadence.
  RECENT_TAG_COUNT=${RECENT_TAG_COUNT:-5}
  missing_tags=()
  # Read the full descending tag list once so we can compute prev-tag ranges.
  # Use a portable read loop (avoids `mapfile`, which is bash 4+ only).
  _all_tags=()
  while IFS= read -r _t; do
    [ -n "$_t" ] || continue   # defensive: skip empty lines from process substitution
    _all_tags+=("$_t")
  done < <(git tag --sort=-v:refname)
  _limit=$RECENT_TAG_COUNT
  if [ "${#_all_tags[@]}" -lt "$_limit" ]; then
    _limit=${#_all_tags[@]}
  fi
  i=0
  while [ "$i" -lt "$_limit" ]; do
    tag="${_all_tags[$i]}"
    # Strip leading 'v' for matching against `## [X.Y.Z]` headers.
    version="${tag#v}"
    if ! grep -qE "^## \[${version}\]" CHANGELOG.md; then
      tag_date=$(git log -1 --format=%cs "$tag" 2>/dev/null || echo "?")
      next_idx=$((i + 1))
      if [ "$next_idx" -lt "${#_all_tags[@]}" ]; then
        prev_tag="${_all_tags[$next_idx]}"
        commit_count=$(git rev-list --count "${prev_tag}..${tag}" 2>/dev/null || echo "?")
      else
        # Oldest tag in the window: fall back to total reachable commits.
        commit_count=$(git rev-list --count "$tag" 2>/dev/null || echo "?")
      fi
      missing_tags+=("$tag ($tag_date, $commit_count commits)")
    fi
    i=$((i + 1))
  done

  if [ "${#missing_tags[@]}" -gt 0 ]; then
    echo "⚠️  CHANGELOG has no entry for the following recent tags:"
    for entry in "${missing_tags[@]}"; do
      echo "    $entry"
    done
  fi
fi
```

If any recent tag is missing an entry, surface the gap to the operator and offer the three-way choice. Interactive prompt format:

```
⚠️  CHANGELOG has no entry for the following recent tags:
    v0.10.0 (2026-06-05, 26 commits)
    v0.10.1 (2026-06-13, 14 commits)

Options:
  [b] Backfill these entries now (drafts entries via Phase 4 logic, one per gap)
  [c] Continue without backfill (leaves the gap in CHANGELOG.md)
  [a] Abort the release

Choose [b/c/a]:
```

### `[b]` Backfill path

For each missing tag (oldest gap first to preserve chronological order in the file):

1. Determine the previous shipped tag (the next-older tag in `git tag --sort=-v:refname`).
2. Reuse Phase 4's draft logic with the `<prev-tag>..<missing-tag>` commit range as input.
3. Present the draft to the operator for revisions exactly as Phase 4 does for the current release.
4. Insert the approved entry into `CHANGELOG.md` in the correct chronological slot (after the next-newer entry, before the next-older entry).
5. Commit each backfill as a separate `docs(changelog): backfill <version> entry` commit, or fold them all into a single `docs(changelog): backfill <X.Y.Z>, <A.B.C>` commit at the operator's preference.

Backfill commits land on `main` before the current-release flow continues — they do **not** become part of the new release tag.

### `[c]` Continue path

Acknowledge the gap and proceed to Phase 2. The gap remains in `CHANGELOG.md`; record nothing extra. This is the right choice for urgent fixes where the operator intends to backfill later.

### `[a]` Abort path

Stop the release. Exit cleanly with a one-line summary listing the missing tags so the operator can plan the backfill before the next attempt.

### `--yes` non-interactive mode

When the skill is invoked non-interactively (e.g., `--yes` flag or detected automation context), do **not** block:

- Print a single-line warning to stderr: `WARN: CHANGELOG missing entries for: v0.10.0, v0.10.1 (continuing — re-run interactively to backfill)`.
- Continue to Phase 2 (equivalent to the `[c]` path).

This keeps automated release pipelines unblocked while leaving an audit trail in the log.

### Tuning

- `RECENT_TAG_COUNT` (default 5) — number of most-recent tags to check. Override via env var for projects with non-weekly cadence.
- The gate scans only the **top N tags by semver descending**. Older gaps are out of scope; if you discover a deeper historical gap, file a separate backfill issue rather than letting it block the current release.

## Phase 2: Gather Changes

### Phase 2a: Detect the version-bumping tool

Before any bump-related probe (current version, `list`, `bump`), detect which version tool the host repo uses. **First match wins**, in this order: bundled `./scripts/version.sh` → `cargo-release` → `bumpversion`/`bump2version` → `poetry` → `npm`. The detected tool is recorded in `VERSION_TOOL` and surfaced to the operator before any bump runs.

`./scripts/version.sh` is intentionally first: it is installed by `install-loom.sh` and may have been deliberately customized for this repo (added a project-specific manifest, removed Loom-internal files). Honoring an explicit script wins over auto-detecting a different tool. Operators who prefer their native tool can delete `scripts/version.sh` after install — the next release will pick up the detected tool instead.

```bash
# Detection order — first match wins. Portable to bash 3.2 (macOS default).
VERSION_TOOL=""
VERSION_TOOL_REASON=""

if [ -x ./scripts/version.sh ]; then
  VERSION_TOOL="version.sh"
  VERSION_TOOL_REASON="./scripts/version.sh is executable"
elif command -v cargo-release >/dev/null 2>&1 && [ -f Cargo.toml ]; then
  VERSION_TOOL="cargo-release"
  VERSION_TOOL_REASON="cargo-release on PATH and Cargo.toml present"
elif command -v bumpversion >/dev/null 2>&1 && { [ -f .bumpversion.cfg ] || [ -f setup.cfg ]; }; then
  VERSION_TOOL="bumpversion"
  VERSION_TOOL_REASON="bumpversion on PATH and .bumpversion.cfg/setup.cfg present"
elif command -v bump2version >/dev/null 2>&1 && [ -f .bumpversion.cfg ]; then
  VERSION_TOOL="bump2version"
  VERSION_TOOL_REASON="bump2version on PATH and .bumpversion.cfg present"
elif command -v poetry >/dev/null 2>&1 && [ -f pyproject.toml ] && grep -q '\[tool.poetry\]' pyproject.toml; then
  VERSION_TOOL="poetry"
  VERSION_TOOL_REASON="poetry on PATH and [tool.poetry] in pyproject.toml"
elif command -v npm >/dev/null 2>&1 && [ -f package.json ]; then
  VERSION_TOOL="npm"
  VERSION_TOOL_REASON="npm on PATH and package.json present"
fi

if [ -n "$VERSION_TOOL" ]; then
  echo "Detected version tool: $VERSION_TOOL ($VERSION_TOOL_REASON)"
else
  echo "No version tool detected. Probed candidates (in order):"
  echo "  1. ./scripts/version.sh        (not executable or absent)"
  echo "  2. cargo-release + Cargo.toml  (one or both missing)"
  echo "  3. bumpversion + .bumpversion.cfg/setup.cfg"
  echo "  4. bump2version + .bumpversion.cfg"
  echo "  5. poetry + pyproject.toml with [tool.poetry]"
  echo "  6. npm + package.json"
fi
```

**Surface the detected tool to the operator** before any subsequent phase runs. If `VERSION_TOOL` is empty, **do not silently proceed** — ask the operator how to handle the bump:

```
No version-bumping tool was detected in this repo.

Options:
  [m] Manual: I'll edit the manifest files myself, then come back to commit + tag.
  [s] Install Loom's bundled scripts/version.sh (re-run `install-loom.sh` or copy it manually)
      and re-invoke /loom:release.
  [a] Abort.

Choose [m/s/a]:
```

On `[m]`, skip Phase 5's automated bump and walk the operator through the manual edit/commit/tag flow with the version they confirmed in Phase 3. On `[s]` or `[a]`, exit cleanly.

### Phase 2b: Gather changes

```bash
# Find the last release tag
git tag --sort=-v:refname | head -1

# Show current version (only if a tool was detected; tool-specific syntax below)
case "$VERSION_TOOL" in
  version.sh)   ./scripts/version.sh ;;
  cargo-release)
    # cargo-release does not have a "show version" subcommand; read it from Cargo.toml.
    grep -m1 '^version' Cargo.toml | sed 's/.*"\(.*\)"/\1/'
    ;;
  bumpversion|bump2version)
    grep -m1 '^current_version' .bumpversion.cfg 2>/dev/null | sed 's/.*=[[:space:]]*//' \
      || grep -m1 '^current_version' setup.cfg 2>/dev/null | sed 's/.*=[[:space:]]*//'
    ;;
  poetry)       poetry version -s ;;
  npm)          node -p "require('./package.json').version" ;;
  *)            echo "(no version tool — operator will report current version manually)" ;;
esac

# List all commits since that tag
git log <last-tag>..HEAD --oneline

# Show the full diff stats
git diff <last-tag>..HEAD --stat
```

Present the user with:
- **Last release**: tag name, date, and version
- **Commits since release**: count and full list
- **Change summary**: categorized by conventional commit prefix (feat, fix, refactor, docs, test, chore)
- **Files changed**: high-level summary of which subsystems were touched

If there are zero commits since the last tag, stop and tell the user there's nothing to release.

## Phase 3: Semver Decision

Present a semver analysis. Reference https://semver.org. The categories below are generic — apply them to whatever public surface your project exposes (libraries, CLIs, protocols, file formats, etc.).

### Breaking Changes (MAJOR bump)
Scan for:
- Removed or renamed public API functions, types, or modules
- Changed function signatures or return types in exported surfaces
- Removed or renamed CLI commands, subcommands, or flags
- Changed CLI command behavior in a way that breaks scripted callers
- Changed wire-protocol / plugin-interface / IPC contracts
- Changed configuration file format in a non-backward-compatible way
- Removed or renamed environment variables that callers set

### New Capabilities (MINOR bump)
- New public API surface (functions, types, modules)
- New CLI commands, subcommands, or flags (additive, backward-compatible)
- New configuration options (with sensible defaults preserving old behavior)
- New optional plugin / protocol / IPC capabilities
- New roles, agents, or orchestration features

### Bug Fixes / Internal (PATCH bump)
- Bug fixes that don't change any public API
- Performance improvements with identical observable behavior
- Internal refactoring not visible to consumers
- Documentation updates
- Dependency bumps (unless they change observable behavior)

Present your recommendation and **ask the user to confirm or override**. Do not proceed until confirmed.

## Phase 4: Draft CHANGELOG

If `CHANGELOG.md` exists at the repo root, draft a new entry following its existing format. Study existing entries to match style.

```bash
# Check whether a CHANGELOG.md exists
if [ -f CHANGELOG.md ]; then
  echo "CHANGELOG.md found — drafting a new entry below ## [Unreleased]"
  head -50 CHANGELOG.md
else
  echo "No CHANGELOG.md found — offering to bootstrap one"
fi
```

If `CHANGELOG.md` is **absent** (e.g., a young repo that hasn't created one yet), ask the user: "No CHANGELOG.md found at the repo root. Create one with the standard 'Keep a Changelog' template? [Y/n]". If yes, write:

```markdown
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [X.Y.Z] - YYYY-MM-DD

### Summary
<one-paragraph release theme>

### Added
- ...
```

If the user declines bootstrap, skip the CHANGELOG update and proceed with version bump only.

Key formatting rules (when `CHANGELOG.md` exists or has just been bootstrapped):
- Use `## [X.Y.Z] - YYYY-MM-DD` header with today's date
- Start with a `### Summary` paragraph describing the release theme
- Group changes under `### Added`, `### Changed`, `### Fixed`, `### Removed`, `### Renamed` as appropriate
- Reference issue numbers with `(#NNN)` format
- Keep descriptions concise but informative
- Omit empty sections

Present the draft and ask for revisions. Iterate until approved.

## Phase 5: Apply Changes

Once the user approves:

1. **Update CHANGELOG.md** (if it exists): Insert the new entry below `## [Unreleased]`.
2. **Discover the version-bearing files** so the user knows what will change. Dispatch on `VERSION_TOOL` from Phase 2a:

   ```bash
   case "$VERSION_TOOL" in
     version.sh)
       ./scripts/version.sh list
       ;;
     cargo-release)
       # cargo-release uses Cargo workspace metadata; show the members it will touch.
       cargo metadata --no-deps --format-version 1 \
         | python3 -c 'import json,sys; m=json.load(sys.stdin)["packages"]; [print(p["manifest_path"]) for p in m]'
       ;;
     bumpversion|bump2version)
       # bumpversion's manifest set lives in the [bumpversion:file:...] sections.
       grep -E '^\[bumpversion:file:' .bumpversion.cfg 2>/dev/null \
         || grep -E '^\[bumpversion:file:' setup.cfg 2>/dev/null \
         || echo "(no [bumpversion:file:*] sections — only the config file itself will be bumped)"
       ;;
     poetry)
       echo "pyproject.toml"
       ;;
     npm)
       echo "package.json"
       [ -f package-lock.json ] && echo "package-lock.json"
       ;;
   esac
   ```

   Show the operator the manifest set the chosen tool will modify. For `bumpversion`/`bump2version` the set is whatever the config declares; for `cargo-release` it is the workspace members; for the others it is the single package manifest.

3. **Bump version**: dispatch the bump command on `VERSION_TOOL`. `<level>` is `patch` / `minor` / `major` from Phase 3; `X.Y.Z` is the resolved version string. Each branch must produce a tagged version commit equivalent to `./scripts/version.sh bump <level> --tag`.

   ```bash
   case "$VERSION_TOOL" in
     version.sh)
       ./scripts/version.sh bump <level> --tag
       ;;
     cargo-release)
       # cargo-release defaults to dry-run; --execute performs the work.
       # --no-publish skips `cargo publish` (the GitHub Release flow in Phase 6 handles distribution).
       cargo release <level> --execute --no-publish
       ;;
     bumpversion)
       bumpversion <level> --tag --commit
       ;;
     bump2version)
       bump2version <level> --tag --commit
       ;;
     poetry)
       poetry version <level>
       git add pyproject.toml
       git commit -m "chore: bump version to $(poetry version -s)"
       git tag "v$(poetry version -s)"
       ;;
     npm)
       # npm version handles commit + tag automatically; --no-git-tag-version=false is the default.
       npm version <level> -m "chore: bump version to %s"
       ;;
   esac
   ```

   - Each branch produces both the commit and the tag in a form the rest of the skill can push.
   - Tool-specific side effects (lockfile regeneration, etc.) are handled by the tool itself; do not double-update.

4. **Verify**:

   ```bash
   case "$VERSION_TOOL" in
     version.sh)   ./scripts/version.sh check ;;
     cargo-release) cargo check --workspace ;;
     bumpversion|bump2version)
       # bumpversion writes current_version back to the config; re-read to confirm.
       grep -m1 '^current_version' .bumpversion.cfg 2>/dev/null \
         || grep -m1 '^current_version' setup.cfg 2>/dev/null
       ;;
     poetry)       poetry version ;;
     npm)          node -p "require('./package.json').version" ;;
   esac
   git tag --sort=-v:refname | head -1   # confirm the new tag exists
   ```

Note: every tool in the dispatch above creates its own commit. To keep the CHANGELOG bump and the version bump together in a single tagged commit, commit the CHANGELOG first and then move the tag forward after the version bump:

```bash
git add CHANGELOG.md
git commit -m "docs: add X.Y.Z changelog entry"
# ...run the tool-specific bump above...
# Move tag to include both commits
git tag -f vX.Y.Z
```

Show the user the result and ask for final confirmation.

<!-- LOOM-EXTENSION-POINT: pre-push -->

## Phase 6: Push and Release

After final confirmation:

1. **Push commits and tag**:
   ```bash
   git push origin main --tags
   ```

<!-- LOOM-EXTENSION-POINT: post-push -->

2. **Create GitHub Release**:
   <!-- LOOM-EXTENSION-POINT: pre-github-release -->
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file - <<< "$(changelog excerpt)"
   ```
   Use the CHANGELOG entry as the release notes.

3. **Build workflow trigger** (only when a release workflow is configured):
   ```bash
   if ls .github/workflows/release.yml 2>/dev/null; then
     echo "release.yml detected — the GitHub Release will trigger the build workflow."
     gh run list --workflow=release.yml --limit 1
   else
     echo "No release.yml workflow detected — the GitHub Release will not trigger any build."
   fi
   ```

**Do not push or create the release without explicit user confirmation.**

## Phase 7: Post-Release Summary

Present a summary. Tailor the build-workflow line based on whether a release workflow was detected in Phase 6, and the version-files line based on the detected tool from Phase 2a:

```
## Release Complete

- Version: vX.Y.Z
- Commit: <sha>
- Tag: vX.Y.Z
- Version tool: <VERSION_TOOL or "manual" if no tool detected>
- GitHub Release: created
- Build workflow: [triggered / N/A — no release workflow configured]
- CHANGELOG: updated with N items
- Version files updated: <tool-specific count or summary>
```

For the version-files line, report what the chosen tool actually modified:

- `version.sh`: `$(./scripts/version.sh list | wc -l | tr -d ' ')` files (see `./scripts/version.sh list`)
- `cargo-release`: the workspace member set (from Phase 5 step 2)
- `bumpversion`/`bump2version`: the `[bumpversion:file:*]` set from `.bumpversion.cfg` / `setup.cfg`
- `poetry`: `pyproject.toml`
- `npm`: `package.json` (+ `package-lock.json` if present)

<!-- LOOM-EXTENSION-POINT: post-summary -->

## `scripts/version.sh` interface

When the skill detects `./scripts/version.sh` (Phase 2a), it dispatches the following subcommands. Projects that ship a custom `scripts/version.sh` from a pre-v0.10.3 install must implement these, or delete the script entirely and let detection fall through to the next supported tool (`cargo-release`, `bumpversion`, `poetry`, `npm`).

| Subcommand | Purpose | Required by Phase |
|---|---|---|
| `./scripts/version.sh` | Print current version to stdout | 2b |
| `./scripts/version.sh list` | List version-bearing files, one per line | 5 step 2 |
| `./scripts/version.sh check` | Verify all version-bearing files agree | 5 step 4 |
| `./scripts/version.sh bump <level> --tag` | Bump (`patch`/`minor`/`major`), commit, tag | 5 step 3 |
| `./scripts/version.sh set <version> [--tag]` | Set explicit version, commit, optionally tag | (not used by skill; supported by Loom's bundled script for operator convenience) |

The skill currently never invokes `set`, but it is documented here because the bundled `scripts/version.sh` ships with it and downstream `version.sh` forks should not silently drop it.

## Operator extension points

This skill exposes the following named seams (HTML-comment markers) that project-specific topic injections can target. Seam names are stable contracts — once published they will not be renamed; new seams may be added over time. Markers are HTML comments, so they do not render in the prose.

| Seam | Location | Intended use |
|---|---|---|
| `pre-changelog-style` | Just before Phase 1.5 (CHANGELOG Completeness Gate) | Inject CHANGELOG-style overrides (e.g., themed-section grouping, Keep-a-Changelog opt-outs, project-specific entry conventions). |
| `pre-push` | Just before Phase 6 (Push and Release) | Inject the project's irreversibility prompt or any final pre-push gate (e.g., "confirm you intend to push tag `vX.Y.Z` and trigger N downstream workflows"). |
| `post-push` | Inside Phase 6, after the `git push origin main --tags` step and before the GitHub Release is created | Inject post-push procedural steps such as polling multiple registry/publish workflows for completion before continuing. |
| `pre-github-release` | Inside Phase 6, immediately before `gh release create` | Inject pre-release-creation gates (e.g., "wait for both Crates and npm workflows to finish before creating the GitHub Release"). |
| `post-summary` | After Phase 7 (Post-Release Summary) | Inject project-specific follow-up steps (e.g., "post release announcement", "ping #releases Slack channel", "open the next milestone"). |

**How projects use these seams**: drop a `release.md` file in `.loom/context/topics/` (the existing methodology-injection mechanism) and reference the target seam name in prose. The agent reading the skill plus the injected topic file will compose them at runtime. Example topic snippet:

```markdown
At extension point `pre-github-release`: do NOT run `gh release create` until BOTH of the following workflows succeed:
  - `.github/workflows/publish-crate.yml`
  - `.github/workflows/publish-npm.yml`
Poll with `gh run list --workflow=<file> --limit 1 --json conclusion` until both report `success`.
```

Projects that find injection insufficient (i.e. need to REPLACE a phase's content, not just inject alongside it) should file an issue requesting Option A (named phase-extension files) — see the architect notes on #3503.

## Important Notes

- **Version tool detection** (Phase 2a): the skill detects the host repo's version-bumping tool in a fixed order — `./scripts/version.sh` → `cargo-release` → `bumpversion`/`bump2version` → `poetry` → `npm` — and dispatches the bump command on the detected tool. The bundled `./scripts/version.sh` is intentionally first so Loom installs that have been customized for the repo continue to be honored.
- **Discover, don't hardcode**: the set of version-bearing files is discovered at release time from whichever tool is detected (`./scripts/version.sh list`, Cargo workspace metadata, `[bumpversion:file:*]` sections, or the single canonical manifest for poetry/npm). Do not bake a count or path list into prose.
- **Release workflow trigger** (when applicable): if `.github/workflows/release.yml` exists, it typically triggers on GitHub Release creation (`release: types: [created]`), NOT on tag push. In that case you must create a GitHub Release via `gh release create` to trigger the build. If no release workflow is configured, the tag push alone completes the release and no build artifacts are produced.
- **Conventional commits**: many projects (including this one if it uses `feat:` / `fix:` / `chore:` prefixes) use conventional commits to drive the semver decision. Use the prefix breakdown from Phase 2 as input to Phase 3.
- **Branch protection**: direct pushes to main from a release flow may show a ruleset bypass warning — this is expected for release commits when the project's policy allows admin bypass for tagged releases. If your project doesn't allow that, run the release through a PR instead.
