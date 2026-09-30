# PR latency by segment: the canonical question set

The standing answer to *"a PR exists — where does the time before it merges
actually go?"* — defined **once**, here, so that a number quoted in an issue, a
Champion digest, or a sweep advisory means the same thing in all three (Issue
#8923).

Every question `PL<n>` below is implemented by `loom-daemon pr-latency`, and
[`loom-daemon/tests/pr_latency_artifacts.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/tests/pr_latency_artifacts.rs)
fails if this doc and the implementation disagree about the set.

```bash
loom-daemon pr-latency                      # the full decomposition
loom-daemon pr-latency --json               # the same, as one document
loom-daemon pr-latency --advise             # live queues only, warn past a threshold
```

## Why this is not the cycle-time rollup

[`cycle-time-questions.md`](https://github.com/rjwalters/loom/blob/main/defaults/observability/cycle-time-questions.md) (#8665)
answers *"what took long to **ship**?"* from the telemetry stream, and its own
"what this cannot answer" section rules out precisely this question:

> **"How long from issue *filed* → curated → built → reviewed → merged?"** The
> telemetry stream starts at `sweep.started`; nothing in it carries the forge's
> `created_at` for the issue, nor the times a label moved.

There is **no durable label-transition store** anywhere in Loom. `forge_events`
is a read-only wake signal, not a history. So every figure here is derived
**live** from the forge's own `issues/<n>/timeline` on each run — the same shape
`claim_reconciliation` already uses to decide verdict staleness. Building a
transitions store first would have been a materially larger change than
"measure", and #8923 says *measure first, then act*.

**Consequences of deriving live, stated plainly:**

- It is a **snapshot**. Re-run it; never quote an old one. Every dwell is
  relative to `measured_at`.
- It costs one `gh pr list` plus one `gh api --paginate` per PR. Fine on demand,
  **not** something to put on a tick.
- It can only see what the timeline still shows. A PR whose timeline read fails
  is reported as *unmeasured* and excluded from every distribution.

## Definitions (fix these before reading any number)

| Term | Definition |
| --- | --- |
| **dwell** | Time since the PR's **current queue label was applied**. This is the unit of every live figure. |
| **age** | Time since the PR was **opened**. Reported beside dwell and never instead of it. |
| **segment** | A closed interval between two *observed* events on one PR. A PR still waiting contributes no segment sample — it appears in the live queue view instead. |
| **lap** | One traversal of review → verdict. A PR may have several; they are kept as separate samples, never averaged into one. |
| **verdict** | A `labeled loom:pr` or `labeled loom:changes-requested` event. A verdict *removal* is not a verdict. |
| **operator gate** | Any of `loom:operator`, `loom:operator-only`, `loom:needs-capability` — the labels that mean a **person** is the only transition out. The four `loom:operator-*` sub-kinds always accompany `loom:operator-only`, so they are shown as detail rather than counted as gates. |
| **parked** | `work_finder::PARK_LABELS` (`loom:blocked`, `loom:operator-only`). Reused, never re-listed: a park set that drifted from the work finder's own would report a PR as reachable when nothing can reach it. |
| **absent vs. zero** | An unmeasured segment is `None` and prints `—`, **never** `0`. This is the single most important rule here, because a `0` in a latency table reads as *instant*. |
| **push** | A `committed` or `head_ref_force_pushed` timeline entry. See the caveat below. |

### Dwell is not age — the mistake this exists to prevent

#8923's own problem statement reported the `loom:changes-requested` queue as
"median age 89.6h" and read it as a Doctor-throughput failure. Re-measured as
**dwell**, the same ten PRs were: two actively being treated, three rejected
four hours earlier, and five parked under `loom:blocked`. There was no aged
Doctor backlog at all. The number was real; the quantity was wrong.

Every live figure this tool emits is therefore dwell, and `age` is printed in the
adjacent column so the two can never again be quoted as one.

## The questions

| ID | Question | Sample | Reads |
| --- | --- | --- | --- |
| **PL1** | *How long does a PR wait for a verdict?* `loom:review-requested` → the next verdict, one sample **per lap**. | per lap | label events |
| **PL2** | *What does the approval path cost?* first verdict → `loom:pr`, for PRs that reached approval. `Some(0)` when approved first time; absent when never approved. | per PR | label events |
| **PL3a** | *How long does an approved PR wait to merge **under an operator gate**?* | per merged PR | labels + merge |
| **PL3b** | *…and with **no** gate?* The split is the point: #8923's hypothesis was that the gate, not the merge machinery, is the dominant term. Comparing PL3a to PL3b answers it. | per merged PR | labels + merge |
| **PL4** | *How fast does Doctor answer a rejection?* `loom:changes-requested` → the next push, plus the count of laps that never got one. | per lap | labels + pushes |
| **PL5a** | *What do stale-SHA re-reviews cost?* **Approval invalidations**: the PR was approved and went back to `loom:review-requested`. A full extra Judge review of work that had already passed — the only extra lap that is pure waste. | per PR | label events |
| **PL5b** | *…versus the healthy loop?* **Repair laps**: review re-requested while the standing verdict was `loom:changes-requested`. Counted **separately** precisely so it cannot inflate PL5a. | per PR | label events |
| **PL6** | *What is waiting right now, and for how long?* The live queue view: every open PR in a queue, by **dwell**, with its gate/park/treating state. | per open PR | current labels |

Plus the **Doctor backlog** (acceptance criterion 3 of #8923): every open
`loom:changes-requested` PR with **no push since the label**, split by whether
Doctor is treating it, it is parked, or it is genuinely queued.

### Why PL5a and PL5b must not be one number

`claim_reconciliation::invalidate_verdict` clears a verdict label and re-applies
`loom:review-requested` when the head SHA has moved past the verdict's recorded
marker. That signature — *approved, then back to review* — is PL5a.

The **rejection** → fix → review-again loop has the same shape in the label log
and is the pipeline working correctly. Folding them together reports the healthy
loop as waste; on the 2026-09-26 sample it would have turned 34 genuine
invalidations into 53 and made the correct behaviour look like half the problem.

### The `committed`-date caveat

REST's issue timeline has no "pushed" event. A `committed` entry carries the
**commit object's committer date** — when the commit was written, not when it
reached the forge. For an agent that commits and immediately pushes (every Loom
role) the two coincide to within seconds. For a human who commits locally and
pushes the next day they do not, and the derived PL4 will read **low**.
`head_ref_force_pushed` *is* a true push time and is used as-is.

## What this question set cannot answer (and why)

- **Anything retroactive beyond the timeline.** There is no store; the answer is
  whatever the forge still serves. A PR outside `--limit` is simply not in the
  sample.
- **Why** a hold stands. The tool reports that a gated PR has been silent for N
  hours. Whether the hold is right is a human's call — see the mediation below.
- **Per-role attribution inside a segment.** PL1 measures the queue, not Judge's
  own working time; a gated PR is excluded from PL1's advisory precisely because
  Judge is not the one holding it.
- **Trends over time.** Every run is a snapshot. Persisting these segments into
  a rollup (so "is the queue getting slower?" becomes answerable) is deliberate
  future scope, not something this infers from one sample.

## The mediation: `--advise` (Phase 2)

`--advise` is the one mediation #8923's acceptance criteria call for, chosen by
what the Phase-1 numbers showed rather than by the guess in the original filing.
It reads **open PRs only**, warns to stderr about anything past
`--threshold-hours` (default 24), **always exits 0**, and never writes a label or
a comment — the same contract as the four sibling pre-wave checks
(`check-host-sleep`, `check-main-freshness`, `check-quarantine-stashes`,
`check-stale-blocked`).

It reports **four disjoint populations**, so each PR is named exactly once and
the total is a count of PRs rather than of findings:

| Population | Meaning |
| --- | --- |
| **Waiting on a person** | Operator-gated, in *any* queue. `loom:operator` is in `work_finder::SKIP_LABELS`, so the engine has stopped: no role will move these. |
| **Approved, nothing holding it** | `loom:pr`, no gate, no park. The strictly worse finding — nothing is waiting on a human, so the merge lane itself stalled. |
| **Awaiting a Judge verdict** | `loom:review-requested`, ungated, unparked. Genuinely Judge's queue. |
| **Rejected, no Doctor push** | `loom:changes-requested`, ungated, unparked, not `loom:treating`. Genuinely Doctor's queue. |

**The gate is not the defect; the silence is.** An operator gate on a finished PR
is a legitimate state that this must never pressure anyone out of. The advisory
exists so that state cannot be invisible for days — which is why it reports,
and stops there.

**A PR with an unknown dwell never fires the advisory.** If the label is present
but its `labeled` event is not on the (possibly truncated) timeline, the dwell is
`None`, and an advisory that fires on "we could not tell" is an advisory that
gets ignored.

### Why a gated PR is not counted against Judge or Doctor

Live data on 2026-09-26 contained the case that motivated this split: #8893 and
#8613 had sat for 11.1h carrying `loom:review-requested` **and** `loom:operator`.
Counting them as a Judge-queue stall would have blamed a role that is correctly
refusing to act on them — the same class of mis-attribution as reading age for
dwell. The gated rows are reported in full, under the population that can
actually clear them.
