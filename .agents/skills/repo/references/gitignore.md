---
name: "gitignore"
description: "Audit gitignore rules — find over-ignored files and under-ignored build artifacts"
domain: repo
type: command
user-invocable: true
---

# /repo:gitignore — Gitignore Audit

Check that gitignore rules are appropriate for this repository. Catches files
that shouldn't be ignored and build artifacts that should be.

## Usage

```
/repo:gitignore                  # Full repo — apply clear rule fixes, report as you go
/repo:gitignore data/            # Check one subtree
/repo:gitignore --ask            # Review findings and confirm before editing
```

## Context First

Determine whether the repo is public or private before judging rules
(`gh repo view --json isPrivate --jq .isPrivate`, or ask the user if there is
no GitHub remote). The right answer differs:
- **Private repos** often want data files, docs, and notes *tracked* — flag
  rules that hide them.
- **Public repos** often want those same files *ignored* — flag tracked files
  that look like they leaked in (credentials, dumps, personal notes are
  critical findings either way).

## What It Checks

### 1. Over-Ignored Files
Flag gitignore rules that exclude things that look like real content:
- Data files (.yaml, .json, .csv) that aren't build output
- Documentation or notes
- Configuration that isn't secrets

**Always keep ignored, in any repo:**
- `.env` files and anything credential-like
- `node_modules/`, `.venv/`, `__pycache__/`
- Build output (`dist/`, `build/`, `target/`, `*.pyc`)
- IDE files (`.vscode/`, `.idea/`)
- OS files (`.DS_Store`)

### 2. Under-Ignored Files
Find tracked files that are probably build artifacts:
- `*.pyc`, `__pycache__/`
- `dist/`, `build/`, coverage output
- Large binaries that look generated (`.o`, `.so`, `.whl`)

### 3. Gitignore Hygiene
- Redundant rules (already covered by a parent `.gitignore`)
- Rules that match zero files (stale after cleanup)
- Scattered `.gitignore` files that could be consolidated

**Do not flag `X` and `X/` as duplicates without verification.** A trailing
slash restricts a gitignore pattern to directories — it never matches a
symlink, even one that points at a directory (`man gitignore`: "If there is a
separator at the end of the pattern then the pattern will only match
directories"). The two rules are therefore not interchangeable:

| Path at `X`           | Matched by `X` | Matched by `X/` |
|-----------------------|:--------------:|:---------------:|
| Real directory        | yes            | yes             |
| Symlink (to anything) | yes            | **no**          |
| Regular file          | yes            | **no**          |

`X` and `X/` in the same file are true duplicates only when `X` is guaranteed
never to be a symlink. Verify it — don't eyeball it:

```bash
[ -d "X" ] && [ ! -L "X" ]   # true only for a real, non-symlink directory
```

`[ -d "X" ]` alone is **not** sufficient: it follows symlinks, so a symlink to
a directory passes it. If the check fails, `X` doesn't exist to test, or `X`
could plausibly become a symlink in this repo (vendored trees, external
volumes, build outputs relocated to another disk), the pair is **not
redundant** — keep both rules in the suggested-fix output and report the pair
as intentional rather than collapsing it. Dropping the bare rule un-ignores any
symlink at that path, because `X/` alone won't re-cover it. This caused a live
regression: `.lake` + `.lake/` were deduped to `.lake/`, unignoring a `.lake`
symlink (rjwalters/lean-genius#43683).

**Do not flag a subdirectory rule as redundant with an ancestor's on matching
pattern text.** A pattern containing a `/` anywhere other than a single trailing
slash is **anchored** to the directory of the `.gitignore` that declares it
(`man gitignore`: "If there is a separator at the beginning or middle (or both)
of the pattern, then the pattern is relative to the directory level of the
particular `.gitignore` file itself"). Identical text in two `.gitignore` files
at different depths can therefore cover entirely **disjoint** path sets:

| Pattern in a `.gitignore`        | Anchored to that directory? | Reaches `sub/<name>`? |
|----------------------------------|:---------------------------:|:---------------------:|
| `name`, `*.log`, `name/`         | no — matches at every depth | yes                   |
| `**/name` (leading `**/`)        | no — matches at every depth | yes                   |
| `/name`, `dir/name`, `dir/*`     | **yes**                     | **no**                |
| `dir/**/name` (non-leading `**`) | **yes** — `dir/` anchors it | **no**                |

Only a **leading** `**/` un-anchors a pattern. A `**` in the *middle* crosses
intermediate directory levels *below* the anchored prefix; it does not cross the
prefix itself. With a root `.gitignore: dir/**/name`, both `dir/name` and
`dir/x/name` are ignored but `sub/dir/name` is **not** — so for redundancy
purposes `dir/**/name` is anchored exactly like `dir/name`, and a
`sub/.gitignore: name` beneath it is **not** redundant with it. Only the leading
form (`**/name`) reaches every depth.

So `proofs/.gitignore: .vscode/` is **not** redundant with a root
`.gitignore: .vscode/*` — the root rule is anchored and never reaches
`proofs/.vscode/`, so dropping the subdirectory rule un-ignores that whole
subtree. This command made exactly that recommendation on a live repo (#531);
only the `git check-ignore` gate below caught it before the commit.

A rule is redundant only when a rule in the **same or an ancestor** `.gitignore`
matches the same paths *from where that rule sits*. Anchoring is not the only
way identical text stays load-bearing, either: `.gitignore` precedence resolves
deepest-file-last, so a subdirectory rule can be re-establishing an ignore that a
**negation** in an ancestor cancelled — root `*.log` + `!important.log` with
`sub/.gitignore: *.log` keeps `sub/important.log` ignored, and removing the
subdirectory copy un-ignores it. Treat the textual redundancy model as a
*candidate filter only*; the verification below is what decides.

### 4. Large Untracked Files
Find untracked files >1 MB that might need a decision:
- Should they be tracked? (data files, docs)
- Should they be gitignored? (build output, caches)
- Should they live outside the repo? (measurement data, large datasets —
  object storage, LFS, or a NAS)

## Interaction

For each `.gitignore` file, show:
- Current rules and what they match
- Suggested additions or removals
- Files affected by changes
- **The ignore-status verification result for every removal or narrowing** — one
  line per rule, `verified` with the path count or `REFUSED` with the paths that
  changed, per "Verify a removal changes nothing" below. A removal reported
  without a verification result is indistinguishable from an unverified guess,
  so the line is never omitted — including when these fixes come from [[all]]'s
  Audit stage, whose report inherits this format.

By default, apply the clear-cut rule changes (adding an obvious build-artifact
ignore, removing a rule that hides real content) and report each edit — gitignore
changes are fully git-reversible. Leave anything ambiguous, or that would change
whether a **tracked** file stays tracked, as a reported recommendation. Under
`--ask`, confirm every edit before writing.

### Verify a removal changes nothing

Removing or narrowing a rule on the strength of the redundancy model alone is not
allowed — the model reasons about pattern text, and the subtleties above
(anchoring, trailing-slash semantics, negation precedence) all live in how git
*resolves* that text. Ask git instead, before and after, and let the answer
decide whether the edit is applied at all.

Run this from the repository root, with the scratch files **outside** the repo so
they cannot perturb the very listing being compared:

```bash
S=$(mktemp -d)

# The population that matters: paths ignored today. Tracked files are unaffected
# by a .gitignore edit, so the ignored-untracked set is the whole exposure.
# Scope the pathspec to the subtree the edited .gitignore governs — `-- .` for a
# root-level rule. Add --directory when a huge ignored tree (node_modules/)
# makes the full listing unwieldy: it collapses a wholly-ignored directory to one
# entry, and a directory that stops being wholly ignored still differs, either by
# vanishing or by expanding into its members.
# `-c core.excludesFile=/dev/null` belongs HERE, on the gate itself — not only on
# the attribution view below. `ls-files --exclude-standard` reads the user's
# global excludes too, so without it a host-global `*.log` keeps a path listed
# after the repo's own rule is gone and the diff comes back empty (SAFE) for a
# removal that really did un-ignore it.
ipaths() {
  git -c core.excludesFile=/dev/null \
    ls-files --others --ignored --exclude-standard -z -- "$1" \
    | tr '\0' '\n' | sed '/^$/d' | sort
}
# Which rule covers each path, in `<file>:<line>:<pattern>\t<path>` form.
attrib() { ipaths "$1" | git -c core.excludesFile=/dev/null check-ignore -v --stdin; }

ipaths proofs/ > "$S/paths.before"
attrib proofs/ > "$S/attrib.before"

# ... apply the removal ...

ipaths proofs/ > "$S/paths.after"
attrib proofs/ > "$S/attrib.after"

diff -u "$S/paths.before" "$S/paths.after"     # MUST be empty
diff -u "$S/attrib.before" "$S/attrib.after"   # informational — see below
```

`-c core.excludesFile=/dev/null` on **both** functions keeps a user's global
excludes from masking a difference the repo's own rules would show. Putting it
only on `attrib` is not enough — that diff is explicitly not the gate (below), so
the override would sit on the one view whose answer does not decide anything
while `ipaths`, which does, stayed machine-dependent.

**Scope that guarantee honestly:** `--exclude-standard` reads three sources — the
repo's `.gitignore` files, the global excludes file, and `.git/info/exclude`.
`core.excludesFile=/dev/null` neutralizes the second only. `.git/info/exclude` is
equally clone-local and is **not** neutralized here (there is no config knob to
redirect it, and moving the file aside would mutate the user's clone, which this
read-only check must not do). So the result is a property of the repo *plus this
clone's `.git/info/exclude`* — not of the machine's global config. If a rule under
test overlaps a pattern in `.git/info/exclude`, say so in the finding rather than
reporting a bare `verified`.

**The path-set diff is the gate.** If it is non-empty — a path un-ignored, or a
path newly ignored by an over-broad rewrite — **revert the edit and do not count
it as applied**. Report it as a finding instead, naming the paths that changed and
the rule that was actually covering them, and leave the rule in place:

```
proofs/.gitignore: .vscode/ — removal REFUSED: 3 paths would un-ignore
  (root .vscode/* is anchored to the root and does not reach proofs/.vscode/)
```

**The attribution diff is not a gate** — it is expected to change on a genuine
dedupe, because the surviving ancestor rule takes over coverage. Report it so the
new owner is visible:

```
sub/.gitignore: *.log — removed, verified: 12 paths unchanged
  (coverage moved to .gitignore:1:*.log)
```

This check is necessary, not sufficient: it can only speak for paths that exist
on disk right now, so a rule covering build output that is not currently present
passes it trivially. The anchoring and precedence reasoning above still has to
hold on its own; the gate exists to catch the cases where that reasoning was
wrong, which is how #531 was caught.

Run the gate per rule, not once per file — a batched `.gitignore` rewrite that
nets out to an unchanged path set can still have one wrong removal masked by
another change. Same gate when these fixes are offered from [[all]]'s Audit stage.

### Verify after write

Applying a `.gitignore` edit is not proof it survived. A concurrent writer —
another agent working in the same clone, a background `git stash` or
`git checkout --`, a pre-commit hook, a Loom sweep quarantining the primary
clone's working tree — can revert a file between the moment you fix it and the
moment you report it, leaving this command claiming a fix that is no longer on
disk.

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

This applies equally when these rule fixes are offered from [[all]]'s Audit
stage rather than from `/repo:gitignore` directly — same edits, same check.

### Loom-managed repo: land fixes where a sweep cannot take them

The check above catches an edit reverted *during* the run. It cannot catch the
likelier failure in a Loom-managed repo, which happens *after* it: a sweep runs
`check-main-clean.sh --quarantine`, which polices the **primary checkout's
working tree as a whole** — not the branch it happens to be on — and stashes
every uncommitted delta it finds (stash label
`loom-quarantine: run=<sweep-id> issue=<N>`), so the fixes this command reported
as applied are off disk minutes later. Branching does not help — the quarantine
is branch-blind.

**The destination ladder lives in one place: [[docs]] → "Loom-managed repo: land
fixes where a sweep cannot take them".** Follow it exactly as written — the
`loom_managed` detection, the four destinations in order (dedicated issue
worktree / `chore/repo-hygiene-<date>` branch + PR in a worktree off
`origin/<default>` when the default branch is PR-protected or the operator asks
/ commit on an otherwise-clean current branch / uncommitted plus a warning), the
`.loom-managed`-sentinel rule for the hygiene worktree, and the quarantine
recovery path. It is not restated here so the two copies cannot drift apart.

Two substitutions are yours, and only these two. **Decide the destination before
applying the first fix** and report it once, up front:

- The one-line warning under the last arm names gitignore fixes:

  ```
  Loom-managed repo: uncommitted gitignore fixes in the primary checkout can be quarantined by a sweep — commit or stash them now.
  ```

- **Name the destination in the report, not just the count** — with this
  command's own noun:

  ```
  Gitignore: 2 fixed on feature/issue-448 (worktree .loom/worktrees/issue-448, a1b2c3d)
  Gitignore: 2 fixed on chore/repo-hygiene-2026-09-30 (worktree .loom/worktrees/repo-hygiene-2026-09-30, a1b2c3d) — main protected; push + PR offered
  Gitignore: 2 fixed, committed on main (a1b2c3d)
  Gitignore: 2 fixed — uncommitted, at risk
  ```

This too applies equally when the rule fixes are offered from [[all]]'s Audit
stage: same edits, same destination decision, and the Audit line names where
they landed.
