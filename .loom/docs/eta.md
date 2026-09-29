# ETA: when will a sweep finish, and when will its work land?

Loom estimates, per issue, when a running sweep finishes (`finish`) and when
the issue's work lands (`land`: its PR merges, or the issue closes as
completed). Every estimate carries a versioned **explanation** that is enough
to recompute it offline, and every estimate is **scored** against what
happened. Both go to SigNoz as `eta.estimate` / `eta.outcome` log records
(field reference: [`telemetry-schema.md`](telemetry-schema.md)).

Phase 1 of #9289. Later phases: backfill and a leak-free backtest (#9325),
estimates for not-yet-started issues (#9326), a CLI (#9327), shadow mode and
promotion (#9328), the loom-ui snapshot (#9329) and fuller docs (#9330).

## The model

Remaining time is a sum over the stages an issue still has to pass, from the
stage it is in now, with one branch at every Judge verdict:

```text
sweep.curator → sweep.builder → review_wait ─┬─ approved ──→ merge_wait → landed
                                             └─ changes_requested → doctor ─┘ (loop, capped)
```

| Stage | Entered when | Left when |
|---|---|---|
| `sweep.curator` | the sweep is dispatched | the curator phase completes |
| `sweep.builder` | the curator phase completes | the PR opens |
| `review_wait` | the PR asks for review | the Judge's verdict |
| `doctor` | `loom:changes-requested` | the PR asks for review again |
| `merge_wait` | `loom:pr` | the PR merges |

Human-gated stages (intake, approval, operator holds) are outside the model:
an issue there has no estimate, with a reason (below). A running sweep gets a
`land` estimate from `sweep.curator` on; an item in `doctor` always counts at
least one rework round, because `doctor` is entered only through a
rejection.

**Distributions.** Each `(repo, stage)` is summarised as a 21-point
nearest-rank quantile grid (`p0, p5, …, p100`) over the most recent 200
samples of the last 60 days. Every grid value is an observed duration. With
fewer than 8 samples in the repo the host-wide samples are used, and with
fewer than 8 there too the estimate is refused.

**Conditioning.** The current stage's draw is conditioned on the time already
spent in it: draws come from the grid's `[F(age), 1]` range and the age is
subtracted. Fewer than 5 samples longer than the age means the item has
outlived its own history (`beyond_history`).

**Combination.** A deterministic Monte Carlo of 4000 paths draws every stage
a path visits from its grid, and a verdict at every `review_wait`
(`P(changes requested | attempt k)` from the Judge verdict history). Rework is
capped at 2 rounds; a path at the cap approves, so `land` is the time to land
*given that it lands*. The seed is derived from the estimate id, and the
generator is SplitMix64, so the same inputs always give the same numbers.

**No leakage.** An estimate at `as_of` reads only samples observed strictly
before `as_of`. Replaying history is therefore the same computation as the
live one (the #9325 backtest depends on this).

**History is host-local (#9343).** v1 reads only this host's own journals,
and every explanation says so: `history.scope = "local"`, with the samples
each source (`samples_by_source`) and each recording host
(`samples_by_host`) contributed. Consequences:

- **Empty** on most hosts: a host that ran no sweeps for a repo has no
  samples for it, so every estimate there is `insufficient_samples`.
- **Biased** where history exists: a host sees only its own sweeps and the
  review transitions it happened to watch.
- **Inconsistent** across hosts: two hosts estimating the same issue read
  different histories and disagree.
- The human-gated stages (review waits, approvals, merges) happen on the
  forge, not on any host.

The direction is a fleet-wide snapshot built from forge / D32 story data
plus the fleet's `sweep.outcome` records in SigNoz, fetched outside the
estimator and handed to it like the local history (`scope = "fleet"`, a
reserved value today). The estimator stays a pure function of
`(history snapshot, input)`, so a backtest stays leak-free.

## Heuristics and versioning

| id | kind | reads | an approved path ends |
|---|---|---|---|
| `finish-v1` | `finish` | in-sweep phase durations (`sweep-outcome-telemetry.jsonl`) | after the in-sweep merge when at least half of the history's successful sweeps merged themselves, else at the verdict |
| `land-v1` | `land` | in-sweep phases and the stage-sample journal | after `merge_wait` |

A shipped id is **immutable**: a golden test pins each id's output on a fixed
fixture. A behaviour change is a new id registered beside the old one
(`eta::Registry`), selected per kind by `autonomous.eta.current`.

## The explanation (`eta-explanation/v1`)

The heuristic builds the explanation first and computes the numbers from it,
so `simulate::run_explanation` reproduces `result` from the JSON alone. Top
level: `schema`, `estimate_id`, `heuristic`, `kind`, `loom` (provenance),
`as_of`, `subject`, `current_stage`, `history_window`, `path`, `stages[]`
(each with its `distribution` grid, `conditioning` on the current stage,
`reached_with_probability`, `mean_visits`), `branches.changes_requested`,
`history` (`scope`, `sources`, `samples_by_source`, `samples_by_host`),
`combination` (`draws`, `seed`, `rng`, `draw_order`), `result` (`p25_sec`,
`p50_sec`, `p75_sec`, `eta_p50_at`, `samples_min`, `stage_marks`),
`contributions`, `features`, `features_omitted`, `no_estimate_reason`,
`truncated`.

`result.stage_marks` (#9366) is the projected future, one mark per stage in
stage order: `p25_at` / `p50_at` / `p75_at` are `as_of` plus that percentile
of the stage's cumulative entry time over the same simulated paths the
`combination` seed already drew (no new draws), and `mean_visits` mirrors the
stage entry's so the list is self-contained. The terminal stage's mark is the
path completion time, so its `p50_at` is exactly `eta_p50_at`; a stage no
path visits carries `null` times, never a fabricated one. A timeline can be
drawn from `stage_marks` alone.

A feature is `null` when it was not measured, with a `features_omitted`
reason; never a default. An explanation stays near 8 KiB; over 32 KiB it
drops `features`, then the stage grids, then the stage marks, then every
remaining list (`detail`), stopping as soon as it fits, and names each drop
in `truncated`.

## No-estimate reasons

| reason | when |
|---|---|
| `blocked` | a hold or park label: `loom:blocked`, `loom:operator`, `loom:operator-only`, `loom:operator-decision`, `loom:operator-mechanical`, `loom:needs-capability` |
| `human_gated` | intake or approval (`loom:triage`, `loom:curating`, `loom:curated`) |
| `insufficient_samples` | a needed stage or the verdict history is under the sample floor |
| `beyond_history` | the item has been in its stage longer than all but 5 samples |
| `no_dispatch_plan` | not started (a later phase estimates these) |
| `unknown_stage` | no stage label, or contradictory ones |
| `stale_inputs` | reserved |

A refusal is emitted as an `eta.estimate` with no `result`, when its reason
first appears.

## Cadence, triggers and the journal

The tracker runs wherever observability runs, and is **on by default**. Two
triggers:

- **Bus**: sweep dispatch, each completed phase and the sweep's end. The item
  is re-estimated immediately.
- **Every 5 minutes** (the collector's snapshot pass): each managed repo's
  review-label listings (ETag-cached, so an unchanged listing is free), at
  most 8 forge reads (`pulls/{n}` for PRs that left review, `issues/{n}` for
  issues whose outcome only the issue can settle — anything over the budget is
  retried next pass), a history reload, and a refresh of every live estimate.
  The estimation step runs on a blocking thread behind a `catch_unwind`, so an
  ETA failure costs only ETA and never the observability collector.

An unchanged estimate is refreshed every `refreshSecs` (300); a changed stage,
rework count or refusal reason emits at once; a series is capped at 20
emissions per rolling hour. Every emitted estimate waits for its outcome in
`.loom/state/eta/pending.jsonl`, which survives a restart.

**The stage-sample journal** (`.loom/logs/eta-stage-samples.jsonl`) records
every boundary the tracker observes, the moment it observes it, with its raw
fields: sweep dispatch, phase, repeat and terminal events, label transitions,
first sightings, verdicts and PR resolutions. A row that completes a stage
with an observed entry has a `duration_sec`; a row whose entry was only a
lower bound does not. Rows the sweep-outcome journal already carries are
marked `in_sweep` and are not read back as history. Rotation: 5 MiB or 30
days, one `.1` generation.

## Outcomes and scoring

| kind | resolves when | outcome |
|---|---|---|
| `finish` | the sweep's terminal event (exited or crashed) | `finished` |
| `land` | the in-sweep merge phase, the PR's merge time read when it leaves the review listings, or the issue closing as **completed** | `landed` |
| `land` | the issue closing as **not planned** | `abandoned` |

Each outcome carries `error_sec` (`actual − p50`), `covered`
(`p25 ≤ actual ≤ p75`), `pinball_loss_sec`
(`Σ ρ_q(actual − q̂)` over q = .25, .5, .75), the horizon bucket of the
predicted p50, and the per-stage actuals against each stage's predicted
quartiles. `abandoned` outcomes and outcomes of refusals are counted but have
no error fields: absent is never zero.

**Nothing else is an outcome.** A PR closed unmerged and a sweep that ended
before any PR are *not* abandonments — a replacement PR or a later sweep
usually lands the same issue — so each queues one `issues/{n}` read and the
verdict comes from the issue's own `state` / `state_reason`. While the issue
stays open the estimates stay pending: the join is on `(repo, issue, kind)`,
so the eventual landing scores them, and only a 30-day expiry drops them.

**A reopen starts a new series.** The first outcome stands: when an estimate
resolves, every estimate of that series emitted *after* the outcome instant
(the tail of a late `pulls_read` / `issues_read`) is dropped unscored, so a
second landing can never score the first landing's leftovers.

**No read is ever lost.** A PR that leaves review and an issue that needs a
state read stay queued in the tracker and are re-offered every pass until they
are answered, so a read that did not fit the per-pass budget or that failed is
retried rather than dropped. An item with an outstanding check gets **no**
`land` estimate meanwhile — a merge train larger than the budget resolves over
the following passes instead of emitting phantom live ETAs for PRs that have
already merged.

## Provenance

Every `eta.estimate` and `eta.outcome` carries the heuristic id and the
computing daemon's `version`, full 40-hex `revision` and `tree_state`, from
the same source as every span's `loom.daemon.*` attributes
([`trace-identity.md`](trace-identity.md)). These are required fields: a
record without them does not deserialize, and one that fails validation is
never emitted. A build whose revision or tree state is `unknown` (a tarball
build) still emits, so no data is lost, but with `complete: false`
(exported as `loom.eta.provenance_complete` /
`loom.eta.outcome_provenance_complete`), and the daemon warns once at start;
the accuracy queries keep only rows where both are true. An outcome carries both the estimating build and its own.
Estimate ids are derived (`derived_hex(["loom.eta.estimate", repo key, issue,
kind, heuristic, as_of])`), never random, and both kinds sit inside the
issue's D32 story trace.

## Configuration

`autonomous.eta` (env > config > default):

| key | env | default |
|---|---|---|
| `enabled` | `LOOM_ETA_ENABLED` | `true` |
| `dryRun` | `LOOM_ETA_DRY_RUN` | `false`: compute, journal and log `eta: would emit …` lines, enqueue nothing |
| `refreshSecs` | `LOOM_ETA_REFRESH_SECS` | `300` (floor 60) |
| `current.finish` / `current.land` | none | `finish-v1` / `land-v1` |

Each pass logs `eta: pass emitted=N refused=M outcomes=K …`.

## Queries

[`eta-queries.sql`](https://github.com/rjwalters/loom/blob/main/defaults/observability/signoz/eta-queries.sql)
answers, against `signoz_logs.distributed_logs_v2`:

- **Q1**: MAE, 25–75 coverage and bias per heuristic, revision, kind, repo
  and horizon bucket.
- **Q2**: mean pinball loss per heuristic × kind, and per revision.
- **Q3**: feature ranking. Each outcome joins its estimate on
  `loom.eta.estimate_id`, and every numeric feature is correlated with the
  error (`rankCorr`, `corr`).

All three group by `(heuristic, revision)` as well as by heuristic, so a
daemon roll shows as two rows, and all three exclude rows with incomplete
provenance.
