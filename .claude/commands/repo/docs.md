---
name: "docs"
description: "Check documentation against reality — content accuracy, README structure, and cross-references"
domain: repo
type: command
user-invocable: true
---

# /repo:docs — Documentation Check

The canonical documentation-health command. Verify that the repo's docs still
describe the repo that exists — not the one that existed three refactors ago.

This is the single entry point for docs work. It covers three layers:

1. **Content accuracy** — does the prose still match how things actually work?
   (unique to this command)
2. **README structure** — do file trees and directory listings match disk?
   (delegates to [[readme]])
3. **Cross-references** — do internal links and paths still resolve?
   (delegates to [[links]])

`/repo:readme` and `/repo:links` remain callable on their own when you only
want that one layer. Reach for `/repo:docs` when you want the whole picture.

## Usage

```
/repo:docs                     # Full repo — apply safe fixes, report as you go
/repo:docs docs/               # Scope to one subtree
/repo:docs README.md           # Check a single doc
/repo:docs --ask               # Review findings and confirm before applying
```

The optional path argument scopes every layer the same way, exactly as
[[readme]] and [[links]] scope on their own.

## What It Checks

### 1. Content Accuracy

This is the semantic layer that structural checks miss — prose that parses
fine and links that resolve fine, but describes behavior that has since
changed. Read the docs against the actual repo state and flag drift:

- **Feature / command tables** that list capabilities the repo no longer has,
  or omit ones it gained. For a repo with `.claude/commands/`, cross-check
  every documented command against the files that actually exist, in both
  directions.
- **CHANGELOG currency** — recent commits (features, breaking changes, renames)
  that landed with no corresponding CHANGELOG entry. Compare the top entry's
  date/version against `git log` since then.
- **Code examples & snippets** in docs that reference symbols, flags, file
  paths, or commands that no longer exist. Verify each referenced identifier
  resolves in the current tree.
- **Described workflows** — step-by-step instructions (install, build, usage)
  that name scripts, targets, or flags. Confirm they still exist and still take
  the arguments shown.
- **Version / count claims** — "supports 12 commands", "requires Node 18",
  hardcoded numbers that drift as the repo changes.

Content findings are judgment calls — when something looks stale but you can't
confirm it from the repo, flag it as a question rather than asserting it's
wrong.

### 2. README Structure (see [[readme]])

Run the full [[readme]] check: ASCII file-tree accuracy, directories missing a
README, and stale "gitignored"/"TODO" annotations. Fold its findings into this
report rather than emitting a separate one.

### 3. Cross-References (see [[links]])

Run the full [[links]] check: markdown links, CLAUDE.md path references,
skill/command wikilinks, and nested CLAUDE.md paths. Fold its findings in the
same way. Broken CLAUDE.md paths remain **critical** — they're the primary
navigation paths for agents.

## Output Format

One consolidated report, grouped by layer so it's clear which are mechanical
(structure, links) and which are judgment calls (content):

```
## Docs Check — docs/

### Content Accuracy (2 findings)
| Severity | Location | Issue |
|----------|----------|-------|
| warn | README.md:14 | Skills table omits /repo:docs (exists on disk) |
| info | CHANGELOG.md | No entry since v0.3.0; 4 feature commits since |

### README Structure (1 finding — via readme)
| warn | docs/analysis/ | No README, 8 files |

### Cross-References (1 finding — via links)
| critical | CLAUDE.md:42 | Link to docs/setup.md — MISSING |

### Summary
- 2 content (0 critical, 1 warn, 1 info)
- 1 structure (0 critical, 1 warn)
- 1 cross-reference (1 critical)
```

## Interaction

By default, apply the safe, reversible fixes as you find them — correcting
stale listings, tables, and factual drift — and report each change (all
git-reversible). Run with `--ask` to review first: for each finding, offer
to **fix it**, **skip** it, or **show** the relevant section before deciding.

Content findings are judgment calls; when you can't confirm something is wrong
from the repo, raise it as a question rather than editing on a guess — even in
the default apply mode.

Never rewrite prose wholesale to "improve" it — the job is accuracy, not a
style pass. Fix the factual drift and leave the voice alone.

## Principles

Same as every hygiene command: **apply safe fixes, gate destructive ones**
(doc edits are reversible, so they apply by default; `--ask` to confirm
first); **general by design** (no assumptions about doc layout — read what's
there); **don't be noisy** (a slightly informal sentence isn't a finding; a
command that no longer exists is).
