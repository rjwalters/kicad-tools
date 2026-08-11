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

**Strip code before scanning.** Remove fenced blocks and inline code spans from
the text first — a `[text](path)` inside backticks is a description of a link,
not a link:

```python
text = re.sub(r'```.*?```', '', text, flags=re.S)   # fenced blocks
text = re.sub(r'`[^`]*`', '', text)                  # inline spans
```

Without this the checker flags the sentences in this very file, and in
[[audit]], that explain what it looks for. A checker that reports its own
documentation as broken is not a checker anyone keeps running.

Skip:
- External URLs (http://, https://)
- Anchor-only links (#section)
- Image URLs from external services

**Resolve against two bases, and only report a link that fails both.** A
relative path can legitimately be written against either the file's own
directory or the repo root, and both conventions are in active use:

1. the directory of the file containing the link
2. the repo root

Report the link only when the target is missing under **both**. State which
base resolved it when the answer is not the file's own directory, so a reader
can tell a convention from a coincidence. Silently assuming the file's own
directory is what produced 28 wrong findings in a single run against
`.loom/CLAUDE.md`, whose links are root-relative and all correct.

**Install-template trees get a third base — their installed destination.** Some
repos ship a tree whose whole purpose is to be copied somewhere else: an
installer's `defaults/`, a cookiecutter skeleton, a `contrib/` dotfile tree.
Those files' links are written to resolve **where the file lands**, so resolving
them in place is wrong by construction and every link in the tree reports
broken. This third base applies only when the repo declares the mapping — see
**Install-template trees** below for the declaration file, the resolution order,
and what the report must say about it.

### 2. CLAUDE.md File References
CLAUDE.md files typically list key file paths (reference tables, "see X"
pointers). Verify every path mentioned resolves. This is **critical**
severity — these are the primary navigation paths for agents.

Critical severity is exactly why the two-base rule above matters most here. **A
CLAUDE.md is loaded into an agent's context and its paths are read from the repo
root**, not from wherever the file happens to sit, so root-relative is the
correct convention in one — not a defect. Resolving a CLAUDE.md link only
against its own directory turns the highest-severity class in this checker into
the one most likely to be wrong.

A CLAUDE.md inside an **install-template tree** is the sharpest form of this: it
is simultaneously the highest-severity class and a file whose paths are written
for a repo root that is not this one. Two bases are not enough for it — see
**Install-template trees** below.

### 3. Skill/Command Cross-References
If the repo has `.claude/skills/` and `.claude/commands/`:
- Every `[[wikilink]]` in a SKILL.md has a corresponding command `.md` file
  in the same domain
- If a `.claude/skill-graph.json` exists: every node references a file that
  exists, and every edge connects two valid nodes

### 4. Nested CLAUDE.md References
Subdirectory CLAUDE.md files often list key files relative to their own
directory. Verify those paths resolve — against that directory **and** the repo
root, per the two-base rule, plus the install-mapping base when the file sits
inside a declared template tree. Both conventions appear in nested files, and
which one a given file uses is not knowable from its location.

### 5. Vendored and installer-managed files
A file under a tool's dot-directory (`.loom/CLAUDE.md`, `.anvil/CLAUDE.md`, and
anything else written by an installer) is **reported but never edited in
place**, even when the fix is unambiguous and `--ask` is not in play. The next
install overwrites the edit, so a fix there is silently temporary and the
finding returns.

Report these in their own group, name the upstream repo that owns the file, and
say the fix belongs there. Same reasoning as [[scrub]]'s handling of findings
inside vendored trees.

## Install-template trees

A template tree is the mirror image of §5's vendored tree: not a copy this repo
received, but the original this repo *sends*. Its links are addressed to the
destination, and nothing about a directory's name says so — only the repo knows.

### The declaration

Read the mapping from `.repo/link-roots.json` (same `.repo/` convention as
[[release]]'s policy file and [[scrub]]'s allowlist). Keys are template-tree
paths relative to this repo's root; values are the destination prefix relative
to the installed repo's root, where `""` means the destination root:

```json
{
  "defaults/.loom": "",
  "defaults/docs": ".loom/docs",
  "defaults/.claude/commands/loom": ".claude/commands/loom"
}
```

**Absent the file — or given an empty object — nothing in this section runs and
resolution is exactly the two-base rule above.** No tree is ever treated as a
template by inference from its name (`defaults/`, `template/`, `skeleton/`), and
no finding is suppressed by default. A guessed mapping hides real broken links,
which is the one failure mode worse than the noise this section exists to
remove.

### Resolution order

For a link with target `P` in a file `F`, try the bases in order and stop at the
first that resolves:

1. **In place** — `dirname(F)/P`.
2. **Repo root** — `P`.
3. **Install mapping** — only when `F` sits under a declared template tree.

The third base takes two steps, because the destination layout does not exist in
this repo:

- **Forward.** Find the declared tree `T` that is the longest path-prefix of `F`,
  with destination `D`. The file's installed path is `D/relpath(F, T)`. Resolve
  `P` against that installed path's directory (and against the destination root)
  to get the **installed target** `Q` — the path the link points at *after*
  installation.
- **Reverse.** `Q` names a location in the installed repo, so map it back to
  find what would be installed there. For each declared `T' → D'` whose
  destination `D'` is a path-prefix of `Q`, the candidate source is
  `T'/relpath(Q, D')`; when `D'` *equals* `Q` — a link that points at the
  destination directory itself — the candidate is `T'`. Try candidates
  **longest `D'` first**, and try literal `Q` as well (some destinations also
  exist here, as installed copies). The link resolves if **any** candidate
  exists on disk: a candidate that is absent is skipped and the search
  continues, and only an exhausted candidate list is a finding.

Worked example — the report that produced this rule. `defaults/.loom/CLAUDE.md`
links to `.loom/docs/troubleshooting.md`. In place that is
`defaults/.loom/.loom/docs/troubleshooting.md`, which is missing; from the repo
root it is `.loom/docs/troubleshooting.md`, missing in a repo that has not
installed itself. Forward: `defaults/.loom → ""`, so the file installs to
`CLAUDE.md` at the destination root and the installed target is
`.loom/docs/troubleshooting.md`. Reverse: the longest matching destination is
`.loom/docs → defaults/docs`, giving `defaults/docs/troubleshooting.md` — which
exists. Resolved via install mapping; not a finding. All 22 findings in that run
were this shape, and all 22 were wrong.

Longest-first ordering in the reverse step is an **attribution** rule, not a
correctness one. Because the step resolves on *any* existing candidate, the
ordering cannot change a resolved-vs-`MISSING` verdict — an absent candidate is
skipped, not fatal. What it changes is *which* declared tree is named as the
source that satisfies the link — the disclosure below. `""` is a path-prefix of
*every* path, so wherever `defaults/.loom/.loom/docs/…` does happen to exist a
shortest-first search credits `defaults/.loom` for a target that
`.loom/docs → defaults/docs` is what actually installs. Longest `D'` is the most
specific mapping, and the most specific mapping is the one that owns the
installed path, so it is the one to report.

The verdict-changing longest-prefix rule is the **forward** step's. There
exactly one `T` is chosen, and that choice fixes the installed path and
therefore `Q` itself: taking a shorter prefix where a longer declared tree also
contains `F` resolves the link against the wrong destination and can turn a
healthy link into a finding. An asymmetric mapping — where a tree installs
*above* one of its own siblings — is the normal case, not the corner case, so
nested declarations and multi-candidate reverse steps both see real traffic.

### It adds a base; it never deletes a finding

A link inside a template tree that resolves under **none** of the three bases is
still reported, at the same severity it would carry anywhere else. Name the
bases that were tried, so a reader can tell "genuinely missing" from "mapping is
wrong":

```
| 88 | .loom/docs/gone.md | MISSING (in place, repo root, and via defaults/.loom -> <dest root>) |
```

### Report what the mapping did

A mapping is repo-authored configuration, so it can be wrong — and a wrong
mapping fails by *hiding* errors, which is invisible unless the report shows its
work. Any run that loads `.repo/link-roots.json` prints a mapping table
alongside the findings, whether or not there were findings:

```
### Install mappings (.repo/link-roots.json)
| Template tree | Installs to | Links resolved |
|---|---|---|
| defaults/.loom | <dest root> | 19 |
| defaults/docs | .loom/docs | 3 |
| defaults/.claude/commands/loom | .claude/commands/loom | 0  <- declared, never used |
```

Two rows of that table are findings in their own right:

- A declared tree that is **not a directory** in this repo — a stale or
  misspelled key. It can never resolve anything; report it.
- A declared tree that resolved **0** links while its files do contain relative
  links — either the mapping is wrong or the tree is not a template tree. Report
  it as a question, don't assume which.

Per-link disclosure follows the same rule as the two-base case: name the base
whenever it is not the file's own directory. A mapping-resolved link reads
**resolved via install mapping (`defaults/.loom` -> `<dest root>`,
source `defaults/docs/troubleshooting.md`)**, never a bare "ok" — the whole
point is that a mapping-resolved link and an in-place one are distinguishable
at a glance. The mapping named is the **forward** one (the tree the linking file
sits in, which is also the tree the table's "Links resolved" column counts
against); the **source** is the reverse candidate that was found, chosen by the
longest-`D'` attribution rule above. Print both: they are frequently different
trees, and a wrong mapping is easiest to spot in the pair.

### Fixing a link inside a template tree

Template files are this repo's source of truth, so unlike §5's vendored files
they are editable. But the corrected path must be written in **destination**
coordinates, exactly like the link it replaces: compute the fix against the
installed layout, then write it as the destination sees it. A fix written in
source coordinates resolves here and breaks in every repo the template installs
into — a silent regression this command would then report as healthy. When the
two coordinate systems disagree about what the fix is, report it instead of
editing.

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

### Precision is itself a finding

When a run produces many findings and few actionable ones, **say so on its own
line** rather than printing the list and moving on:

```
30 findings, 0 actionable — 2 were code spans, 28 resolve from the repo root.
Check the resolution rules before acting on this report.
```

A high false-positive rate is a defect in the checker, not a property of the
repo, and it is the more useful signal of the two. The cost of a noisy run is
not the wasted minute — it is that a check returning 100% noise on a healthy
repo teaches people to skim past its output, which is expensive the first time
it is right.

### Verify after write

Fixing a link is not proof the fix survived. A concurrent writer — another
agent working in the same clone, a background `git stash` or `git checkout --`,
a pre-commit hook, a Loom sweep quarantining the primary clone's working tree —
can revert a file between the moment you fix it and the moment you report it,
leaving this command claiming a fix that is no longer on disk.

So immediately after applying each fix, and **before counting it as applied**,
re-read the changed region of the file and confirm your specific edit is
present. `git diff -- <path>` / `git status --porcelain -- <path>` is a cheap
first pass, but only proves the path differs from HEAD — it cannot distinguish
your edit from someone else's, so it must not be the sole check when the file
may carry other uncommitted changes.

This check is **unconditional** — run it whether or not you have any reason to
suspect a concurrent writer. Detecting a daemon first would be racy (one can
start right after the check), and in a repo with no concurrent writer the check
always finds the edit still applied, so nothing about the reported output
changes.

If a fix is gone on re-check, report it on its own line as **reverted after
apply — needs re-run**. Do not silently re-apply it, and do not count it in the
fixed total — that total must only ever include edits confirmed still on disk.
