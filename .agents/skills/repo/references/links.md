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
text = re.sub(r"```.*?```", "", text, flags=re.S)  # fenced blocks
text = re.sub(r"`[^`]*`", "", text)  # inline spans
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

**Both bases resolve only candidates that stay inside the repo toplevel.**
Normalize `dirname(F)/P` (base 1) and `P` (base 2) lexically — collapse `.`
and `..` path components without touching the filesystem, never
`realpath`/`readlink -f` — and check whether the normalized result still
begins with `..`. A candidate that does is not tried as base 1 or base 2 at
all, even when a file happens to exist at that escaping path on disk: it is
handed to the sibling-repo base (see **Sibling-repo relative links** below)
instead. An escaping candidate that resolves anyway is a citation into
another repo silently reported as if it were in place — undisclosed proof it
crossed a repo boundary, exactly the gap this restriction closes. Detection
must stay lexical: resolving symlinks (`realpath`/`readlink -f`) would deport
a symlinked directory that stays inside the repo to the sibling base by
mistake, since a symlink target can differ from its lexical path without the
link itself ever leaving the repo.

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

Bases 1 and 2 apply only to a candidate that stays inside the repo toplevel —
the same lexical escape-detection rule from the two-base section above. A
candidate that still begins with `..` after normalization skips both bases
and falls through to base 3 (if `F` sits under a declared template tree) and
then to the sibling-repo base, exactly as it does outside a template tree.

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

## Sibling-repo relative links

A relative link that resolves **outside the repo root** is not automatically
broken. In a multi-repo workspace where the documentation discipline is "cite,
don't restate", docs cite files in checked-out sibling repos this way — e.g. a
strategy doc linking `[session note](../../notes/sessions/2026-07-30.md)` to a
working-session note in a sibling checkout. These citations are the
load-bearing provenance trail behind a decision, so this repo treats them as a
fourth resolution base rather than silently skipping them or flagging every one
as broken.

### The declaration

Read the mapping from `.repo/link-siblings.json` — a **different** file from
[[links]]'s own `link-roots.json` (that one maps a tree inside this repo to a
destination inside this repo; this one maps a sibling name to a location
outside this repo), same `.repo/` convention as [[release]]'s policy file and
[[scrub]]'s allowlist, and the same flat-map **shape** as `link-roots.json`
itself:

```json
{
  "notes": "../notes",
  "kicad-tools": "../kicad-tools",
  "anvil": "../anvil"
}
```

Keys are sibling names, used in disclosures. Values are each sibling's
**location**, relative to this repo's root — almost always `"../<name>"`, the
directory that contains this repo's checkout and its siblings. A location need
not be a direct child of the workspace parent: `"archive": "../notes/archive"`
declares a sibling nested inside another sibling's own checkout, which the
earlier `{"parent": "..", "siblings": [names]}` shape could not express at
all — every declared sibling there had to be `parent`'s direct child. A
relative link whose normalized target does not fall under any declared
location is out of scope for this check entirely — not reported as broken,
not reported as unverifiable, simply not a workspace citation this repo
recognizes. **Absent the file — or given an empty object — nothing in this
section runs and out-of-repo links are handled exactly as they are today**,
under the existing two-base rule alone. No sibling is ever inferred from a directory
name or shape, same discipline as `.repo/link-roots.json`'s "no tree is ever
treated as a template by inference from its name".

### Resolution order

Bases 1-2 (and the install-mapping base 3, where declared) resolve only
candidates that stay inside the repo toplevel — see the escape-detection rule
under **What It Checks** above. A link target `P` in file `F` whose base-1
candidate escapes (normalizes to a path that still begins with `..`) is
checked against this **fourth base** before being reported. An in-repo
candidate that simply fails every earlier base — genuinely missing, never
escaping — is a normal `MISSING` finding and never reaches this section; that
is what keeps this base from re-litigating ordinary broken links.

1. Normalize `dirname(F)/P` lexically to get `Q` — the same escaping
   candidate bases 1-2 declined to resolve. `Q` is recomputed here rather than
   reused, since whether a given link reaches this base at all depends on
   that same escape check.
2. Match `Q` against the declared sibling **locations**, each normalized the
   same lexical way, and pick the sibling whose location is the **longest**
   matching path-prefix of `Q` — the most specific declared sibling wins, the
   same "longest wins" discipline as `link-roots.json`'s reverse step (see
   **Install-template trees** above). There it is only an attribution rule;
   here it changes the verdict: crediting a shorter, present sibling for a
   target that actually belongs to a longer, absent nested sibling reports
   the nested sibling's absence as a false `MISSING` in this repo instead of
   the correct `unverifiable`. If no declared location matches, this base
   does not apply — report as before.
3. If the matched sibling's declared **location exists on disk** as a
   directory, validate `Q` exactly like an internal link: exists -> resolved
   via sibling repo `<name>`; missing -> a normal broken-link finding, not a
   false positive — the sibling *is* here, so a missing target means the
   citation is genuinely stale.
4. If the matched sibling's declared **location does not exist on disk**, the
   link is unverifiable, not broken — this machine cannot tell whether the
   target exists. Report it as **`sibling repo not present — unverifiable`**,
   a distinct status from `MISSING`. This is the same "different failure modes
   get different rows" discipline [[update-tools]] uses for `source repo
   missing` vs. `sidecar missing` (see [[update-tools]] step 3) — folding
   "sibling absent" into `MISSING` would turn every machine without every
   sibling checked out into a wall of permanent false positives.

### Report

Sibling-repo links get their own status values, distinguishable at a glance
from `MISSING`:

```
| Line | Target | Status |
|------|--------|--------|
| 12 | ../../notes/sessions/2026-07-30.md | resolved via sibling repo `notes` |
| 45 | ../../notes/sessions/2026-06-01.md | MISSING (sibling `notes` present, file not found — renamed?) |
| 61 | ../../kicad-tools/docs/setup.md | sibling repo not present — unverifiable (declared, no checkout at `../kicad-tools`) |
```

Never fold the third row into the first two — a machine without every sibling
checked out would otherwise show permanent false-positive noise on every run,
the exact "precision is itself a finding" failure this checker already guards
against for install-mapping.

### It adds a base; it never deletes a finding

Same rule as install-template trees: a link that resolves under none of the
bases tried is still reported, at the same severity it would carry anywhere
else. A declared sibling that resolved 0 links, or whose declared location is
present on disk but never referenced, is reported the same way a
zero-resolution `link-roots.json` entry is — as a question, not silently
ignored.

### Worked example

A strategy doc at `workspace/repo-a/docs/strategy.md` links
`[session note](../../notes/sessions/2026-07-30.md)`. Normalized against the
file's own directory that is `workspace/notes/sessions/2026-07-30.md` —
outside `repo-a`'s root entirely, so base 1 does not apply (the candidate
escapes) and, absent a sibling declaration, the two-base rule alone reports it
`MISSING`. With `repo-a/.repo/link-siblings.json` declaring
`{"notes": "../notes"}`:

- If `workspace/notes/` is checked out and the session file exists, the link
  resolves via sibling repo `notes` — not a finding.
- If `workspace/notes/` is checked out but the file is gone (renamed, deleted),
  it's a genuine `MISSING` finding — the provenance trail really is broken.
- If `workspace/notes/` is not checked out on this machine at all, the link is
  `sibling repo not present — unverifiable` — not a false positive.

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
