---
name: "release"
description: "Cut a release — pre-flight checks, semver decision, CHANGELOG, version bump, tag, and GitHub Release"
domain: repo
type: command
user-invocable: true
---

# /repo:release — Cut a Release

Guide a careful, interactive release of this repository. Every phase requires
confirmation before proceeding — **do not rush, and never push or tag without
an explicit yes.** The version-bearing files and the bump tool are *discovered*
at release time, never hardcoded, so this works in any repo.

## Usage

```
/repo:release                  # Interactive release from the default branch
```

## Phase 1 — Pre-flight

Confirm the repo is safe to cut from. The CI gate degrades gracefully when no
workflows exist.

```bash
# CI status, if CI exists at all
if [ -d ".github/workflows" ] && [ -n "$(find .github/workflows -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null | head -1)" ]; then
  gh run list --branch "$(git symbolic-ref --short HEAD)" --limit 5 --json name,conclusion --jq '.[] | "\(.name): \(.conclusion)"'
else
  echo "No CI workflows — using clean tree + zero blocking PRs as the gate"
fi
gh pr list --state open --json number,title --jq '.[] | "#\(.number) \(.title)"'
git status --porcelain
```

- CI present and failing → stop, fix first.
- CI absent → clean `git status` + no blocking open PRs is the gate.
- Open PRs that should land first → ask.

## Phase 1.5 — CHANGELOG completeness gate

Before drafting this release's entry, verify recent shipped tags each have a
CHANGELOG entry — cheap to catch now, expensive to reconstruct later. **No-op if
`CHANGELOG.md` is absent** (Phase 4 bootstraps it).

```bash
[ -f CHANGELOG.md ] || echo "(no CHANGELOG.md — skipping gate)"
# For each of the last ~5 tags (git tag --sort=-v:refname), check that
# CHANGELOG.md contains a header for its version (strip a leading 'v').
```

For any tag missing an entry, surface the gap and offer: **[b]** backfill it now
(draft via Phase 4 logic over the `<prev-tag>..<tag>` range, insert in
chronological order, commit separately — backfills do **not** join the new
release tag), **[c]** continue and leave the gap, or **[a]** abort.

## Phase 2 — Detect the version tool

Detect the host repo's bump mechanism. **First match wins**, in this order. An
explicit `scripts/version.sh` is honored first; a plain `VERSION` file is the
most-general fallback.

```bash
VERSION_TOOL="" ; WHY=""
if [ -x ./scripts/version.sh ]; then
  VERSION_TOOL="version.sh"; WHY="./scripts/version.sh is executable"
elif command -v cargo-release >/dev/null 2>&1 && [ -f Cargo.toml ]; then
  VERSION_TOOL="cargo-release"; WHY="cargo-release + Cargo.toml"
elif command -v cargo-set-version >/dev/null 2>&1 && [ -f Cargo.toml ]; then
  VERSION_TOOL="cargo-set-version"; WHY="cargo-edit + Cargo.toml"
elif [ -f Cargo.toml ] && grep -q '^\[workspace\.package\]' Cargo.toml; then
  VERSION_TOOL="cargo-workspace"; WHY="Cargo [workspace.package] direct-edit"
elif command -v bumpversion >/dev/null 2>&1 && { [ -f .bumpversion.cfg ] || [ -f setup.cfg ]; }; then
  VERSION_TOOL="bumpversion"; WHY="bumpversion + config"
elif command -v bump2version >/dev/null 2>&1 && [ -f .bumpversion.cfg ]; then
  VERSION_TOOL="bump2version"; WHY="bump2version + .bumpversion.cfg"
elif command -v poetry >/dev/null 2>&1 && [ -f pyproject.toml ] && grep -q '\[tool.poetry\]' pyproject.toml; then
  VERSION_TOOL="poetry"; WHY="poetry + [tool.poetry]"
elif command -v npm >/dev/null 2>&1 && [ -f package.json ]; then
  VERSION_TOOL="npm"; WHY="npm + package.json"
elif [ -f VERSION ]; then
  VERSION_TOOL="version-file"; WHY="plain VERSION file at repo root"
fi
echo "${VERSION_TOOL:-<none>} — ${WHY:-no tool detected}"
```

**Surface the detected tool to the user.** If none is detected, do not proceed
silently — offer: **[m]** manual (they edit manifests, you commit + tag), or
**[a]** abort.

### Drift gate (multi-file tools only)

Tools with more than one version-bearing file can disagree, and a blind bump
would mis-delta the drifted one. Before reading the current version, verify
agreement — `./scripts/version.sh check` (fatal if it fails), a `bumpversion
--dry-run --allow-dirty` probe (advisory), etc. Single-source tools
(`cargo` inheritance, `poetry`, `npm`, `version-file`) are drift-free — skip.

## Phase 3 — Gather changes & decide the bump

```bash
last=$(git tag --sort=-v:refname | head -1)
git log "${last}..HEAD" --oneline
git diff "${last}..HEAD" --stat
```

Read the current version per tool (`./scripts/version.sh`; `grep -m1 '^version'
Cargo.toml`; `poetry version -s`; `node -p "require('./package.json').version"`;
`cat VERSION`; …). If there are **zero** commits since the last tag, stop —
nothing to release.

Present a semver analysis (https://semver.org) against whatever public surface
the repo exposes (API, CLI, protocol, config, file formats):

- **MAJOR** — removed/renamed public API, CLI, flags; broken wire/config contracts.
- **MINOR** — new backward-compatible API, commands, flags, options.
- **PATCH** — bug fixes, perf with identical behavior, internal refactors, docs.

Use conventional-commit prefixes (`feat`/`fix`/`chore`…) as input. Recommend a
level and **ask the user to confirm or override.**

## Phase 4 — Draft the CHANGELOG

If `CHANGELOG.md` exists, study its format and draft a new entry matching it
(header with today's date, a summary line, grouped changes, issue refs). If it's
**absent**, offer to bootstrap a "Keep a Changelog" template. Present the draft
and iterate until approved. Omit empty sections.

## Phase 5 — Apply

Once approved:

1. Insert the new entry into `CHANGELOG.md`.
2. **Show the version-bearing files** the tool will touch, then bump. Dispatch on
   the detected tool; each branch must produce a version commit **and** tag:

   ```bash
   case "$VERSION_TOOL" in
     version.sh)        ./scripts/version.sh bump <level> --tag ;;
     cargo-release)     cargo release <level> --execute --no-publish ;;
     cargo-set-version) cargo set-version --bump <level> --workspace && cargo update --workspace ;;  # then commit + tag
     cargo-workspace)   sed -i.bak -E 's/^version = "[0-9.]+"/version = "'"$NEW"'"/' Cargo.toml && rm -f Cargo.toml.bak && cargo update --workspace ;;  # then commit + tag
     bumpversion)       bumpversion <level> --tag --commit ;;
     bump2version)      bump2version <level> --tag --commit ;;
     poetry)            poetry version <level> ;;  # then commit + tag v$(poetry version -s)
     npm)               npm version <level> -m "chore: bump version to %s" ;;
     version-file)      printf '%s\n' "$NEW" > VERSION ;;  # then commit (with CHANGELOG) + tag v$NEW
   esac
   ```

   For tools that don't self-commit (`cargo-set-version`, `cargo-workspace`,
   `poetry`, `version-file`), stage the bumped files **plus `CHANGELOG.md`**,
   commit, and `git tag -a "v$NEW" -m "v$NEW"` — match the repo's existing
   commit/tag convention (check `git log` and `git tag`).
3. **Verify**: re-read the version and confirm the tag exists
   (`git tag --sort=-v:refname | head -1`). For cargo, `cargo check --workspace`.

Show the result and get final confirmation.

## Phase 6 — Push & release

After an explicit yes:

```bash
git push origin "$(git symbolic-ref --short HEAD)" --follow-tags
```

If a release workflow exists (`.github/workflows/release.yml`, typically
triggered on Release creation rather than tag push), create a GitHub Release so
it fires; use the CHANGELOG entry as the notes:

```bash
gh release create "v$NEW" --title "v$NEW" --notes-file <(sed -n "/^## \[\?$NEW/,/^## /p" CHANGELOG.md)
```

Otherwise the tag push alone completes the release.

## Phase 7 — Summary

```
RELEASE COMPLETE
================
Version:   v0.3.0
Tag:       v0.3.0 (pushed)
Tool:      version-file
CHANGELOG: 1 entry added
Release:   GitHub Release created  (or: tag push only — no release workflow)
```

## Principles

Cutting a release is irreversible and outward-facing, so unlike the safe-fix
hygiene commands it stays **report first, act second** — nothing is committed,
tagged, or pushed without a yes. **General by design** — the tool and the file
set are discovered, never assumed. If the repo needs a release-time reminder
(e.g. "bump the protocol version when the API changes"), keep it in the repo's
own CLAUDE.md; this command reads that context at runtime.
