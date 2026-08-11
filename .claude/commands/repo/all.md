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
it only ever runs [[deps]]' read-only half (`--check`): scaffolding Dependabot
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

**If `eligible=no`**, this stage is a no-op: say nothing, and run the remaining
stages exactly as before, with Reset in full at the end. Unpushed work on the
working branch, a dirty tree, and an already-on-default run all land here, and
none of them behave any differently than they did before this check existed.

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

**Third-party dependencies** (see [[deps]]): run the report-only form,
`[[deps]] --check`. Report three independent items — never collapsed into one
"Dependabot: on":

- whether `.github/dependabot.yml` is present (that file governs **version**
  updates only),
- the repo-level **security-updates** flag — report it **UNKNOWN (needs
  admin)**, not `disabled`, when the token can't read the setting, exactly as
  [[deps]] does; "can't see it" and "it's off" are different answers,
- the **count of open Dependabot PRs**, split into how many are real forward
  majors and how many are **stale** — proposing a version the manifest on the
  base branch already satisfies (or exceeds). [[deps]]' stale check does this
  comparison; a stale PR is never counted as a major and is **never** presented
  here as pending upgrade work. This split matters right after a bulk-update
  merge — the exact moment someone runs `/repo:all` to confirm the repo is
  clean — because Dependabot's still-open PRs from a pre-merge scan are stale,
  and reporting them as majors is a false upgrade-pressure signal.

Those counts — open, real majors, stale — are all `/repo:all` needs. Computing
the stale count requires [[deps]]' cheap per-PR manifest comparison (base-branch
manifest vs. the PR's target), but the rest of the per-PR classification table
(ecosystem, CI status, diff notes) stays with `/repo:deps --review`, which is a
separate, confirm-gated activity.

Only `--check` runs from here. `/repo:all` **never** scaffolds
`.github/dependabot.yml`, **never** flips a repository flag, and **never**
merges a Dependabot PR. Those are [[deps]]' always-confirm-first actions and
stay out of the sweep entirely — if any of them is warranted, say so and let
the user run `/repo:deps` themselves. Under `--ask` this half is unchanged;
it is already report-only, so there is nothing to confirm.

Dependabot is a GitHub feature, and [[deps]] refuses to run against another
forge. So if `origin` is not a GitHub remote, skip this half, report it on its
own line, and continue — it never fails the stage or the run:

```
Deps: check skipped (not a GitHub remote)
```

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
Audit:        3 findings surfaced (gitignore rule fixed)
Scrub:        1 at HEAD deferred (identity: docs/runbook.md:41); 63 history-only, 18 topology (--deep)
Docs:         2 fixed (README table, CHANGELOG entry), 1 deferred: docs/analysis/ missing README
Tidy:         freed 240 MB (build/, .cache/, 3 empty dirs)
Tools:        Anvil updated 1.4.0 → 1.5.1; Loom current
Deps:         dependabot.yml present, security updates OFF, 3 open PRs (0 majors, 2 stale — already satisfied by manifest)
Reset:        on main (up to date), tree clean, 4 branches deleted, 1 stash kept
Skipped:      remote (never part of /repo:all); deps install/review (confirm-first — run /repo:deps); scrub --deep/--owner/--forks (run /repo:scrub)
```

List anything intentionally left behind — deferred findings, kept stashes,
UNKNOWN branches — so the user knows exactly what state the repo is in.

When stage 2's early sync-and-switch ran, the `Reset:` line still carries the
pruning half's outcome — branches, worktrees, and stashes reviewed — and notes
the switch **once**, as something already done, so the summary never reads as
if the branch changed at the end of the run:

```
Reset:        synced early (feature/x → main, was 6 behind), tree clean, 4 branches deleted, 1 stash kept
```

Never drop the pruning reporting just because the switch moved earlier, and
never describe the same switch in both places.

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
