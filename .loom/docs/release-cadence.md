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

## A release is visible before its assets are (#8515)

`release.yml` publishes the Release **first** (the `resolve` job's `gh release
create`) and uploads the per-target assets afterwards, from independent
`build-daemon` matrix legs. For the whole duration of that matrix — minutes —
the Release exists with **no artifact for your platform**, and a single `gh
release view` snapshot cannot tell that apart from a platform that will never
have one. Until #8515, `releases/latest` — the exact endpoint every updater
resolves against — named that Release from the instant it was created, so every
host that ticked during the window concluded its platform was unbuilt (soft
path: a needless source rebuild; forced `--fetch`: a hard failure whose message
was identical to a genuinely missing artifact).

### The Latest pointer waits for the uploads

The Release is now created with **`--latest=false`**, and a `promote-release`
job — `needs: [resolve, build-daemon]`, so it runs only once *every* matrix leg
has uploaded successfully — marks it Latest afterwards. During the window
`releases/latest` therefore keeps naming the previous, **complete** Release, and
an updater that ticks mid-matrix resolves that one, finds it is not newer than
what it already runs, and no-ops.

Deliberately **not** a draft Release: the new Release, its tag and its notes are
published and fetchable by exact tag the whole time — only the repository's
Latest pointer waits. And if a platform's build or upload fails, the promotion
is skipped (the workflow is red), which leaves Latest on the previous complete
Release instead of advancing it onto a broken one. A draft would instead risk a
Release that is never published at all, invisible to everyone, on any single-leg
failure.

### …but the pointer never moves backward

Deferring the pointer means setting it *imperatively*: `gh release edit --latest`
sends `make_latest=true`, which pins **that** release as Latest regardless of its
creation date — unlike the `legacy`, date-ordered default it replaces. That
matters because release runs overlap. The workflow's `concurrency` group is keyed
per tag/SHA with `cancel-in-progress: false`, so runs for *different* releases are
explicitly allowed to run at once, and do: v0.19.295 ran 18:37:25Z → 18:52:30Z
while v0.19.296 started at 18:42:40Z. Start gaps of 5-20 min against 15-18 min
durations (macOS runner queueing can add much more) make "a run whose matrix is
slower than its successor's" an ordinary outcome, not an exotic one — and its
promotion would land *last*, dragging Latest back onto an older release and
rolling every host below it down a version until the next bump.

So `promote-release` reads the pointer before it writes: if `releases/latest`
already names a **newer** release, the promotion is skipped, logged to the step
summary as an intentional no-op, and the job exits **0**. Nothing is wrong in that
state — a newer *complete* Release is already Latest, which is exactly where the
fleet should be. The same comparison relaxes the post-write read-back to "`$TAG`
**or newer**", so a concurrent run promoting past us in the gap between our write
and our read is not a red run either (a false red on a healthy state is the
failure mode [`ci-principles.md`](ci-principles.md) exists to prevent). Anything
*older* than `$TAG` on the read-back is still a hard failure.

"Newer" is conservative by construction: either creation date (what `legacy`
ordered by, and what the race actually inverts) **or** semantic version claiming
the incumbent is newer holds the pointer where it is. A promotion skipped when it
could have run costs one release cycle of staleness and self-heals on the next
bump; a promotion made when it should not have been is the regression the check
exists to prevent.

### The resolver still says which case it is in

Promotion closes the window for *this* repo's own automated releases. It does
not cover a hand-cut Release (`/repo:release`, the `release` event — the
author's own Latest choice is honoured, never overridden here), a consumer repo
on an older workflow, or a platform that is genuinely unbuilt. So the daemon's
resolver (`loom-daemon/src/release_resolve/resolve.rs`) still reads the
release's `publishedAt` and asset count on the failure path and reports which
case it is in:

- Inside the upload window (`ASSET_UPLOAD_GRACE_MINUTES`, 60m): *"… the release
  was published 4m ago and it publishes no assets at all yet — its per-target
  assets are most likely STILL UPLOADING"*.
- Outside it: *"… published 3d 4h ago and it publishes 6 asset(s), none matching
  this target, well past the 60m upload window — this platform looks genuinely
  unbuilt rather than mid-upload"*.
- Publish time unreadable (an older `gh`): said as unknown. An unknown age is
  **never** reported as transient — nothing would bound the claim.
- Asset list unreadable: reported as unreadable, which is a different fact from
  "the release publishes nothing yet".

Resolution still fails in every one of those cases, so the tick falls back to a
source build exactly as before and an over-generous window cannot mask a
genuinely unbuilt platform — the window changes only the **wording** of a
refusal, never its outcome.

`loom-daemon-update.sh`'s own resolver (the shell twin `--fetch` uses) still
emits the flat, pre-#8515 wording: it is a `contract`-category script, frozen by
both the file-size ratchet and the portable shell budget, so the classification
cannot be added there without first porting the resolution behind the daemon.
Tracked separately in
[#8654](https://github.com/rjwalters/loom/issues/8654).

## See also

- `CLAUDE.md` § "Forge Authentication & Releasing" — how `/repo:release` works
  and what it publishes.
- [`.loom/docs/daemon-reference.md`](daemon-reference.md) — daemon self-update
  wrapper scripts (`loom-daemon-start.sh` / `loom-daemon-update.sh`) and how
  they fit the update lifecycle.
- Issue [#6010](https://github.com/rjwalters/loom/issues/6010) — the incident
  and acceptance criteria this doc satisfies.
- Issue [#8515](https://github.com/rjwalters/loom/issues/8515) — the
  create-before-upload visibility race, the deferred Latest pointer, and the
  age/asset-count reporting above.
- Issue [#8290](https://github.com/rjwalters/loom/issues/8290) — the one-off
  `gh release create` HTTP 403 that motivated the retry logic and the
  "accepted gap" decision above.
