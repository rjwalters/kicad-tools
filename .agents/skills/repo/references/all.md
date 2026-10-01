---
name: "all"
description: "The whole hygiene pass in order — audit, scrub, docs, tidy, update-tools, reset — safe fixes by default, destructive steps gated"
domain: repo
type: command
user-invocable: true
---

# /repo:all — The Whole Hygiene Pass

Run the full sequence of sensible repo work in one go: scan for problems, check
the public surface for sensitive identifiers, bring the docs back in line with
reality, tidy filesystem clutter, refresh installed tool packages, report on
third-party dependency currency, and land back on a clean baseline. This is the
umbrella command
— it orchestrates the other `/repo:*`
commands in a deliberate order, applying each one's safe fixes by default and
keeping the same safety gates on destructive steps that each uses on its own.

It deliberately does **not** launch cloud dev sessions ([[remote]]) — that
provisions paid infrastructure and is never part of a routine hygiene pass. And
it only ever runs [[deps]]' read-only half (`--check`): configuring dependency automation
config, flipping repository flags, and merging bot PRs all stay behind
`/repo:deps`' own confirmations.

## Usage

```
/repo:all                      # Full repo — apply safe fixes across stages, report as you go
/repo:all --ask                # Confirm findings before applying at every stage
/repo:all packages/core        # Scope the read-only scans to one subtree
/repo:all --prune              # Also delete confirmed-safe branches/worktrees (passed to reset, after the loss check)
/repo:all --caches             # Also clear regenerable caches in the Tidy stage (passed to tidy; off by default)
```

The optional path argument scopes the scanning stages ([[audit]]) the same way
it does for those commands. Stages that act on global git or filesystem state
([[tidy]], [[reset]]) always operate on the whole repo.

## Stages

Run these in order. Each stage applies its safe, reversible fixes by default
and reports them; irreversible removals (Tidy's ASK items, Reset's
branch/worktree/stash deletion) still require explicit approval and pass the
permanent-loss check — they are never chained silently. Under `--ask`,
every stage reverts to report-first: show what was found and get a yes before
acting. If the user declines a stage, note it and continue to the next.

### 1. Audit (see [[audit]])

Run the full read-only health sweep: README accuracy, orphaned files, broken
links, gitignore issues, branch & worktree hygiene. Produce the combined audit
report. This surfaces everything before anything is touched.

Offer to fix gitignore findings here. Leave README, link, and documentation
fixes for the Docs stage next — don't apply them twice.

**Orphaned files are actioned here too, and nowhere else.** Every other finding
class has a later home — docs and links to Docs (stage 4), branch and worktree
hygiene to Reset (stage 7) — but an orphan has none. Tidy (stage 5) is the
stage it looks like it belongs to and deliberately is not: Tidy inventories
*filesystem clutter*, and its SAFE tier is safe precisely because those files
are regenerable or were never committed. A file tracked in git is neither, so
without an owner here an orphan finding would fall out of the run entirely.
Split the findings by tracked-ness and handle each half explicitly:

```bash
git ls-files --error-unmatch -- "$path" >/dev/null 2>&1 && echo tracked || echo untracked
```

- **Untracked orphans** (build outputs never committed, empty dirs) — leave
  them for Tidy in stage 5, which already sorts them into its own SAFE/ASK
  tiers. Acting on them here would put the same file behind two different
  gates in one run.
- **Tracked orphans** — offer removal here, **one file at a time, each behind
  its own explicit yes**, including under the default (non-`--ask`) form. This
  is never folded into Tidy's auto-applied SAFE tier and must not behave like
  it: deleting a tracked file is a repo change, recoverable only from history,
  and "nothing references it" is evidence rather than proof — a deliberately
  kept standalone script, a fixture opened by path at runtime, and a genuinely
  dead file all look identical to the orphan check. Show the finding's own
  evidence with the offer (what was grepped for, how many references were
  found) so the yes is an informed one, and remove with `git rm` so the removal
  is staged as the repo change it is.

This is the same kind of exception the gitignore offer above already is, and it
does not loosen [[audit]]'s read-only posture: [[audit]] still changes nothing
on its own — as [[orphans]] likewise reports and waits — and what acts here is
`/repo:all`'s stage, only after the user says yes to that specific file.

**Anything not removed is deferred, not dropped.** Carry every tracked orphan
the user declined — or that was never offered, because the stage was skipped or
the run is scoped to a subtree — into the final summary as a deferred item, the
same way stage 3's scrub findings are. A finding that appears only in the audit
report and nowhere in the summary is exactly what this stage exists to stop
losing.

### 2. Sync early, if and only if nothing can be lost (see [[reset]])

[[reset]] is two separable halves, and only one of them is safe to move:

- **Sync-and-switch** (reversible): `git fetch`, check out the default branch,
  `git pull --ff-only`. Nothing is removed.
- **Pruning** (gated): stash review, branch & worktree deletion. Can
  permanently remove work, so it keeps its gates and stays last (stage 7).

Running the whole of Reset last assumes the working branch is the right place
for the later stages to be looking — which holds when it carries unpushed work
those stages should see. When the branch is **fully pushed and behind the
default branch** that assumption inverts: there is no local-only content to
protect, and Docs/Tidy either report drift that is already fixed upstream or
edit a copy that pollutes an open PR's diff and blocks the branch switch Reset
performs at the end. So check the branch state here, before Docs runs:

```bash
current=$(git symbolic-ref --short HEAD 2>/dev/null) || current=""
# Fetch BEFORE resolving origin/HEAD: `default` comes from a local ref, so
# resolving it first would read a stale (or, on a fresh or --single-branch
# clone, missing) value and no-op on the run's first opportunity to be eligible.
git fetch origin --quiet
default=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')

eligible=no
if [ -n "$current" ] && [ -n "$default" ] && [ "$current" != "$default" ] \
   && [ -z "$(git status --porcelain)" ]; then
  # "Fully pushed" = HEAD has an upstream AND is not ahead of it.
  if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    ahead_of_upstream=$(git rev-list --count '@{u}..HEAD')
  else
    ahead_of_upstream=""   # no upstream at all => never pushed, not eligible
  fi
  behind_default=$(git rev-list --count "HEAD..origin/$default" 2>/dev/null || echo 0)
  if [ "$ahead_of_upstream" = "0" ] && [ "$behind_default" -gt 0 ]; then
    eligible=yes
  fi
fi

# A second, independent detection — not a change to the block above. It
# covers exactly the case the first block's `current != default` guard
# discards without inspecting further: already on the default branch, with
# local commits it hasn't pushed AND commits upstream it doesn't have. The
# two are mutually exclusive: this one requires current == default, so it can
# never also set eligible=yes.
diverged_on_default=no
if [ -n "$current" ] && [ -n "$default" ] && [ "$current" = "$default" ] \
   && [ -z "$(git status --porcelain)" ]; then
  ahead_of_origin_default=$(git rev-list --count "origin/$default..HEAD")
  behind_origin_default=$(git rev-list --count "HEAD..origin/$default")
  if [ "$ahead_of_origin_default" -gt 0 ] && [ "$behind_origin_default" -gt 0 ]; then
    diverged_on_default=yes
  fi
fi
```

Every condition must hold, and each one rules out a distinct way the early
switch could lose work or fail:

| Condition | Why it is required |
|---|---|
| On a branch (not detached), and it isn't already the default | Nothing to switch to; a detached HEAD has no upstream to reason about |
| `origin/HEAD` resolves to a default branch | Without it there is no defensible switch target — leave the order alone. The fetch above runs first so a stale copy is refreshed in time; a repo that has never had `origin/HEAD` set locally at all (some `--single-branch` clones) needs a one-off `git remote set-head origin --auto`, and stays a no-op until then |
| Working tree clean (`git status --porcelain` empty) | A dirty tree would have to be stashed to switch, and this run ends on the default branch, so there is no natural point to pop it back |
| Branch **has an upstream** and is **0 commits ahead of it** | This is the literal "no unpushed commits". A branch that was **never pushed** has no upstream and is **not** eligible — treat it exactly like unpushed commits |
| Branch is behind `origin/<default>` by at least one commit | If it isn't behind, the checkout is already current and switching buys nothing |
| **Already on the default branch, and diverged from `origin/<default>`** (ahead **and** behind) | Not part of `eligible` — `current != default` already excludes it above — but not a silent no-op either. It fails this table's first row and is handled separately as `diverged_on_default`, below |

Being ahead of the *default branch* is **not** disqualifying — a pushed PR
branch normally is. The unpushed-work test is against the branch's own
upstream, which is a different axis; a branch that has diverged from the
default branch is still eligible as long as everything on it is pushed.

**If `eligible=yes`**, run only the sync-and-switch half now — [[reset]]'s
step-1 refresh (`git fetch --all --prune`) followed by its step 4 (`git
checkout "$default"` and `git pull --ff-only`) — so Scrub, Docs, Tidy, and
Update tools all operate on a fresh default-branch checkout. Report it on one
line as it happens:

```
Reset: synced early — feature/x was fully pushed and 6 commits behind main; switched before Docs
```

Under `--ask`, report the finding and get a yes before switching, like every
other stage. The checkout and the pull fail differently, and only one of them
leaves the run where it started — report them as the distinct outcomes they are:

- **The checkout fails.** HEAD never moved, so change nothing, report why, and
  continue with the stage order unchanged — stage 7 will surface it again with
  [[reset]]'s own handling. The common case in a Loom-managed repo lands here:
  the default branch is already checked out in another worktree (Loom keeps one
  per issue), so git refuses with `fatal: '<default>' is already used by
  worktree at '<path>'` — exit 128, HEAD unchanged. Name that cause when it is
  the reason; it is an expected, recognized no-op, not a generic failure.
- **The checkout succeeds and the `--ff-only` pull then fails** (a diverged
  local default branch, say). The switch already happened, so "nothing changed"
  would be false: the run is now sitting on that diverged local default branch,
  and Scrub, Docs, Tidy, and Update tools will read that copy for the rest of
  the run. Say exactly that — which branch you are on and that it is diverged
  from its upstream — rather than reporting a clean no-op. Stage 7 still
  surfaces it again with [[reset]]'s own divergence handling.

**If `eligible=no`** and `diverged_on_default=no`, this stage is a no-op: say
nothing, and run the remaining stages exactly as before, with Reset in full at
the end. Unpushed work on the working branch, a dirty tree, and an
already-on-default-but-merely-behind run all land here, and none of them
behave any differently than they did before this check existed.

**One narrow exception to "say nothing": a dirty tree in a Loom-managed repo.**
There, a dirty primary checkout is not merely a tree this stage declines to
switch — it is a tree other processes actively rewrite. A sweep runs
`check-main-clean.sh --quarantine`, which stashes the primary clone's entire
uncommitted delta (labelled `loom-quarantine: run=<sweep-id> issue=<N>`) to give
its worktrees a clean base, regardless of which branch that delta sits on. That
is a finding rather than a fix, so — like `diverged_on_default` above — it does
not need permission to say, and it changes nothing about eligibility:

```
Reset: not eligible for early switch — tree dirty; Loom-managed repo, so uncommitted work here can be quarantined by a sweep mid-run
```

Say it once, here, so the Docs stage's choice of where to put its fixes (stage 4)
reads as a consequence rather than a surprise. Detect the same way [[docs]] does
(`.loom/` root with `worktrees/`, or a running `loom-daemon`, and not already
inside a `.loom-managed` worktree); in any other repo this line never appears.

**If `diverged_on_default=yes`**, this is the shape this issue exists for: the
checkout is already on the default branch, so there is no branch to switch
to — but Scrub, Docs, Tidy, and Update tools are still about to run against a
copy that is blind to `$behind_origin_default` upstream commits, exactly as if
it had failed to switch off a stale feature branch. Report it before Docs
runs, even under the default (non-`--ask`) form — this is a finding, not a
fix, so it doesn't need permission to say:

```
Reset: main has diverged from origin/main — 2 unpushed commits, 3 upstream commits later stages won't see; resolve first or continue
```

Under `--ask`, offer to resolve it rather than only reporting it — but this is
the same operator decision [[reset]] step 4 already gates its diverged-local-
default-branch case behind, not a new one: report the divergence (`git log
--oneline @{u}..HEAD` and `HEAD..@{u}`) and ask how to proceed. Do not rebase
or force anything on your own. If the user declines to resolve now, continue
the run unchanged — the finding was already reported, and stage 7 surfaces the
same divergence again with [[reset]]'s own handling.

### 3. Scrub (see [[scrub]])

Scan this repo's public surface for sensitive identifiers — credentials, cloud
resource IDs, identity, affiliated entities, network topology — across tracked
files at HEAD, commit history, and issue/PR bodies and comments. Read-only:
[[scrub]] never edits a file, an issue, or history, so this stage can never
change the repo.

Run the **default form only**. Do not pass `--deep`, `--owner`, or `--forks`
from here, even under `/repo:all --ask`:

- `--owner` enumerates every public repo for an owner from the forge and
  `--forks` walks fork networks recursively — both cost real API budget and
  minutes, and neither is scoped to the repo this hygiene pass is about.
- `--deep` adds the history-only and network-topology classes. Those are
  high-volume and almost never actionable in a routine pass — internal
  hostnames in `Co-authored-by:` trailers alone ran to 60+ commits across 8
  repos in one real sweep, all correct, all unfixable without a history
  rewrite. Emitting them every run is how this stage would train people to skim
  past the whole report.

So report **credentials and live-at-HEAD findings**, and collapse everything
else to a count with a pointer:

```
Scrub: 1 finding at HEAD (identity: docs/runbook.md:41); 63 history-only, 18 topology — /repo:scrub --deep
```

**Findings never fail the run.** [[scrub]] reports and stops — the remedy is a
judgment call with irreversible forms (deleting an issue, rewriting history),
and it is never made automatically, under any flag. Carry findings into the
final summary as deferred items so they are not silently forgotten.

`/repo:scrub` exits `2` when it could not check — no `gh`, not authenticated, an
API failure mid-scan. That is **inconclusive, not clean**: report it on its own
line and continue, exactly as the Deps half does for a non-GitHub remote.

```
Scrub: check incomplete (gh not authenticated) — issue/PR surface unscanned
```

### 4. Docs (see [[docs]])

Bring the documentation back in line with reality: content accuracy (stale
prose, out-of-date command/feature tables, CHANGELOG drift), README structure,
and internal cross-references. This is the explicit, named home for the doc
fixes the audit surfaced — apply the ones the user approves.

**Report where those fixes landed, not only that they were applied.** In a
Loom-managed repo [[docs]] picks a destination up front — its "Loom-managed
repo: land fixes where a sweep cannot take them" step: a commit in a dedicated
issue worktree, a commit on a `chore/repo-hygiene-<date>` branch plus a PR when
the default branch is PR-protected, a commit on an otherwise-clean current
branch, or uncommitted plus an explicit quarantine warning. [[gitignore]] and
[[links]] point at that same canonical ladder, so the one choice governs the
rule fixes stage 1 applied and any link fixes made here. Carry that destination
into this stage's line and into the final summary — a branch or worktree name
when the fixes were committed,
`uncommitted, at risk` when they were not. In a repo that is not Loom-managed
there is no destination to name and the line is exactly what it always was.

### 5. Tidy (see [[tidy]])

Inventory filesystem clutter — build artifacts, caches, temp files, empty dirs
— present it grouped with sizes, and delete the SAFE junk (OS droppings, merge
leftovers, empty dirs outside tool-scaffolding/worktree roots — see [[tidy]]'s
own SAFE/ASK categorization for exactly which empty directories qualify).
**Regenerable caches are kept by default** in a routine
hygiene pass — deleting them just forces a costly rebuild — so this stage does
**not** pass `--caches` to [[tidy]] unless the user gave `/repo:all --caches`.
Environments (`node_modules/`, virtualenvs) and other ASK items are never
auto-removed here regardless.

### 6. Update tools, and check dependency currency (see [[update-tools]], [[deps]])

Two currency checks run here, both report-first.

**Installed tool packages** (see [[update-tools]]): check Loom, Anvil, Repo
itself, … against their sources. Report what's behind and offer to update.

**Third-party dependencies** (see [[deps]]): run `[[deps]] --check`. Report the
installed organization policy/source, detected provider (Renovate, Dependabot,
or intentional mixed coverage), config/app status, vulnerability alerts,
security PR ownership, cooldown and automerge policy, and open dependency PRs.
These are independent states: an intentionally disabled Dependabot security-PR
flag is expected when Renovate owns fixes. Missing Dependabot config does not
mean automation is absent.

Summarize open PRs as real forward majors, other pending work, and genuinely
stale PRs. Use [[deps]]' base-manifest **and lockfile** comparison; a permissive
manifest range alone cannot establish that a lockfile security update is stale.
Full diff/CI triage remains with `/repo:deps --review`.

This stage is always report-only, including under `--ask`: it does not deploy
organization policy, change client files/settings, or merge PRs. Refer policy
drift to [[org-policy]] and client adoption to [[deps]]. If origin is not GitHub,
report `Deps: check skipped (not a GitHub remote)` and continue the sweep.

### 7. Reset (see [[reset]])

Last, because this is where branch state can be permanently removed. Run the
end-of-task baseline ritual: working-tree safety check, stash review, branch &
worktree pruning, `git fetch --prune`, and return to the default branch. Pass
`--prune` and `--ask` through if either was given to `/repo:all`.

The **pruning half always runs here**, with its existing gates — which
branches, worktrees, and stashes are safe to remove has nothing to do with
which branch the earlier stages ran against, so stage 2 never moves it.

If stage 2 already performed the sync-and-switch, run [[reset]] unchanged
anyway — the checkout is a no-op and the fetch/pull still picks up anything
the remote gained during the run — but **don't report the switch twice**. It
was announced when it happened; here just report the resulting branch state
along with the pruning outcome.

Otherwise this stage behaves exactly as it always has: the earlier scans and
cleanup happened while you were still on the working branch, and you finish on
a clean default branch.

## Final Summary

After all stages, print one consolidated report so nothing is silently
forgotten:

```
REPO:ALL COMPLETE
=================
Audit:        3 findings surfaced (gitignore rule fixed, 1 tracked orphan removed), 2 tracked orphans deferred: vite.config.d.ts, vitest.config.d.ts
Scrub:        1 at HEAD deferred (identity: docs/runbook.md:41); 63 history-only, 18 topology (--deep)
Docs:         2 fixed (README table, CHANGELOG entry), 1 deferred: docs/analysis/ missing README
Tidy:         freed 240 MB (build/, .cache/, 3 empty dirs)
Tools:        Anvil updated 1.4.0 → 1.5.1; Loom current
Deps:         Renovate, org policy current, alerts ON, security PRs: Renovate, age: 14d/1d, automerge OFF, 3 open PRs (0 majors, 2 stale)
Reset:        on main (up to date), tree clean, 4 branches deleted, 1 stash kept
Skipped:      remote (never part of /repo:all); deps install/review (confirm-first — run /repo:deps); scrub --deep/--owner/--forks (run /repo:scrub)
```

List anything intentionally left behind — deferred findings, tracked orphans
left in place, kept stashes, UNKNOWN branches — so the user knows exactly what
state the repo is in. Tracked orphans are named individually rather than
counted: the whole point of deferring one is that the user has to decide about
that specific file later, and a bare `2 orphans deferred` sends them back to
re-run the audit to find out which.

When stage 2's early sync-and-switch ran, the `Reset:` line still carries the
pruning half's outcome — branches, worktrees, and stashes reviewed — and notes
the switch **once**, as something already done, so the summary never reads as
if the branch changed at the end of the run:

```
Reset:        synced early (feature/x → main, was 6 behind), tree clean, 4 branches deleted, 1 stash kept
```

Never drop the pruning reporting just because the switch moved earlier, and
never describe the same switch in both places.

### Where the fixes live (Loom-managed repos)

The example above is a repo that is not Loom-managed, so `N fixed` is the whole
story. In a Loom-managed one it is not: uncommitted edits in the primary
checkout are quarantined by the next sweep's `check-main-clean.sh --quarantine`
(stash label `loom-quarantine: run=<sweep-id> issue=<N>`), so `2 fixed` can be
true when it prints and false ten minutes later. The Docs stage therefore picks
a destination up front — see [[docs]]' "Loom-managed repo: land fixes where a
sweep cannot take them", the canonical copy [[gitignore]] and [[links]] point at
— and **the summary names that destination**:

| Where the fixes landed | Summary line |
|---|---|
| Committed in a dedicated issue worktree | `Docs: 2 fixed on feature/issue-448 (worktree .loom/worktrees/issue-448, a1b2c3d)` |
| Committed on a hygiene branch (PR-protected default) | `Docs: 2 fixed on chore/repo-hygiene-2026-09-30 (worktree .loom/worktrees/repo-hygiene-2026-09-30, a1b2c3d) — main protected; push + PR offered` |
| Committed on the current branch | `Docs: 2 fixed, committed on main (a1b2c3d)` |
| Left uncommitted in the primary checkout | `Docs: 2 fixed — uncommitted, at risk` |
| Not a Loom-managed repo | `Docs: 2 fixed (README table, CHANGELOG entry)` — unchanged |

The same applies to the `Audit:` line when stage 1 applied gitignore rule fixes:
name where those landed too, on the same four shapes.

The `uncommitted, at risk` row carries the warning [[docs]] prints, once, on its
own line beneath the summary — it is the one arm where the run ends with work
that something else may take:

```
Loom-managed repo: uncommitted doc fixes in the primary checkout can be quarantined by a sweep — commit or stash them now.
```

### Re-verify before printing

A fix applied in the Docs stage can be gone by the time this summary prints. A
concurrent writer — another agent in the same clone, a background `git stash`
or `git checkout --`, a Loom sweep quarantining the primary clone's working
tree — reverts the working tree without touching this run, and the stage that
applied the edit has long since reported success.

So **immediately before printing the consolidated summary, each stage
re-verifies that the edits it applied are still present on disk** (the same
unconditional verify-after-write check each command documents — see [[docs]],
[[readme]], [[gitignore]], [[links]]). A stage's `N fixed` count only ever
includes edits confirmed still on disk at print time.

Any edit found reverted gets its **own line**, never folded into that stage's
fixed count — same convention as deferred findings and kept stashes above:

```
Docs:         1 fixed (README table), 1 reverted after apply — needs re-run: CHANGELOG entry
```

Only the affected stage's line changes; stages whose edits survived report
exactly as they otherwise would. In a repo with no concurrent writer the
re-verification always finds every edit still applied, so the summary is
byte-for-byte what it was before this check existed.

## Principles

Same as every hygiene command: **apply safe fixes, gate destructive ones**
(reversible fixes apply by default, `--ask` to confirm first, irreversible
removals always require explicit opt-in); **general by design**; **don't be
noisy**. `/repo:all` adds only sequencing — each stage keeps its own safety
gate, and no stage is skipped or its destructive actions auto-approved just
because it runs under the umbrella.
