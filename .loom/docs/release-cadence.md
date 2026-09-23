# Release cadence vs. `VERSION`

`VERSION` (and the other five `scripts/version.sh`-managed files, #5517) bumps
on nearly every merge to `main` in this repo — it tracks the tree, not a
release. GitHub Releases are a **separate, deliberately less frequent**
event. This doc states the intended cadence and what it means for the
signed-artifact `--fetch` path (Epic #4990 Phase 3, #5009/#5018/#5020),
closing the gap described in #6010.

## The decision: explicit fleet-rollable releases, not every patch

Tagging every `VERSION` bump is not the goal — this repo bumps `VERSION`
roughly as often as it merges PRs (single digits to dozens of times a day),
so per-patch releases would mean cutting, codesigning, and cosigning cross-
platform artifacts continuously for no operational benefit; most bumps are
mechanical (docs, small fixes) with no fleet-roll urgency behind them.

Instead, a release is cut when there is a **concrete reason to roll the
fleet** from it — e.g. a fix or feature that hosts are waiting on, or simply
"it's been a while and the gap is getting expensive" (the trigger that filed
#6010: cutting a release would have let `--fetch` replace four
`cargo build --release` invocations, two of them on hosts already close to
their breaker trip). There is no fixed interval (daily/weekly) requirement —
the release-vs-`VERSION` gap is expected to fluctuate, not stay pinned at
zero.

`/repo:release` (see `CLAUDE.md` § "Forge Authentication & Releasing") is the
only supported way to cut a release; it is a human/operator-invoked flow, not
something Builder/Judge/Champion trigger automatically.

## What this means for `--fetch`

`defaults/scripts/cli/loom-daemon-update.sh`'s `--fetch` (force) mode resolves
the newest GitHub Release and hard-fails rather than silently falling back to
a source build when no usable artifact resolves — by design (Epic #4990 Phase
3b). Given the cadence above, **`--fetch` is for use at or shortly after a
release boundary**, not as the default fleet-roll path on every `VERSION`
bump. The supported default remains a source build
(`loom-daemon-update.sh` with no `--fetch`, or `--no-fetch` to force it
explicitly) — `--fetch` is an accelerator once a release exists for the
version you want, not a replacement for source-build in the general case.

## Making the gap visible (#6010)

Before this doc, the only signal that `--fetch` could not reach the current
source tree was a hard failure on `--fetch` itself, or an easy-to-miss
"not newer than the installed version" message that only compared against
whatever was already installed — not against the source tree an operator was
about to build from. `loom-daemon-update.sh` now also compares the newest
resolved release against the **source tree's own `VERSION` file** (not just
the installed binary's version) and reports the gap on both paths:

- **Resolution time** (any `--check`/plain run, not just `--fetch`): when the
  newest release is behind the source tree's `VERSION`, a warning is printed:
  `Artifact path cannot reach current source: newest release ... is behind
  this source tree's VERSION (...)`.
- **`--check`**: the same gap is summarized up front —
  `Release gap: installed ..., newest release ..., source ... — the
  artifact-fetch path cannot reach current source until a release >= ... is
  cut.`
- **Forced `--fetch` hard-fail**: the refusal now names the cause when it is
  this gap, rather than only the generic "no usable release artifact was
  resolved" message.

This is advisory only — it never changes exit codes on the plain/`--check`
paths, and the pre-existing hard-fail behavior of a forced `--fetch` with no
usable artifact is unchanged. It exists so an operator planning a fleet roll
can tell, before running anything destructive, whether `--fetch` is currently
usable or whether a release needs to be cut first.

## A missed intermediate Release is an accepted gap, not a defect (#8290)

`release.yml`'s `resolve` job now retries `gh release create` up to twice on a
5xx/403 before failing (bounded, logged) — see the step's own comments for the
retry shape. That covers most one-off transient forge failures. It does **not**
guarantee every `VERSION` bump gets a Release: if all retries are exhausted, or
the run fails before the retry loop even starts, that version's tag/Release is
simply never created.

**This is accepted, not treated as a gap to backfill**, for two reasons:

1. **Each bump's tag is unique** (`v<VERSION>`, and `VERSION` moves forward on
   nearly every merge). A later run resolves a *different*, newer tag — it has
   no way to notice or recreate a specific *earlier* version's missing
   Release without extra state (e.g. diffing the full tag history against
   Releases on every run just to catch a rare one-off). That mechanism would
   run on every single push to guard against a failure mode observed exactly
   once in this repo's history, at a cost (extra `gh` calls, more surface
   area to go wrong) out of proportion to the problem.
2. **The cadence design (above) already tolerates a release-vs-`VERSION` gap
   as normal** — most `VERSION` bumps intentionally get no Release at all,
   and a release is cut only at an explicit fleet-rollable boundary. A single
   missing intermediate Release from a transient failure is indistinguishable,
   downstream, from the far more common case of a bump that was never
   *meant* to have its own Release: the immediately-following bump's Release
   (or the next deliberately-cut one) supersedes it either way, and
   `--fetch`/`--check`'s gap reporting (above) is already keyed off "newest
   release vs. current source `VERSION`", not off "does every past version
   have a Release" — so it does not regress from this.

Concretely: this happened once, for `v0.19.169` on 2026-09-18 (HTTP 403,
"Resource not accessible by integration"); the very next bump, `v0.19.170`,
published normally one minute later with no operator action needed. If this
recurs at a rate that suggests it is not actually transient, that is a signal
to revisit this decision (e.g. add a periodic reconciliation job that lists
tags with no matching Release) — not something to build preemptively for a
single observed incident.

## See also

- `CLAUDE.md` § "Forge Authentication & Releasing" — how `/repo:release` works
  and what it publishes.
- [`.loom/docs/daemon-reference.md`](daemon-reference.md) — daemon self-update
  wrapper scripts (`loom-daemon-start.sh` / `loom-daemon-update.sh`) and how
  they fit the update lifecycle.
- Issue [#6010](https://github.com/rjwalters/loom/issues/6010) — the incident
  and acceptance criteria this doc satisfies.
- Issue [#8290](https://github.com/rjwalters/loom/issues/8290) — the one-off
  `gh release create` HTTP 403 that motivated the retry logic and the
  "accepted gap" decision above.
