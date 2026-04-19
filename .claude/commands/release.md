# Release Manager

You are preparing a release of **Loom** from the {{workspace}} repository.

## Overview

This skill guides a careful, interactive release process. Every release must:
1. Verify CI is green on main
2. Analyze what changed since the last release
3. Help the user decide the correct semver bump
4. Draft and refine the CHANGELOG entry
5. Update version across all 7 version-bearing files
6. Commit, tag, and (with confirmation) push
7. Create a GitHub Release to trigger the build workflow

**Do not rush. Each phase requires user confirmation before proceeding.**

## Phase 1: Pre-flight Checks

Before starting, verify the release is safe to cut:

```bash
# Check CI status on main
gh run list --branch main --limit 5 --json name,conclusion --jq '.[] | "\(.name): \(.conclusion)"'

# Check for open PRs that might need to land first
gh pr list --state open --json number,title --jq '.[] | "#\(.number) \(.title)"'

# Check for uncommitted changes
git status
```

Present findings to the user. If CI is failing, stop and fix first. If there are open PRs, ask if they should land before the release.

## Phase 2: Gather Changes

```bash
# Find the last release tag
git tag --sort=-v:refname | head -1

# Show current version
./scripts/version.sh

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

Present a semver analysis. Reference https://semver.org:

### Breaking Changes (MAJOR bump)
Scan for:
- Removed or renamed public API functions/types
- Changed ForgeClient protocol methods
- Changed CLI command flags/behavior
- Changed MCP tool interfaces
- Removed or renamed Tauri commands
- Changed config file format

### New Capabilities (MINOR bump)
- New forge support (e.g., Gitea, GitLab)
- New CLI commands (`loom-forge`, `loom-auto-merge`)
- New agent roles or orchestration features
- New MCP tools
- New configuration options

### Bug Fixes / Internal (PATCH bump)
- Bug fixes that don't change API
- Performance improvements
- Internal refactoring
- Documentation updates
- Dependency bumps

Present your recommendation and **ask the user to confirm or override**. Do not proceed until confirmed.

## Phase 4: Draft CHANGELOG

Draft a CHANGELOG entry following the existing format in `CHANGELOG.md`. Study existing entries to match style.

Key formatting rules:
- Use `## [X.Y.Z] - YYYY-MM-DD` header with today's date
- Start with a `### Summary` paragraph describing the release theme
- Group changes under `### Added`, `### Changed`, `### Fixed`, `### Removed`, `### Renamed` as appropriate
- Reference issue numbers with `(#NNN)` format
- Keep descriptions concise but informative
- Omit empty sections

Present the draft and ask for revisions. Iterate until approved.

## Phase 5: Apply Changes

Once the user approves:

1. **Update CHANGELOG.md**: Insert the new entry below `## [Unreleased]`
2. **Bump version**: Run `./scripts/version.sh bump <level> --tag`
   - This updates all 7 files: `package.json`, `mcp-loom/package.json`, `src-tauri/tauri.conf.json`, 3 `Cargo.toml` files, `CLAUDE.md`
   - Plus `Cargo.lock`
   - Creates the commit and tag automatically
3. **Verify**: `./scripts/version.sh check`

Note: The version bump script creates the commit, so commit the CHANGELOG first:
```bash
git add CHANGELOG.md
git commit -m "docs: add X.Y.Z changelog entry"
./scripts/version.sh bump <level> --tag
# Move tag to include both commits
git tag -f vX.Y.Z
```

Show the user the result and ask for final confirmation.

## Phase 6: Push and Release

After final confirmation:

1. **Push commits and tag**:
   ```bash
   git push origin main --tags
   ```

2. **Create GitHub Release** (this triggers the build workflow):
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file - <<< "$(changelog excerpt)"
   ```
   Use the CHANGELOG entry as the release notes.

3. **Verify build triggered**:
   ```bash
   gh run list --workflow=release.yml --limit 1
   ```

**Do not push or create the release without explicit user confirmation.**

## Phase 7: Post-Release Summary

Present a summary:

```
## Release Complete

- Version: vX.Y.Z
- Commit: <sha>
- Tag: vX.Y.Z
- GitHub Release: created
- Build workflow: [triggered / status]
- CHANGELOG: updated with N items
- Version files: 7 files + Cargo.lock updated
```

## Important Notes

- **Version script**: `scripts/version.sh` is the single source of truth for version management. Never manually edit version numbers.
- **7 version-bearing files**: `package.json`, `mcp-loom/package.json`, `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, `loom-daemon/Cargo.toml`, `loom-api/Cargo.toml`, `CLAUDE.md`
- **Release workflow trigger**: The build workflow (`.github/workflows/release.yml`) triggers on GitHub Release creation (`release: types: [created]`), NOT on tag push. You must create a GitHub Release via `gh release create`.
- **Conventional commits**: This project uses conventional commit prefixes (`feat:`, `fix:`, `chore:`, etc.).
- **Build output**: The release workflow builds macOS DMGs (Apple Silicon + Intel) and attaches them to the GitHub Release.
- **CLAUDE.md update**: The version script updates the `**Loom Version**` line in CLAUDE.md automatically.
- **Branch protection**: Direct pushes to main will show a ruleset bypass warning — this is expected for release commits.
