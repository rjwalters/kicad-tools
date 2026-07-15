---
name: "orphans"
description: "Find unreferenced files — dead scripts, stale data, outputs without sources"
domain: repo
type: command
user-invocable: true
---

# /repo:orphans — Orphan Finder

Find files that appear to have no consumers — scripts nobody calls, data files
nothing reads, build outputs without sources.

## Usage

```
/repo:orphans                    # Full repo
/repo:orphans tools/             # Scope to subtree
/repo:orphans --type scripts     # Only check scripts
```

## What It Checks

### 1. Orphaned Scripts
Python/shell scripts in `scripts/`, `tools/`, or `bin/` directories that are
not referenced by:
- Any other script, Makefile, package.json script, or CI config
- Any README or documentation
- Any skill/command file

Search for the script's basename across the repo. If zero references exist
outside the file itself, it's likely orphaned.

### 2. Orphaned Data Files
YAML, JSON, CSV files that are not:
- Imported or read by any code
- Referenced in any README or documentation
- A recognized tool config (`.eslintrc`, `pyproject.toml`, CI workflows, etc. —
  many configs are consumed by convention, not by reference)

### 3. Source/Output Mismatches
- Generated outputs (`.pdf`, `.html`, compiled binaries) without a
  corresponding source (`.tex`, `.md`, `.py`, …)
- Sources whose expected output is missing (may indicate a broken build)
- `.example` / `.sample` files where the real file now exists

### 4. Empty Directories
Directories that contain no files (only subdirs or nothing at all).

## Interaction

Present findings grouped by type. For each orphan, show:
- The file path and size
- What was searched for (e.g., "grepped for `extract.py` — 0 references")
- Suggested action: delete, move, or add a reference

Deleting an orphan is destructive and a judgment call — an "orphan" is often a
legitimate standalone file the user keeps deliberately — so unlike the safe-fix
commands this one never auto-acts. It reports and waits for your decision
(delete, move, or add a reference), with no `--ask` needed: report-and-wait
is the only mode.
