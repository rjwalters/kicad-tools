# CI Principles: Dumb and Reliable

CI in this repo is deliberately unclever. This document records why, because
every mechanism removed here was individually justified when it was added, and
the next person to add one will have an equally good argument.

## The rules

1. **Prefer a slow correct job to a clever fast one.** Runner minutes are
   cheaper than an unverified merge, and far cheaper than a misattributed
   failure that costs an agent a triage cycle.

2. **Never cancel verification of a distinct commit once it has started.**
   Superseding is correct on a PR branch, where a newer push replaces an
   older one and the older result is worthless. On the default branch every
   commit is distinct work, so a started `main` run is never cancelled.
   Bounding the *queue* is the one exception (#9608): `main` shares one
   concurrency group with `cancel-in-progress: false`, so at most one run is
   in progress and one waits, and a newer push supersedes only a run that has
   not started. The tip is always verified. What a merge burst loses is the
   per-commit result for the intermediate commits that never started; if the
   tip is red, bisect across the burst to find the culprit. Without the bound,
   a burst of ~45 merges queued ~30 full runs and left the tip unverified for
   hours.

   Measured on `main` `CI` push runs (Actions API), #9619 merged 2026-09-30
   04:20 UTC:

   | Window | Runs | Peak overlapping runs | Push → done, p50 / p90 |
   |---|---|---|---|
   | 09-28 16:00 → 09-30 04:20 (before) | 198 | 32 | 16 min / 64 min |
   | 09-30 04:20 → 16:00 (after) | 76 | 3 | 8 min / 12 min |

   The price is visible and expected: about a third of `main` runs now conclude
   `cancelled`. A `cancelled` `main` run is **no verdict**, neither a pass nor
   a rule violation. `main_health_gate.rs` reduces it to `Unknown`, and
   `work_finder/main_red_fix.rs` counts only `failure`/`timed_out`/
   `startup_failure`. Do not re-run it to "complete" the record, and do not
   treat it as a skipped check.

   Releases follow the same verdict: `release.yml` releases only a `main`
   commit whose `CI` run concluded `success` (`workflow_run`, #10826). A
   `cancelled` or red commit is never released; its version may be skipped
   ([release-cadence](release-cadence.md)).

   Tell the two kinds of `cancelled` apart before reading a high rate as a
   regression (#10670): a run the bound superseded while *pending* has no
   jobs; a run cancelled after it *started* has some, and that one breaks
   this rule. On 2026-10-06 54 of the last 100 `main` runs were cancelled and
   every one was job-less. `signoz/ci-queries.sql` section 18 reports the
   split (`cancelled_after_start` must stay 0), and loom-daemon's
   `merge_group_ci::main_cancel` lint fails CI if any workflow change could
   cancel a started `push` or `merge_group` run: a `cancel-in-progress` that
   is not provably false there, a group shared with a cancelling PR run, or a
   run-cancelling step reachable there. The lint runs on every PR that
   touches any `.github/workflows/**` file (ci.yml's `Workflow Cancellation
   Lint` job), not only on Rust changes. One gap is known: concurrency groups
   are repo-wide, but the lint compares groups only within one workflow, so
   two *different* workflows sharing a literal group (one on `push`, one
   cancelling on `pull_request`) is not detected. Keep every group prefixed
   with its workflow's name, as all current ones are.

3. **Path-filtering is an optimisation, not a correctness tool.** A check that
   can fail because of a file *outside* its path group must not be filtered by
   path. `conflict-markers` states this in its own comment and is right:
   "must NOT be path-filtered — the corruption can land in any file, including
   one whose path group the PR did not otherwise touch."

   A push to `main` runs every job with no path filter, with **one
   operator-approved exception** (#10825, 2026-10-07): the image jobs
   (`worker-base-image` and the three `*-image-smoke` jobs). On push they run
   only when `changes-push` finds an image input in the diff, as defined by
   `scripts/ci-image-inputs.sh`, the one definition the PR filter shares.
   Image inputs are every path `.dockerignore` re-includes, `.dockerignore`
   itself, `ci.yml`, the run-job seam and the script. The diff base is the
   head of the last **green** `CI` push run on `main`, never
   `github.event.before`. In a burst, `before` can be a commit whose run was
   superseded (rule 2), and after a red smoke the next clean diff would go
   green over it (rule 6). The filter fails **closed**: no green base, a base
   that is not an ancestor, a failed or truncated file list, or a failed or
   cancelled `changes-push` all run the image jobs, and the last two also
   turn `CI Result` red. The loom-daemon **source** is deliberately not an
   image input, though the images ship it. That observation is owed to
   `ci-daily.yml`'s `docker-smokes`, which builds the daemon from the commit
   and runs every smoke unfiltered (rule 10). Adding a second push-time
   filter needs its own operator approval.

4. **One mechanism per behaviour.** If two mechanisms implement cancellation,
   skipping, or retry, their interaction is owned by nobody and will surprise
   someone. Prefer fixing the one that exists to adding a second that covers
   its gap.

5. **A workaround for a platform quirk is scoped to the case that motivated
   it**, and names the quirk. A fix for "a force-pushed PR leaves a queued run
   holding the group" belongs on pull-request events, not on every ref.

6. **A check that cannot run must not look like a check that passed.** This is
   the same rule the guard and resync layers learned the hard way (#7745,
   #7761): `exit 0` has to mean "verified", never "skipped".

7. **Split work across runners by moving it, never by filtering it.** When a
   job is the long pole (#9065), divide it so every piece still runs exactly
   once. Move whole steps into a sibling job, or use nextest's deterministic
   `--partition count:k/N`, under which each test lands in exactly one leg and
   the legs' "N tests run" lines sum to the unpartitioned total. Do not use a
   filterset that picks "the tests that matter": that is path-filtering by
   another name (rule 3). Hand a binary between jobs only when it measurably
   wins. The debug daemon qualifies: its consumers dropped from 100-130s of
   compiling to 9-24s. A consumer whose producer failed must turn red rather
   than be skipped (`if: ${{ !cancelled() }}` plus a download that fails when
   the artifact is missing), because a skipped required check counts as
   passing (rule 6).

   The aggregate backstop is the `CI Result` job (#10444): `if: always()`,
   `needs:` every job, and it fails when `Detect Changes` is not `success` on
   a PR (a cancelled / never-acquired runner otherwise skips every filtered
   job while the always-run required checks stay green, as on #10403), when
   any job failed or was cancelled, or when a job was skipped only because its
   upstream did not succeed. A skip the path filter decided still passes. The
   rule lives in `scripts/ci-result-gate.sh`; `merge-pr.sh` independently
   refuses a head whose latest `CI` run is not `success` and names the
   cancelled jobs. Remedy: `gh run rerun --failed <run>` (in place, never a
   cancel). `CI Result` becomes a required context only after it has reported
   on `main`; that ruleset change is an operator step.

8. **Group required gates by component, and judge each component on its own
   inputs.** Many tiny required jobs compete for the concurrent-job cap, so
   #9065 folded 19 of them into `Structural Checks`, `Daemon Checks` and the
   macOS `Shell Syntax` leg. Each gate keeps a `# component: <name>` marker
   and its own input spec. The freshness guard calls a composite stale when
   *any* component is stale, never on the union of their inputs, so grouping
   does not make merges go stale more often. Every step runs under
   `!cancelled()`, so one red gate cannot hide another.

9. **Narrowing what a *result* covers is not path-filtering what a *check*
   runs.** Rule 3 forbids deciding whether to RUN a check from a path set.
   The freshness guard asks a different question — "can merging this PR turn
   a check that already ran and passed red?" — and answering it needs the
   input set the check actually read (`merge_pr/stale_checks/inputs.rs`).
   Three narrowings live there, and both must obey the same three constraints:
   **derive the scope from the repo's own text, never a hand-maintained
   second copy**; **keep it per-file, never per-commit**; and **fail closed
   to the broader answer on any doubt**.
   - *Machine restamps* (#8919, #9065): the release version bump and the
     `chore: resync installed Loom surfaces` metadata stamp are discounted
     from the base-move diff, per FILE and only for lines matching each
     field's pinned value shape. A resync that also rewrote an installed
     surface leaves that surface in the diff.
   - *`ci.yml` block attribution* (#9065): one path covers ~25 jobs of which
     three are required, so a change set's `ci.yml` hunks are attributed to
     the job and `# component:` block they edit, read from the workflow's own
     text. **Both sides** are attributed, each against the tree its patch
     diffs *to* — the base tip for the base move, the PR head for the PR's
     own delta — and each independently, so one side's doubt never narrows
     the other. A preamble edit, a structural deletion, an unparseable hunk,
     a touched line past the file's end, or markers that disagree with the
     guard's component table all restore the whole-file meaning on that side.
     Nothing is skipped and no check's coverage narrows — every gate still
     runs on every PR.
   - *Per-repo declarations* (#9589): a consumer repo's required contexts are
     absent from loom's table, so each is stale on any base move unless the
     repo declares its inputs in `.loom/stale-check-inputs.json`
     ([stale-check-inputs](stale-check-inputs.md)). The declaration is the
     repo's own text, read from the base tip, and is itself a global input of
     every check it declares. An unlisted context gets no guessed default, and
     any malformed or unreadable file is ignored with a warning.

10. **Every speed trade-off on the merge gate is owed a slow run somewhere
    else.** Rules 7 and 8 make a fast gate legitimate; they do not make it
    sufficient. Partitioning, sharding, a shared debug build, a restored
    cache and a path filter each remove a *class* of observation, not just
    wall time — a cross-partition test interaction, a non-hermetic suite, a
    release-profile-only break, a cold-build break, an unedited-path break.
    The daily backstop below is where each of those is paid back. When the
    next optimisation lands on `ci.yml`, the question is not "is this safe on
    its own" but "which run still makes the observation this removes".

11. **A merge-group run is held to the same rules as a `main` run** (#10257).
    Each merge group is a distinct commit, the combined tree that will land.
    Its run is never cancelled once started (rule 2). Its concurrency group is
    keyed on its own head SHA, so no other run can supersede it while it is
    still pending. Without that key the queue would wait on a check that never
    reports. A suite that is skipped on `merge_group` is not coverage
    (rule 6), and path filters do not apply there (rule 3). `merge_group` runs
    the full suite: everything `push` runs, plus the image jobs that `push`
    path-filters under rule 3's one exception. `loom-daemon merge-group-ci audit` checks all of
    this statically, and it counts any suite it cannot prove runs as
    uncovered. See [merge-queue-ci](merge-queue-ci.md).

## The daily backstop and its tracking issues

`.github/workflows/ci-daily.yml` (#9085) is the slow run rule 10 requires. It
is scheduled, never required, never path-filtered, and never cancelled — its
concurrency group is keyed on `github.run_id`, which is unique per run, so a
manual dispatch racing the scheduled one cannot supersede it (rule 2). It
undoes each of `ci.yml`'s speed trade-offs: an unpartitioned full run with no
`rust-cache` and an explicit assertion that `target/` did not exist before the
first build, the whole release target matrix minus signing, every
`package-lock.json`, `cargo deny` against `deny.toml`, the shell suites at
parallelism 1 and 8 and on macOS/bash 3.2, `nextest` three times over for
flakes, Rust beta, and the Docker smokes without their path filter.

One job is not a speed trade-off paid back but a proof `ci.yml` cannot make
(#10868): `Compatibility Floor (SUPPORTS_INSTALLED)` runs the compatibility
contract's direction A against the release `SUPPORTS_INSTALLED` names, where
`ci.yml` tests only the release just before the change
([`release-cadence.md`](release-cadence.md), "Compatibility contract").

The full **default-feature** suite runs only here (#10823): `ci.yml`'s
`Rust Unit Tests` legs run the whole workspace with `--features
loom-daemon/otlp` (the shipped configuration), and the PR gate adds only a
targeted `--lib` step for the `#[cfg(not(feature = "otlp"))]` tests, whose
names it derives from source.

**A red daily run nobody reads is worse than none**, because it trains people
to ignore red. So the `report` job is part of the mechanism, not a nicety:

- **One tracking issue per failing job**, never per run and never per failing
  step. Title is exactly `[ci-daily] <job name>`; the lookup is an exact
  title match against **all** open issues (not a label query), so a second
  consecutive failure comments on the existing issue instead of opening a
  duplicate, however the issue has been relabelled since. Matrix legs are
  distinct jobs and get distinct issues.
- **The next green run for that same job closes it**, with a comment naming
  the run that went green.
- **Only `failure` and `timed_out` open an issue, and only `success` closes
  one.** `skipped`, `cancelled` and `neutral` leave any open issue exactly as
  it is — rule 6 applied to the reporter itself: a job that did not run is
  not a job that passed, and must not close a standing failure.
- **`(warn-only)` in a job's name is a contract**, not decoration: the
  reporter never files for such a job. `continue-on-error: true` alone is not
  enough, because the job still reports a `failure` conclusion to the jobs
  API. Rust beta is the only job that carries it today — an upcoming-toolchain
  regression is information, not a defect in this repo.
- **The tracking issues use an existing label, never a new one.** They are
  filed with `loom:triage` — the ordinary intake label — so they enter the
  normal pipeline like any other filed issue. The workflow creates no labels:
  the label set is intentional and `.github/labels.yml` is authoritative.
  Identity lives in the title, which is why the lookup ignores labels and the
  issue body asks that the title be left unchanged.

SigNoz needs nothing workflow-side: `loom-daemon ci-telemetry` captures every
run of every workflow of the configured owners by auto-discovery, so the daily
run's durations, outcomes and logs are queryable from the day it lands
([`ci-observability.md`](ci-observability.md)).

## What prompted this

Three failures on 2026-09-15, all from cleverness that reviewed well.

### `cancel-superseded` destroyed the default branch's verification

#6683 added a belt-and-suspenders cancellation job, because GitHub's native
`cancel-in-progress` does not reliably cancel a run that is still `queued` —
observed live, a PR sat pending for ~20 minutes. Real problem, reasonable fix.

The job cancels other runs on the same ref, resolved as
`github.head_ref || github.ref_name`. On a push, `head_ref` is empty, so the
ref is `main`, and **every merge cancelled the previous merge's run**.

Measured before the fix: of the last 30 `main` runs, **22 cancelled, 7
succeeded** (#7779, PR #7803).

The second-order cost exceeded the first. *"Does this also fail on `main`?"* is
the first question in triaging a red check and the one-step way to separate a
regression from a flake. For weeks it had no answer, which pushed the cost onto
every PR author and made the flake categorisation in #7789 rest on reading
issue titles rather than run data.

### Two cancellation mechanisms compounded invisibly

The `concurrency:` stanza and the job above each looked correct in isolation.
The issue that reported the symptom blamed the stanza — the obvious culprit —
and fixing only that would have changed nothing, because the job was the
dominant cause. Nobody owned the interaction. Hence rule 4.

### Path-filtered skipping obscures attribution

The `changes` job skips jobs by file group. That is a real saving, and it is
also why a red check no longer reliably means "caused by this PR". Combined
with the flake load in #7789, the signal degrades to "something, somewhere,
possibly yours" — and a signal nobody believes is one nobody reads.

## The failure mode this document exists to prevent

Every mechanism above was added deliberately, by someone solving a real
problem, and would pass review again today on its own merits. The aggregate is
what failed. A principle recorded up front is cheaper than re-litigating each
addition, and much cheaper than discovering the interaction months later from
a cancellation rate.

## Related

- #7779 / PR #7803 — the cancellation fix; #9608 — the bounded `main` queue
  (one running + newest pending) that rule 2 now allows
- #10670 — the `main_cancel` lint and the superseded-vs-started cancel split
- #7789 / #7791 — flake tracking, and why retry must *record* rather than hide
- #7745 / #7761 — the same "a skipped check must not read as a pass" rule,
  learned in the resync and guard layers
- #8248 / #8919 / #9065 — the required-check freshness guard, its input-scoped
  predicate, and rule 9's two narrowings
- #9065 / #9069 — the wall-time work rule 10 exists to balance, and #9085 —
  `ci-daily.yml`, the slow run that balances it
- #10826 — releases only from a green `main` `CI` run, at its `head_sha`
  ([release-cadence](release-cadence.md))
- #10257 — merge-queue qualification: rule 11, the `merge_group` trigger in
  `ci.yml`, and the audit that proves it ([merge-queue-ci](merge-queue-ci.md))
- [`ci-observability.md`](ci-observability.md) — the observability face of
  the same family: every run, job, duration, outcome and log is captured in
  SigNoz as standing policy, so a regression like #7779's cancellation storm
  is visible over time instead of weeks later (#8827)
