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
