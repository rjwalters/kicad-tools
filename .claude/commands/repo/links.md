---
name: "links"
description: "Validate internal cross-references — markdown links, CLAUDE.md paths, skill graph edges"
domain: repo
type: command
user-invocable: true
---

# /repo:links — Link Checker

Validate that internal cross-references across the repo actually resolve.
Catches broken links from reorganization, renames, and deletions.

This is the cross-reference layer of [[docs]]. Use it directly when that's all
you want to check; use [[docs]] for the full documentation sweep.

## Usage

```
/repo:links                    # Full repo — fix unambiguous links, report as you go
/repo:links CLAUDE.md          # Check one file
/repo:links .claude/           # Check skill/command files
/repo:links --ask              # Review findings and confirm before fixing
```

## What It Checks

### 1. Markdown Links
Scan all `.md` files for `[text](path)` links where `path` is a relative file
path (not a URL). Verify the target exists on disk.

Skip:
- External URLs (http://, https://)
- Anchor-only links (#section)
- Image URLs from external services

### 2. CLAUDE.md File References
CLAUDE.md files typically list key file paths (reference tables, "see X"
pointers). Verify every path mentioned resolves. This is **critical**
severity — these are the primary navigation paths for agents.

### 3. Skill/Command Cross-References
If the repo has `.claude/skills/` and `.claude/commands/`:
- Every `[[wikilink]]` in a SKILL.md has a corresponding command `.md` file
  in the same domain
- If a `.claude/skill-graph.json` exists: every node references a file that
  exists, and every edge connects two valid nodes

### 4. Nested CLAUDE.md References
Subdirectory CLAUDE.md files often list key files relative to their own
directory. Verify those paths resolve relative to that directory.

## Interaction

Group findings by source file:

```
## CLAUDE.md — 2 broken links

| Line | Target | Status |
|------|--------|--------|
| 42 | docs/setup.md | MISSING (removed?) |
| 87 | legacy/MIGRATION.md | MISSING (renamed?) |

## packages/core/CLAUDE.md — 1 broken link
...
```

For each broken link, find the most likely correct target (fuzzy match on
filename). When there's a single confident match, fix the link and report it;
when the match is ambiguous or no target exists, report it for a human call.
Under `--ask`, propose every fix and confirm before editing.
