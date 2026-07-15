---
name: "audit"
description: "Full repo health scan — READMEs, orphans, links, gitignore, branches"
domain: repo
type: command
user-invocable: true
---

# /repo:audit — Full Health Sweep

Run a full health sweep of the repository (or a specific subtree). Reports
findings grouped by category and severity. Does not make changes — presents
findings for discussion.

## Usage

```
/repo:audit                    # Full repo
/repo:audit packages/core      # Scope to one subtree
```

## What It Checks

Run each of the following checks and compile results into a single report:

### 1. README Accuracy (see [[readme]])
- Browsable directory levels that lack a README (top-level and significant
  subdirs — so GitHub renders docs at each level someone navigates to)
- READMEs whose file/folder listings don't match actual contents
- Stale "gitignored" or "TODO" annotations that are no longer true

### 2. Orphaned Files (see [[orphans]])
- Scripts not referenced by any other file, Makefile, or CI config
- Data files (.yaml, .json, .csv) not imported or referenced anywhere
- Generated outputs (PDFs, binaries) without a corresponding source

### 3. Broken Links (see [[links]])
- Markdown links (`[text](path)`) pointing to nonexistent files
- CLAUDE.md paths that don't resolve
- Skill/command cross-references to missing files

### 4. Gitignore Issues (see [[gitignore]])
- Files that are ignored but probably shouldn't be
- Build artifacts that aren't ignored but should be
- Redundant or stale gitignore rules

### 5. Branch & Worktree Hygiene (see [[branches]])
- Local branches whose PRs are merged
- Orphaned or stale worktrees

## Output Format

Present findings as a table grouped by category:

```
## Repo Audit — packages/core

### README Issues (2 findings)
| Severity | Path | Issue |
|----------|------|-------|
| warn | docs/analysis/ | No README, 8 files |
| info | README.md | Lists `sim/` but dir doesn't exist |

### Orphaned Files (1 finding)
...

### Summary
- 2 README issues (0 critical, 1 warn, 1 info)
- 1 orphan (0 critical, 0 warn, 1 info)
- 0 broken links
- 0 gitignore issues
- 3 stale branches
```

After presenting, ask: "Want me to fix any of these?"

## Severity Levels

- **critical**: Something is actively misleading or broken (dead link in
  CLAUDE.md, gitignore rule hiding important files)
- **warn**: Should be fixed but isn't causing immediate harm (stale README,
  orphaned script)
- **info**: Nice to fix, low priority (missing README in a small directory)
