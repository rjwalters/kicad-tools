# Champion promotion throughput (#10753)

**Load when**: you are tuning how fast Champion promotes, posting or acting on
a `NEEDS REVISION` verdict, or Curator finds a Champion send-back (a
`loom:triage` issue with an unanswered revise marker).

On 2026-10-07, 17% of curated issues (118 of 683 over five days, fleet-wide)
had waited a day or more for approval, with a median wait of 30 h so far.
Three throttles caused that tail. Each is fixed below.

## 1. Promotion runs every pass, even while PRs remain

> **Step dispatch (#11894, live since #11909).** Promotion runs as one terminal targeted Champion step per curated issue (`/loom:champion --issue N`), drawn by the work finder. The interval Champion runs `--non-item` only. See [`docs/design/work-finder-step-dispatch.md`](https://github.com/rjwalters/loom/blob/main/docs/design/work-finder-step-dispatch.md) §12.

Champion used to promote only once no PRs remained. A repository with a steady
or held PR queue never reached promotion. On 2026-10-07, rjwalters/loom had 38
open `loom:pr` PRs, 30 of them held by `loom:operator`, so "no PRs remain" was
never true. Promotion now runs every pass (`champion.md` → "Autonomous
Operation"), whether or not PRs remain held, sequenced, waiting on CI, or
unvisited. Every PR row is still visited each pass, in the same order, under
the same safety criteria and holds. A `LOOM_MAX_UNREVISED_EVALUATIONS` bound
(default 2, #4967) caps revision rounds (§3).

## 2. Where a fleet sets the knobs

A role session inherits the daemon's environment.
`loom-daemon-start.sh` forwards the exported `LOOM_*` variables into the
service unit (launchd `EnvironmentVariables`, systemd `Environment=`), and
later re-renders keep them. To change a value on a host, export it and re-run
`loom-daemon-start.sh`. A plain `loom-daemon restart` keeps the old
environment. See "Changing daemon environment variables" and "Env keys carried
forward across a re-render" in `daemon-reference.md`. For a hand-run
`/loom:champion`, export the value in that shell. Precedence is env var >
`LOOM_HYPERPARAMS` vector > config block > default (`hyperparameters.md`).

## 3. "Needs revision" goes to Curator, not to the operator

A `NEEDS REVISION` verdict used to wait for somebody to edit the body. After
one unchanged re-check, Champion escalated to the operator with a bare
`loom:operator-only` hold. In example-org/tool-repo#202, the findings were
"split per leaf" and "finish the registry audit". Both are agent work, and the
issue was escalated an hour after its first verdict. Under #10001, the
operator is asked for product-level calls only.

### The loop

1. **Champion rejects** (`champion-issue-promo.md` Step 4). It posts the
   verdict with a `<!-- champion:revise:body-<HASH> -->` marker, and in the
   edit that releases `loom:evaluating` removes `loom:curated` and adds
   `loom:triage` (#11860). The findings are the comment's first bullet list,
   the shape `classify-dependency-block.sh` already reads. When the only
   failure is an open dependency, the verdict is a block instead: nothing to
   revise.
2. **Curator's intake finds the send-back** (`curator.md` → "Revising a
   Champion send-back"): a `loom:triage` issue whose revise marker names the
   current body hash is unanswered. Curator skips admission and the premise
   gate for it.
3. **Curator revises.** It edits the body and appends a dated `## Revision`
   section that answers each finding: fixed, or refuted with evidence. Then it
   swaps `loom:triage` for `loom:curated`. Its other options are to close the
   issue, split it (the parent is then parked on its children with
   `park-record apply`, as a Builder does, so a tracking parent never comes
   back to Champion), or file a ranked decision for a product-level call.
4. **Champion re-evaluates.** The body edit changed the body hash, so the
   idempotency check finds no verdict for the new text and evaluates it fresh.
   This is the mechanism #7650 already relies on, and Champion keeps no other
   state for it.

### The bound

`PRIOR_REJECTIONS` counts every posted `NEEDS REVISION` verdict across all
revisions and never resets. Once it reaches `LOOM_MAX_UNREVISED_EVALUATIONS`,
or the body comes back unrevised, Step 4 stops routing ordinary rounds. After
the dependency-timing and premise-false gates, it takes the first case that
fits:

| Case | Action |
|---|---|
| The gap is a product-level call | File a ranked decision |
| No trusted `<!-- champion:revision-exhausted -->` comment on the issue | One final Curator round, carrying that marker |
| The final round failed and an independently identified preference or authority question exists | File a ranked decision on that question |
| The final round failed and every remaining finding is factual | One disposition round (`<!-- champion:revision-disposition -->`): Curator closes, splits or files a decision, not another edit |
| Otherwise | File a ranked decision: the disposition round was the agents' last move |

With the default of 2:

| Failing verdict | `PRIOR_REJECTIONS` before it | Outcome |
|---|---|---|
| 1st | 0 | Curator round 1 |
| 2nd (on the revised body) | 1 | Curator round 2 |
| 3rd | 2 | Final Curator round (marker), or a decision if the gap is product-level |
| 4th | 3 | Ranked decision if a preference or authority question is named; otherwise one disposition round for factual findings. The final-round marker is already on the issue |
| 5th | 4 | Ranked decision |

**At most three Curator rounds, then one terminal step: a well-formed decision or a Curator disposition.** The marker
stays on the issue, so a final round is never granted twice. A body Curator
returns unedited counts as a failed round and goes straight to the bound, with
no silent skip (#11860).

### When the operator is asked

The operator is asked only through
`loom-daemon operator-decision apply <N>` (which adds `loom:operator`),
with 2 to 4 ranked options ([`operator-decision.md`](operator-decision.md)).
Champion never applies a bare operator hold. There are two cases:

- **The gap is product-level from the start.** For example, whether the work
  is wanted at all, or a choice between two legitimate directions that facts
  cannot settle.
- **The final round came back and still fails on a preference or authority
  question.** The question must be identified independently of the failed
  rounds, by the falsifiability test in `label-state-machine.md`. The usual
  options are revise to a named scope, close as not planned, or accept as
  filed.

Exhausted rounds alone never make a factual finding a human call. If every
remaining finding is factual (an incorrect path, a missing registry audit),
the issue gets one disposition round, in which Curator must close, split or
file a decision. It does not get another edit. Only if that round also comes
back failing does Champion file the decision itself (#11860): it can no longer
stand down and leave the issue curated.

`apply` rewrites the body. Champion therefore stamps the post-apply body hash
as the verdict marker in its escalation comment. If the operator un-parks the
issue, that reads as a ruling on the current body (`OPERATOR_RULED`, #8245),
and Champion does not escalate the same text again.

## 4. Every evaluation is terminal (#11860)

Operator ruling, 2026-10-13 (the item-first step-dispatch design). A Champion evaluation of
a proposal ends in exactly one of:

| Verdict | End state |
|---|---|
| promote | `loom:issue` |
| revise | `loom:triage`, `loom:curated` removed, NEEDS REVISION + revise marker (above) |
| block | `loom:blocked` + park record; re-evaluated once the blocker closes |
| escalate | `loom:operator`, through `operator-decision apply` |

A premise-false close (#7657) ends the issue instead. What this retired:

- **The Step 3c capacity deferral** (#6729), which left a passing proposal
  curated. A cap now stops evaluation before the claim.
- **The silent skip and its `champion:unrevised-skips` tally** (#4967), which
  left an unrevised proposal curated. A body-hash counter would never advance
  anyway: Curator's revise always changes the hash. `PRIOR_REJECTIONS` alone
  bounds the round trip.
- **The dependency defer**, which left the issue curated with a marker patched
  onto a comment. It is a block now, lifted by a role or a human (#11301).
- **`loom:needs-revision`.** `labels.json` retires it onto `loom:triage` with
  `also_remove: ["loom:curated"]`, so `loom-daemon labels migrate` moves an
  open item to the state a revise verdict writes today. A migrated item has no
  revise marker; Curator's intake accepts the `champion:proposal-verdict:body-`
  marker every NEEDS REVISION verdict already carried.

Departures: `champion.md` Priority 2 no longer calls `loom:blocked` "outside
Champion's promotion authority" (a block is Champion's own verdict), and
`champion-issue-promo.md`'s "When NOT to Promote" ends each case in a verdict,
never a skip. A human un-park (`OPERATOR_RULED`, #8245) used to stand Champion
down until the body changed; it now brings a fresh evaluation, which never
re-escalates that body (a failure is a revise verdict).

**Rollout.** Run `loom-daemon labels migrate --apply --keep-labels` in the same
roll as the resync that ships this. Until then an open `loom:needs-revision`
item without `loom:curated` is in no Curator queue, and one with it is drawn by
Champion as unrevised and spends its final round early.

The new markers are control signals, so both sides read them from trusted
comments only (`loom-daemon forge trusted-comments`, #9548): an outsider's
forged revise or verdict marker neither skips Curator's admission nor sends a
proposal to Step 4, and its NEEDS REVISION text is not counted.

## Out of scope

- Recording each verdict in SigNoz is #10752.
- The dependency-cycle detector's own hold (`detect-dependency-cycle.sh`) and
  the epic flow (`champion-epic.md`) are unchanged.
