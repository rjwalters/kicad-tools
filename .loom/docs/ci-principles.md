# CI Principles: Dumb and Reliable

CI in this repo is deliberately unclever. This document records why, because
every mechanism removed here was individually justified when it was added, and
the next person to add one will have an equally good argument.

## The rules

1. **Prefer a slow correct job to a clever fast one.** Runner minutes are
   cheaper than an unverified merge, and far cheaper than a misattributed
   failure that costs an agent a triage cycle.

2. **Never cancel verification of a distinct commit.** Superseding is correct
   on a PR branch, where a newer push replaces an older one and the older
   result is worthless. It is never correct on the default branch, where every
   commit is distinct work that nothing else will verify.

3. **Path-filtering is an optimisation, not a correctness tool.** A check that
   can fail because of a file *outside* its path group must not be filtered by
   path. `conflict-markers` states this in its own comment and is right:
   "must NOT be path-filtered — the corruption can land in any file, including
   one whose path group the PR did not otherwise touch."

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
   Two narrowings live there, and both must obey the same three constraints:
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

- #7779 / PR #7803 — the cancellation fix
- #7789 / #7791 — flake tracking, and why retry must *record* rather than hide
- #7745 / #7761 — the same "a skipped check must not read as a pass" rule,
  learned in the resync and guard layers
- #8248 / #8919 / #9065 — the required-check freshness guard, its input-scoped
  predicate, and rule 9's two narrowings
- [`ci-observability.md`](ci-observability.md) — the observability face of
  the same family: every run, job, duration, outcome and log is captured in
  SigNoz as standing policy, so a regression like #7779's cancellation storm
  is visible over time instead of weeks later (#8827)
