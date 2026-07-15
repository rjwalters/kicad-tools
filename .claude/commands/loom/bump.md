# Version Bump + Tag (generic)

You are bumping the version of {{workspace}} — a **generic project**, not necessarily Loom itself.

## When to use this skill

- The operator wants to release a new version of **their project**.
- This is a lightweight, no-CHANGELOG quick-bump. For a full versioned release with CHANGELOG gates, semver decision, and GitHub Release, use `/repo:release` from [rjwalters/repo](https://github.com/rjwalters/repo).
- Works for any project shape: an npm package, a Cargo crate, a Python package, a single-script shell project, an npm+cargo monorepo, etc.

If the operator wants the full release methodology (pre-flight/CI gate, CHANGELOG completeness + version-drift gates, tag, GitHub Release), they should install [repo](https://github.com/rjwalters/repo) and use `/repo:release` — it detects and honors `scripts/version.sh` as its first-priority version tool. `/loom:bump` is the quick-bump for when that full flow is overkill.

**Do not rush.** Each phase requires explicit confirmation before proceeding to the next.

## Phase 1: Detect version sources

Walk the current repository root **once** and record which version-bearing files exist. Look for these sources **in order** and remember every match — multiple shapes can coexist (e.g. an npm+cargo monorepo).

### Detection order

1. **`scripts/version.sh`** (top-level): if this file already exists from a prior `/loom:bump` run, **short-circuit** the detection. Skip directly to Phase 5 ("Invoke the generated script"). The script is the source of truth on subsequent runs — the operator may have edited it.

2. **`package.json`** (top-level): an npm project. Read `.version` via `jq -r .version`.

3. **`*/package.json`** (workspace packages): an npm workspace / monorepo. Glob the top-level for subdirectory `package.json` files and read `.version` from each. Common patterns: `packages/*/package.json`, `apps/*/package.json`, or simply `<name>/package.json` (Loom's `mcp-loom/package.json` shape).

4. **`Cargo.toml`** (top-level + workspace members): a Cargo crate or workspace.
   - If `[package].version` is present at top level, that's a single-crate project.
   - If `[workspace].members` lists subdirectories, each member's `Cargo.toml` has its own `[package].version`.
   - If `Cargo.lock` exists, it must be refreshed via `cargo update -w <member>` (or `cargo update -w` for all) after each `Cargo.toml` change so the lockfile stays in sync.

5. **`pyproject.toml`**: a Python project. Read **`[project].version`** (PEP 621) OR **`[tool.poetry].version`** (Poetry style). These are mutually exclusive — never both.

6. **`setup.py` / `setup.cfg`** (legacy Python): some older Python projects still use these. Look for `version=` in `setup.py` or `version = ` under `[metadata]` in `setup.cfg`.

7. **Top-level shell script with `VERSION="X.Y.Z"` line**: a single-file shell-script project (the `rjwalters/clean` shape). Grep for `^VERSION="[0-9]+\.[0-9]+\.[0-9]+"` in top-level `*.sh` files. Common script names: `cleanup.sh`, `install.sh`, `setup.sh`, `<projectname>.sh`.

8. **`CLAUDE.md` and `README.md`**: markdown projects that embed the version in human-readable docs. Look for the line `**Version**: X.Y.Z` (or `**Loom Version**: X.Y.Z` — Loom's own pattern). Use ripgrep / grep with the regex `^\*\*[A-Za-z ]*Version\*\*: [0-9]+\.[0-9]+\.[0-9]+`.

### Cross-check

If multiple sources are detected, they should agree on the current version. If they disagree, **surface the inconsistency to the operator and ask** before proceeding:

```
Detected version sources:
  - package.json: 0.4.1
  - Cargo.toml:   0.4.0  ← MISMATCH
  - CLAUDE.md:    0.4.1

Sources disagree on the current version. The most-common value is 0.4.1.
Should I treat 0.4.1 as the current version and update Cargo.toml to match,
or do you want to investigate first?
```

### No sources detected

If **no** version-bearing files are found, stop and tell the operator:

> No version sources detected in this project. Supported shapes are: `package.json`, `Cargo.toml`, `pyproject.toml`, `setup.py`/`setup.cfg`, top-level shell script with `VERSION="X.Y.Z"`, and `CLAUDE.md` / `README.md` with `**Version**: X.Y.Z`. If your project uses a different shape, add one of these conventions first.

## Phase 2: Ensure `CHANGELOG.md` exists with `[Unreleased]`

Look for `CHANGELOG.md` at the repository root.

- **If present and contains `## [Unreleased]`**: continue.
- **If present but missing `## [Unreleased]`**: ask the operator to add one, or offer to insert it just below the file header.
- **If absent entirely**: offer to scaffold a minimal Keep-a-Changelog header. Ask first — do not write without confirmation.

Suggested scaffold:

```markdown
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

```

## Phase 3: Compute the new version (semver bump)

Ask the operator:

> What level should I bump — `patch`, `minor`, or `major`? Or do you want to set an explicit version like `1.2.3`?

Apply semver rules:

- `patch`: `X.Y.Z` → `X.Y.(Z+1)`
- `minor`: `X.Y.Z` → `X.(Y+1).0`
- `major`: `X.Y.Z` → `(X+1).0.0`
- explicit: `X.Y.Z` (validate format — three integers separated by dots)

Show the operator the computed new version and ask for confirmation.

## Phase 4: Draft the changelog entry

This is a **human-in-the-loop** step. **Never auto-generate the changelog content from commits.** Open `CHANGELOG.md`, find the `## [Unreleased]` section, and:

1. If the operator has already written content under `## [Unreleased]`, present it and ask whether to use it as-is or edit.
2. If the section is empty, ask the operator to dictate the entry. Suggest the categories from Keep a Changelog: `### Added`, `### Changed`, `### Fixed`, `### Removed`, `### Deprecated`, `### Security`.

Once approved, **promote** the `## [Unreleased]` heading to `## [X.Y.Z] - YYYY-MM-DD` (today's UTC date, ISO-8601). Insert a fresh empty `## [Unreleased]` section above the new one so the next release has a place to accumulate.

Example transform:

```diff
-## [Unreleased]
+## [Unreleased]
+
+## [0.4.2] - 2026-06-05

 ### Fixed
 - Foo bug
```

## Phase 5: Generate (or update) `scripts/version.sh`

### If `scripts/version.sh` already exists

Invoke it directly:

```bash
./scripts/version.sh bump <level> --tag
```

The generated script is the source of truth — the operator may have customized it. **Do not silently overwrite** an existing `scripts/version.sh`. If you suspect drift (e.g. a new version-bearing file was added since the script was generated), offer a diff and ask before replacing.

### If `scripts/version.sh` does NOT exist

Generate one tailored to the detected sources. The template structure below mirrors Loom's own `scripts/version.sh` but **parameterized** for whatever shape was detected. Adapt the per-shape sections — emit only the readers/writers for files that actually exist in this project.

#### Template (adapt to detected sources)

```bash
#!/usr/bin/env bash
# version.sh - Manage version across all project packages
# Generated by /loom:bump — safe to edit; subsequent /loom:bump runs reuse this script.
#
# Usage:
#   ./scripts/version.sh                  # Show current version
#   ./scripts/version.sh check            # Verify all files are in sync
#   ./scripts/version.sh bump patch       # X.Y.Z → X.Y.(Z+1)
#   ./scripts/version.sh bump minor       # X.Y.Z → X.(Y+1).0
#   ./scripts/version.sh bump major       # X.Y.Z → (X+1).0.0
#   ./scripts/version.sh set 1.2.3        # Set explicit version
#   ./scripts/version.sh set 1.2.3 --tag  # Set + commit + tag

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# All files that contain the version string.
# EDIT THIS LIST when you add or remove version-bearing files.
VERSION_FILES=(
  # "package.json"            # npm shape
  # "mcp-loom/package.json"   # npm-workspace shape
  # "loom-daemon/Cargo.toml"  # cargo-workspace shape
  # "pyproject.toml"          # python shape
  # "cleanup.sh"              # shell-VERSION shape (top-level)
  # "CLAUDE.md"               # markdown-version shape
  # "README.md"
)

# Pick the canonical source for `get_version` — usually the first
# entry in VERSION_FILES, but parameterized so the project can choose.
get_version() {
  # Example for npm shape:
  # jq -r '.version' "$REPO_ROOT/package.json"
  echo "REPLACE_WITH_CANONICAL_SOURCE_READER"
}

get_version_from_file() {
  local file="$1"
  case "$file" in
    *.json)
      jq -r '.version' "$REPO_ROOT/$file"
      ;;
    *.toml)
      # PEP 621: [project] version = "..."
      # Poetry: [tool.poetry] version = "..."
      # Cargo:  [package] version = "..."
      grep -m1 '^version' "$REPO_ROOT/$file" | sed 's/version = "\(.*\)"/\1/'
      ;;
    *.sh)
      # Top-level shell script with VERSION="X.Y.Z"
      grep -m1 '^VERSION=' "$REPO_ROOT/$file" | sed 's/VERSION="\(.*\)"/\1/'
      ;;
    *.md)
      grep -o '\*\*[A-Za-z ]*Version\*\*: [0-9]*\.[0-9]*\.[0-9]*' "$REPO_ROOT/$file" \
        | grep -o '[0-9]*\.[0-9]*\.[0-9]*'
      ;;
  esac
}

check_versions() {
  local expected
  expected=$(get_version)
  local all_match=true

  for file in "${VERSION_FILES[@]}"; do
    local actual
    actual=$(get_version_from_file "$file")
    if [ "$actual" != "$expected" ]; then
      echo "MISMATCH  $file: $actual (expected $expected)"
      all_match=false
    else
      echo "OK        $file: $actual"
    fi
  done

  # If Cargo.lock is present, verify it agrees too.
  if [ -f "$REPO_ROOT/Cargo.lock" ]; then
    # The set of workspace members to check is project-specific — list them here.
    # local lock_versions
    # lock_versions=$(grep -A1 'name = "PROJECT-CRATE"' "$REPO_ROOT/Cargo.lock" \
    #                 | grep '^version' | sed 's/version = "\(.*\)"/\1/' | sort -u)
    : # remove this line and add real checks for Cargo.lock workspace crates
  fi

  if $all_match; then
    echo ""
    echo "All versions in sync: $expected"
    return 0
  else
    echo ""
    echo "Version mismatch detected. Run: $0 set $expected"
    return 1
  fi
}

bump_version() {
  local current="$1"
  local part="$2"

  IFS='.' read -r major minor patch <<< "$current"

  case "$part" in
    major) echo "$((major + 1)).0.0" ;;
    minor) echo "$major.$((minor + 1)).0" ;;
    patch) echo "$major.$minor.$((patch + 1))" ;;
    *) echo "Unknown bump type: $part (use major, minor, or patch)" >&2; exit 1 ;;
  esac
}

set_version() {
  local new_version="$1"

  if ! [[ "$new_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid version format: $new_version (expected X.Y.Z)" >&2
    exit 1
  fi

  local old_version
  old_version=$(get_version)

  echo "Updating version: $old_version → $new_version"
  echo ""

  # ---- Per-shape writers — emit ONLY the sections that match the detected shapes. ----
  #
  # JSON files (npm / npm-workspace):
  # for file in package.json mcp-loom/package.json; do
  #   local tmp; tmp=$(mktemp)
  #   jq --arg v "$new_version" '.version = $v' "$REPO_ROOT/$file" > "$tmp"
  #   mv "$tmp" "$REPO_ROOT/$file"
  #   echo "  Updated $file"
  # done
  #
  # Cargo.toml files (cargo / cargo-workspace):
  # for file in loom-daemon/Cargo.toml loom-api/Cargo.toml; do
  #   awk -v ver="$new_version" '!done && /^version = "/ { print "version = \"" ver "\""; done=1; next } 1' \
  #     "$REPO_ROOT/$file" > "$REPO_ROOT/$file.tmp" && mv "$REPO_ROOT/$file.tmp" "$REPO_ROOT/$file"
  #   echo "  Updated $file"
  # done
  #
  # pyproject.toml ([project].version OR [tool.poetry].version):
  # awk -v ver="$new_version" '!done && /^version = "/ { print "version = \"" ver "\""; done=1; next } 1' \
  #   "$REPO_ROOT/pyproject.toml" > "$REPO_ROOT/pyproject.toml.tmp" \
  #   && mv "$REPO_ROOT/pyproject.toml.tmp" "$REPO_ROOT/pyproject.toml"
  # echo "  Updated pyproject.toml"
  #
  # Top-level shell script with VERSION="X.Y.Z":
  # sed -i '' "s/^VERSION=\".*\"/VERSION=\"$new_version\"/" "$REPO_ROOT/cleanup.sh"
  # echo "  Updated cleanup.sh"
  #
  # Markdown (CLAUDE.md / README.md) with **Version**: X.Y.Z:
  # sed -i '' "s/\*\*Version\*\*: .*/\*\*Version\*\*: $new_version/" "$REPO_ROOT/CLAUDE.md"
  # echo "  Updated CLAUDE.md"
  #
  # Cargo.lock refresh (only if Cargo.toml(s) were updated):
  # (cd "$REPO_ROOT" && cargo update -w 2>/dev/null) && echo "  Updated Cargo.lock"

  echo ""
  echo "Version set to $new_version"
}

do_tag() {
  local version="$1"

  echo ""
  echo "Committing and tagging..."
  (
    cd "$REPO_ROOT"
    # Stage every version-bearing file PLUS Cargo.lock (if it exists) and CHANGELOG.md.
    # EDIT THIS LIST to match VERSION_FILES above.
    git add "${VERSION_FILES[@]}"
    [ -f "Cargo.lock" ] && git add Cargo.lock
    [ -f "CHANGELOG.md" ] && git add CHANGELOG.md
    git commit -m "chore: bump version to $version"
    git tag -a "v$version" -m "v$version"
  )
  echo ""
  echo "Created commit and tag v$version"
  echo "Push with: git push origin HEAD --tags"
}

# --- Main ---

case "${1:-}" in
  ""|show)
    echo "$(get_version)"
    ;;
  check)
    check_versions
    ;;
  bump)
    part="${2:-patch}"
    current=$(get_version)
    new_version=$(bump_version "$current" "$part")
    set_version "$new_version"
    if [ "${3:-}" = "--tag" ]; then
      do_tag "$new_version"
    fi
    ;;
  set)
    if [ -z "${2:-}" ]; then
      echo "Usage: $0 set <version> [--tag]" >&2
      exit 1
    fi
    set_version "$2"
    if [ "${3:-}" = "--tag" ]; then
      do_tag "$2"
    fi
    ;;
  *)
    echo "Usage: $0 [show|check|bump <major|minor|patch> [--tag]|set <version> [--tag]]"
    exit 1
    ;;
esac
```

Write the generated `scripts/version.sh` with the placeholder sections **filled in** for the detected shapes and the unused sections **removed** (don't ship commented-out templates to the operator's repo). Make the script executable: `chmod +x scripts/version.sh`. Show the operator the generated file before saving.

## Phase 6: Run the bump + tag flow

Once `scripts/version.sh` exists, invoke it:

```bash
./scripts/version.sh bump <level> --tag
```

This will:
1. Update every file in `VERSION_FILES` to the new version.
2. Refresh `Cargo.lock` (if present).
3. Stage the changes plus `CHANGELOG.md`.
4. Create a commit `chore: bump version to X.Y.Z`.
5. Create an annotated tag `vX.Y.Z`.

**Verify** the result before proceeding:

```bash
./scripts/version.sh check     # all versions in sync
git log -1 --oneline           # commit message
git tag --list 'v*' | tail -1  # new tag
```

Show the operator the diff and the new commit. Ask for final confirmation before Phase 7.

## Phase 7: Push and `gh release create` (OPTIONAL)

This phase is **explicitly gated**. Ask the operator:

> Push to origin and create a GitHub Release? (y/N)

Do NOT proceed without an explicit "yes".

If confirmed:

```bash
git push origin HEAD
git push origin "v$NEW_VERSION"
```

Then create the GitHub Release. Use the just-promoted changelog block as the release notes:

```bash
# Extract the most recent ## [X.Y.Z] - DATE block from CHANGELOG.md
notes=$(awk '/^## \['"$NEW_VERSION"'\]/{flag=1; next} /^## \[/{flag=0} flag' CHANGELOG.md)

gh release create "v$NEW_VERSION" \
  --title "v$NEW_VERSION" \
  --notes "$notes"
```

**Do NOT publish to package registries.** This skill does not run `npm publish`, `cargo publish`, `twine upload`, or any other registry push. Tag + GitHub Release only. If the operator wants registry publication, that's a separate manual step (or a CI workflow they configure on their own).

If the project does not use GitHub (e.g. it's on Gitea or self-hosted), skip the `gh release create` step and just push the tag. The operator can create the release through their forge's UI.

## Phase 8: Summary

Present a final summary:

```
Release v$NEW_VERSION complete

- Version: $NEW_VERSION
- Commit:  <sha>
- Tag:     v$NEW_VERSION
- Files:   <list of updated files>
- Pushed:  yes / no
- Release: created / skipped
```

## Notes and constraints

- **Do not auto-generate CHANGELOG content from commits.** The `## [Unreleased]` content comes from the operator. The skill only *promotes* the existing `[Unreleased]` heading to `[X.Y.Z] - DATE`.
- **Do not publish to package registries.** Tag + GitHub Release only.
- **For the full release methodology, defer to `/repo:release`.** That command (from [rjwalters/repo](https://github.com/rjwalters/repo)) owns the CHANGELOG/version-drift gates and GitHub Release flow, and honors `scripts/version.sh`. `/loom:bump` is the lightweight quick-bump counterpart.
- **Do not overwrite an existing `scripts/version.sh` without confirmation.** The operator may have customized it; offer a diff first.
- **`scripts/version.sh` is the source of truth on subsequent runs.** Phase 1's short-circuit step ensures the skill defers to the generated script after the first run.
- **Multiple shapes can coexist.** An npm+cargo monorepo (Loom's own shape) needs updates across `package.json`, `Cargo.toml`, `Cargo.lock`, and possibly `CLAUDE.md`. Emit writers for every detected shape.
- **The seven detection sources are**: `package.json`, `*/package.json` (workspace), `Cargo.toml` (+ workspace members + `Cargo.lock`), `pyproject.toml` (`[project]` or `[tool.poetry]`), `setup.py`/`setup.cfg` (legacy Python), top-level shell script with `VERSION="X.Y.Z"`, and `CLAUDE.md`/`README.md` with `**Version**: X.Y.Z`.
- **Branch protection**: if the operator's repo enforces PR-only merges to `main`, a direct push of the bump commit will fail. In that case, push the bump commit to a feature branch and open a PR — the tag can be created after the PR merges.
