# Release Manager

You are preparing a release of the `kicad-tools` package from the {{workspace}} repository.

## Overview

This skill guides a careful, interactive release process. Every release must:
1. Analyze what changed since the last release
2. Help the user decide the correct semver bump
3. Draft and refine the CHANGELOG entry
4. Update version references
5. Commit, tag, and (with confirmation) push

**Do not rush. Each phase requires user confirmation before proceeding.**

## Phase 1: Gather Changes

Run these commands to understand what's changed:

```
# Find the last release tag
git tag --sort=-v:refname | head -1

# List all commits since that tag
git log <last-tag>..HEAD --oneline

# Show the full diff stats
git diff <last-tag>..HEAD --stat

# Count by commit type (feat/fix/etc)
git log <last-tag>..HEAD --oneline --format="%s"
```

Present the user with:
- **Last release**: tag name, date, and version
- **Commits since release**: count and full list
- **Change summary**: categorized by conventional commit prefix (feat, fix, refactor, docs, test, chore, etc.)
- **Files changed**: high-level summary (which subsystems were touched)

If there are zero commits since the last tag, stop and tell the user there's nothing to release.

## Phase 2: Semver Decision

Present a semver analysis to help the user choose the right version bump. Reference https://semver.org:

### Breaking Changes (would warrant MAJOR bump)
Scan for any of these in the diff since last tag:
- Removed or renamed public API functions/classes/methods
- Changed function signatures (parameter order, required params added)
- Changed return types or error behavior
- Removed CLI commands or changed their flags/behavior
- Removed or renamed MCP tools
- Changed file format output

### New Capabilities (would warrant MINOR bump)
- New CLI commands or subcommands
- New MCP tools
- New public API classes/functions
- New optional dependencies or extras
- New configuration options

### Bug Fixes / Internal (would warrant PATCH bump)
- Bug fixes that don't change API
- Performance improvements
- Test improvements
- Internal refactoring
- Documentation updates

Present your analysis like this:

```
## Semver Analysis

Current version: X.Y.Z

### Breaking changes found:
- [list or "None detected"]

### New capabilities:
- [list]

### Fixes / internal:
- [list]

### Recommendation: X.Y.Z -> A.B.C (MINOR/PATCH/MAJOR)
Rationale: [brief explanation]
```

**Ask the user to confirm or override the version number.** Do not proceed until they confirm.

## Phase 3: Draft CHANGELOG

Draft a CHANGELOG entry following the existing format in `CHANGELOG.md`. Study the existing entries to match style and structure.

Key formatting rules:
- Use `## [X.Y.Z] - YYYY-MM-DD` header with today's date
- Group changes under `### Added`, `### Changed`, `### Fixed`, `### Removed`, `### Deprecated` as appropriate
- Use sub-headers (#### Section Name) to group related changes by subsystem when there are many changes
- Reference PR/issue numbers with `(#NNN)` format
- Use bold for the item title, followed by description
- Keep descriptions concise but informative — a user should understand what changed and why
- Omit empty sections (don't include `### Changed` if nothing changed)
- Add an `[Unreleased]` section above the new entry if one doesn't exist

Present the draft to the user and ask for revisions. Iterate until they approve.

## Phase 4: Apply Changes

Once the user approves the CHANGELOG draft:

1. **Update `pyproject.toml`**: Change `version = "X.Y.Z"` to the new version
2. **Update `CHANGELOG.md`**: Insert the new entry below the `## [Unreleased]` header (add one if missing)
3. **Verify**: Read back both files to confirm the changes look correct

Show the user the exact changes (a diff summary) and ask for final confirmation.

## Phase 5: Commit and Tag

After final confirmation:

1. **Stage** the two changed files:
   ```
   git add pyproject.toml CHANGELOG.md
   ```

2. **Commit** with a conventional message:
   ```
   git commit -m "chore: release v{VERSION}"
   ```

3. **Tag** the commit:
   ```
   git tag v{VERSION}
   ```

4. **Show the result**:
   ```
   git log --oneline -3
   git tag --sort=-v:refname | head -3
   ```

5. **Ask about pushing**: Tell the user what push commands are needed and ask if they want to proceed:
   ```
   git push origin main
   git push origin v{VERSION}
   ```
   Explain that pushing the tag will trigger the PyPI publish workflow (`.github/workflows/publish.yml`).

**Do not push without explicit user confirmation.**

## Phase 6: Post-Release Summary

After everything is done, present a summary:

```
## Release Complete

- Version: vX.Y.Z
- Commit: <sha>
- Tag: vX.Y.Z
- PyPI publish: [will trigger on push / user declined push]
- CHANGELOG: updated with N items across M categories
```

## Important Notes

- **Single source of truth**: Version lives in `pyproject.toml` line 7. The `__init__.py` reads it via `importlib.metadata` automatically — no code change needed there.
- **No pre-release versions**: This project uses simple semver (X.Y.Z), not pre-release suffixes.
- **Conventional commits**: This project uses conventional commit prefixes (`feat:`, `fix:`, `chore:`, etc.) — use them to categorize changes.
- **PyPI workflow**: Pushing a `v*` tag triggers `.github/workflows/publish.yml` which builds and publishes to PyPI automatically.
