# Release cadence vs. `VERSION`

`VERSION` (and the other five `scripts/version.sh`-managed files, #5517) bumps
after nearly every merge to `main` that touches `defaults/`
(`version-bump-on-merge.yml`, #7743) — it tracks the tree, not a release. A
GitHub Release is a **separate event with its own rule**, stated below, and
not every `VERSION` gets one. This doc states that rule and what it means for
the signed-artifact `--fetch` path (Epic #4990 Phase 3, #5009/#5018/#5020) and
for everything that reads the release list.

## The decision: a release is a green `main` commit with an unreleased `VERSION` (#10826)

`.github/workflows/release.yml` releases automatically, and **only** from a
`main` commit whose `CI` run went green:

- **Trigger.** `workflow_run` on the `CI` workflow (`ci.yml`) completing. The
  `resolve` job runs only when that CI run concluded `success`, was a `push`
  (never a PR or merge-queue run), ran on `main`, and came from this
  repository. A `main` commit whose CI **failed**, was **cancelled** (superseded
  by a newer pending run — about a third of `main` runs, by design, see
  [`ci-principles.md`](ci-principles.md)) or **never ran** is never released.
- **What is released.** The CI run's `head_sha` — the commit CI tested — never
  `main`'s current tip. `VERSION` is read from that commit's tree, the tag
  `v<VERSION>` is created at that commit, and every build job checks the tag
  out, so the tag, the binary's `--version` (`Cargo.toml` at that tree) and the
  tested tree are the same thing.
- **When it publishes** (`scripts/release-decision.sh`, unit-tested by
  `scripts/test-release-decision.sh`): only if no tag or Release `v<VERSION>`
  exists yet, no **newer** version has already been released (a late re-run of
  an old green CI run never publishes an older version after a newer one), and
  `head_sha` is still reachable from `main` (rewritten history never
  publishes an orphan). Otherwise the run records `SKIPPED: <reason>` in its
  summary and builds nothing. Missing facts (an unreadable compare/tag list)
  fail the run red rather than release on a guess.
- **Concurrency.** Automated runs share one group, `release-main`, with
  `cancel-in-progress: false`: one release builds at a time and only the
  newest pending run waits. A burst of green `main` runs therefore costs at
  most two release builds — the oldest running, the newest pending — and the
  pending runs it displaced show as `cancelled` (their versions are skipped,
  which is intended: the newer commit carries the newer `VERSION`). Before
  #10826 each bump had its own group, so runs overlapped freely. A hand-cut
  Release (`/repo:release`, the `release` event) and a `workflow_dispatch` dry
  run keep their per-tag groups and are unchanged.

### Versions are assigned at merge; tags may skip versions

`version-bump-on-merge.yml` is unchanged: a bump commit still assigns the
next patch version right after the merge. That version is released only if
the bump commit, or a later `main` commit still carrying the same unreleased
`VERSION`, finishes green. Examples:

- Bump commit B's CI is superseded by merge M2's pending run. M2 goes green
  still carrying B's `VERSION`, so `v<VERSION>` is released **at M2** — the
  tree that was tested.
- Two bumps land in one burst and only the newer one's CI goes green: the
  older version never gets a tag. That gap is expected.
- `main`'s tip is red: nothing is released until a later commit is green.

Assigning the number at release time instead was rejected: the release
workflow would have to push a commit to `main` (App token, ruleset bypass),
racing merges and re-triggering CI and the bump; and the shipped binary
reports `CARGO_PKG_VERSION` from the tree it was built from, so the tag can
equal the binary's version only if the number is already in the tested tree.

### Who reads the release list, and why gaps are fine

- **`release_resolve` / auto-update** resolve `releases/latest`, which only
  `promote-release` moves once every platform has uploaded (#8515). They never
  walk tags or assume contiguous versions.
- **The fleet floor** (`loom_min_version`, #10698) is satisfied by the newest
  release at or above it, so a floor equal to a skipped or still-unreleased
  `VERSION` is met by the next release. New consequence: a red or backed-up
  `main` now delays the release that satisfies a floor. Set the floor from the
  published release list (`gh release list`), not from `VERSION` in the tree.
- **Release notes** (`--generate-notes`) diff against the previous tag, so a
  skipped version's changes appear in the next release's notes.
- **The `Release gap`** reported by `loom-daemon-update.sh --check` (below)
  compares the newest release with the source tree's `VERSION`; it now also
  grows while `main` CI is red or still running, which is accurate.

A manual `/repo:release` (see `CLAUDE.md` § "Forge Authentication &
Releasing") still works and is still the rollback path: it fires the
`release` event, which this workflow builds exactly as before.

## What this means for `--fetch`

`defaults/scripts/cli/loom-daemon-update.sh`'s `--fetch` (force) mode resolves
the newest GitHub Release and hard-fails rather than silently falling back to
a source build when no usable artifact resolves — by design (Epic #4990 Phase
3b). A source tree is usually a few commits ahead of the newest release (its
`main` CI has not finished, or its `VERSION` was skipped), so **`--fetch`
reaches the newest green release, not necessarily the tree you are on**. When
you need an exact unreleased tree, build from source
(`loom-daemon-update.sh --no-fetch`).

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
   nearly every merge). A later run resolves a *different*, newer tag, and
   once a newer version is released an older one is never published (#10826) — it has
   no way to notice or recreate a specific *earlier* version's missing
   Release without extra state (e.g. diffing the full tag history against
   Releases on every run just to catch a rare one-off). That mechanism would
   run on every single push to guard against a failure mode observed exactly
   once in this repo's history, at a cost (extra `gh` calls, more surface
   area to go wrong) out of proportion to the problem.
2. **The cadence rule (above) already tolerates a release-vs-`VERSION` gap
   as normal** — a version whose CI never went green is skipped by design
   (#10826). A single missing Release from a transient failure is
   indistinguishable, downstream, from that common case: the next green
   commit's Release supersedes it either way, and `--fetch`/`--check`'s gap
   reporting (above) is already keyed off "newest release vs. current source
   `VERSION`", not off "does every past version have a Release" — so it does
   not regress from this.

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
matters because release runs can overlap. Until #10826 the workflow's
`concurrency` group was keyed per tag/SHA, so runs for *different* releases ran
at once, and did: v0.19.295 ran 18:37:25Z → 18:52:30Z while v0.19.296 started at
18:42:40Z. Automated runs now share the one `release-main` group and no longer
overlap each other, but a hand-cut `release` run keeps its own group and can
still overlap an automated one, so the guard below stays. Start gaps of 5-20 min against 15-18 min
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

## Compatibility contract (#10716)

Each release declares what its two halves need from each other (tracker
[#10698](https://github.com/rjwalters/loom/issues/10698), D3/D4). Both values
are constants in `loom-daemon/src/install_compat.rs`:

| Constant | Meaning | Recorded where |
|---|---|---|
| `SUPPORTS_INSTALLED` | the oldest installed Loom (`loom_version`) this daemon works with | in the daemon only |
| `REQUIRES_DAEMON` | the oldest daemon the installed files this release ships work with | `.loom/install-metadata.json` `requires_daemon`, written by `loom-daemon init` and `scripts/install-loom.sh` |

**They move only when a change breaks compatibility**, in the PR that breaks
it, never at release time:

- Raise `REQUIRES_DAEMON` when shipped shell starts needing something an older
  daemon lacks: a new `loom-daemon` subcommand, or a hard
  `# requires-daemon: <sub> >= <version>` floor above the current value. Name
  a published release, or the version this change ships as (`VERSION` + 1
  patch) when the dependency lands in the same PR. The value is a floor, not
  a tag: releases skip versions (above), so `v<REQUIRES_DAEMON>` may never be
  published, and CI proves the claim against the oldest published release at
  or above it.
- Raise `SUPPORTS_INSTALLED` when the daemon stops working with older
  installed files: it starts executing an installed file that older releases
  do not ship, or relies on a changed argument contract. Add any newly
  executed file to `DAEMON_INVOKED_INSTALLED_FILES` in the same PR.

The `Compatibility contract across adjacent releases` step of CI's
`Install Surface Checks` job (`loom-daemon install-compat check`) proves both
claims. It runs the previous release's installed files against the new daemon,
and the new installed files against the oldest published release at or above
`REQUIRES_DAEMON` (`--fetch-old-daemon`; a release whose assets are still
uploading is skipped). While no such release is published, which is the PR
that raises the value and `main` until the next release, the new daemon stands
in for it. It fails when a claim is violated. To try a proposed value before
changing the constant, run it locally with `--requires-daemon <v>` /
`--supports-installed <v>` and `--fetch-old-daemon` (or `--old-daemon <that
release's binary>`). `loom-daemon install-compat show --repo <clone>` prints
both sides for one repo and how they classify.

Three things about that step's verdict (#10868):

- **A release still uploading is skipped, and the output says so.** The note
  names each tag passed over (`release v<x> is tagged but its assets are not
  uploaded yet`). A listed asset that then fails to download is retried, three
  attempts five seconds apart, before the step fails naming the tag and the
  asset. An asset listing the forge could not answer still fails the step.
- **A daemon that crashes is a violation, not a pass.** A subcommand is
  missing only when clap refuses it. A probe binary that cannot be executed,
  is killed by a signal or panics is reported as broken, with the binary, the
  subcommand and the exit status. It is never counted as having the
  subcommand.
- **The floor check also runs as a unit test.**
  `no_shipped_hard_floor_is_above_requires_daemon`
  (`loom-daemon/src/install_compat/tests.rs`) walks `defaults/` with the
  harness's own code and fails when a hard `# requires-daemon:` floor is above
  `REQUIRES_DAEMON`. It catches a floor that is too high, never one that is
  too low.

### The daily proof of `SUPPORTS_INSTALLED`

The CI step tests one release in direction A: the newest tag at or below
`VERSION`. So nothing in the merge gate runs the release `SUPPORTS_INSTALLED`
itself names. The `Compatibility Floor (SUPPORTS_INSTALLED)` job of
`.github/workflows/ci-daily.yml` does, once a day:

```bash
loom-daemon install-compat check --prev-ref "v<SUPPORTS_INSTALLED>" --fetch-old-daemon
```

It reads the value from the built daemon (`install-compat show --json`), so
moving the constant needs no workflow edit. A failure opens the
`[ci-daily] Compatibility Floor (SUPPORTS_INSTALLED)` tracking issue.

When it fails the claim is false, and there are two ways to make it true:

- **Raise `SUPPORTS_INSTALLED`** to the oldest release for which the command
  above passes (try one with `--supports-installed <v> --prev-ref v<v>`), and
  update the constant's comment. This is the default. What it means: once the
  dispatch hold ([#10719](https://github.com/rjwalters/loom/issues/10719))
  acts on the value, a repo installed below it is held until it is resynced.
  Fleet repos are resynced by the daemon; a repo outside the fleet that old
  needs a manual resync.
- **Keep the value** and restore in the daemon whatever the old installed
  shell needs. Choose this when the gap is a regression, not an intended
  break.

`SUPPORTS_INSTALLED` must name a release tag: the job fails when `v<value>`
does not exist, because then nothing can be run against it.

## See also

- `CLAUDE.md` § "Forge Authentication & Releasing" — how `/repo:release` works
  and what it publishes.
- [`.loom/docs/daemon-reference.md`](daemon-reference.md) — daemon self-update
  wrapper scripts (`loom-daemon-start.sh` / `loom-daemon-update.sh`) and how
  they fit the update lifecycle.
- [`.loom/docs/release-glibc-floor.md`](release-glibc-floor.md) — the decided
  minimum glibc `build-daemon`'s Linux artifacts must run against, and why
  the build host is pinned rather than left on `ubuntu-latest`.
- Issue [#6010](https://github.com/rjwalters/loom/issues/6010) — the original
  release-vs-`VERSION` gap and the `--check` visibility it added.
- Issue [#8515](https://github.com/rjwalters/loom/issues/8515) — the
  create-before-upload visibility race, the deferred Latest pointer, and the
  age/asset-count reporting above.
- Issue [#10826](https://github.com/rjwalters/loom/issues/10826) — release only
  from a green `main` commit (`workflow_run` on `CI`), `release-main`
  concurrency, and versions skipped by design.
- Issue [#8290](https://github.com/rjwalters/loom/issues/8290) — the one-off
  `gh release create` HTTP 403 that motivated the retry logic and the
  "accepted gap" decision above.
