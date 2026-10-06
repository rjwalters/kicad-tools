# `pick.decision` (Issue #10212)

One record per **role tick** (Judge, Doctor, Champion, Curator and every other
role-runner role) and per **work-finder tick**: what it looked at, in the order
it ranked it, what it acted on, and why it skipped the rest. A queue-aware ETA
needs an item's position *as the serving role sees it*; this is the only record
of that, and it cannot be reconstructed afterwards.

Role records carry the queue the role agent **actually consumed** and what it
**actually did** (#10432), captured through the per-tick pick journal described
below; the daemon's admission-gate listing is only the fallback when the agent's
queue was not observed. `candidate_source` and `decisions_observed` say which.

OTLP-only log record (`otlp: Logs`, `native: false`), envelope
`schema_version: 12` (`NEW_KIND_SCHEMA_VERSION`). The log **body** is the
record's JSON; the attributes are `loom.kind` (`pick.decision`, which the
queries below filter on) and `loom.role` (both already on the collector's
`keep_keys`, so no collector change). Implementation:
`telemetry/kinds/pick_decision.rs` (payload, caps, reason set),
`observability/pick_decision.rs` (builders and emitters),
`observability/pick_journal.rs` (the per-tick pick journal),
`observability/otlp/mapping/pick_decision.rs` (OTLP).

**No forge calls are added.** The work-finder record reads the
`WorkFinderTickSummary` the tick already published. A role record reads the
pick journal its agent wrote (rows `pr-queue` and the agent `gh` front had
already fetched, and the argv of writes the agent was already issuing) and the
queue listing the role's own gate probe already fetched. Nothing is emitted, and
no journal is created, when no OTLP exporter runs.

## Fields

| Field | Type | Notes |
|---|---|---|
| `schema_version` | integer | payload version, currently `2` (not the envelope gate). `2` added `candidate_source`, `decisions_observed`, the `not_selected` reason and role `acted[]` entries |
| `role` | string | `work_finder`, `judge`, `doctor`, `champion`, `curator`, ... |
| `host` | string | the deciding host |
| `tick_id` | string | role ticks: the role-execution id (`role-<role>-<instant>`), the same id as the tick's trace `loom.sweep_id` when it launched; work finder: `work_finder-<instant>` |
| `started_at` / `ended_at` | RFC 3339 | tick bounds. Present on **empty ticks too**, so cadence per host and gaps (stalls) are measurable |
| `outcome` | string | role: the `role_tick.outcome` result (`success`, `skipped_queue_empty`, ...); work finder: `dispatched`, `halted`, `idle`, `none_dispatched` |
| `candidates_total` | integer | candidates considered, before the cap |
| `candidates[]` | array | in the ranker's order, **capped at 50**: `rank` (1-based), `repo` (forge slug, or `repo_unresolved`; never a local path), `number`, `stage`, `sort_key` (`name`, `value`) |
| `acted[]` | array | `repo`, `number`, `action`, capped at 50. Work finder: `dispatched`. Roles: one entry per distinct observed write, from the closed set below; may name an item outside `candidates` |
| `skipped[]` | array | one `{repo, number, reason}` per skipped candidate listed in `candidates` |
| `candidate_source` | string | `ready_queue` (work finder), `serving_queue` (the role's `pr-queue` result), `listing` (the issue/PR listings the role read, e.g. Curator), `gate_listing` (fallback: the daemon's gate listing), `none` (nothing observed) |
| `decisions_observed` | boolean | `true` when the agent's writes were observed, so every candidate without an action is a real skip; `false` when only a ranking was seen, and an unexplained candidate is then in neither `acted` nor `skipped` |

## What "ranking" means per source

- **Work finder.** `candidates` are the ready-queue rows in the daemon's real
  dispatch order (`work_finder::candidate_cmp`). `stage` is `loom:issue`;
  `sort_key` is `candidate_cmp`, valued with the plan's comparator keys
  (`name=value,...`) or the rank. Each row is *acted* (`dispatched`) or *skipped*
  with a reason mapped from its `QueueDisposition`.
- **Judge, Doctor, Champion** (`candidate_source = serving_queue`). The rows of
  the **latest** `loom-daemon pr-queue --role <role>` call the agent made during
  the tick, in `pr_planning::ordered_queue` order (operator priority level,
  interactive preference, fallback admission and each role's tie-breaks
  applied), so a fallback-only Judge queue is recorded even though the gate
  listing was empty. Earlier snapshots are not merged in: a refresh that
  adds a starred item or drops one is ranked as the queue the role last served,
  and an item acted on from an earlier snapshot stays in `acted` (actions are
  recorded independently of the ranking). `stage` is the row's workflow
  label (`loom:review-requested`, `loom:changes-requested`, `loom:pr`) or
  `fallback`; `sort_key` is `pr_queue`, valued
  `level=<operatorPriorityLevel>,reason=<priorityReason>,origin=<origin>,mode=<mode>`.
- **Curator** and any role that reads its queue with `gh issue|pr list`
  (`candidate_source = listing`). The rows the agent `gh` front served, in the
  order the agent read its listings (Curator's priority queries run in priority
  order), in the order the agent's own `--jq` printed them (the expression runs over the
  whole array, so `sort_by` and `.[0]` count). A printed value must name its row
  (an object, a bare number, or a string led by `#<number>`); otherwise the
  listing is left unobserved rather than guessed. `stage` is the listed label; `sort_key` is
  `listing_order`.
- **Fallback** (`candidate_source = gate_listing`): no journal was read (the
  agent's runtime does not inherit it, or the queue was never read). The gate's
  listing in **forge listing order**, which can differ from the serving order.
  A Judge/Doctor tick whose queue was empty was never spawned and carries
  `outcome = skipped_queue_empty`.

### The pick journal

When an OTLP exporter runs, the role runner gives each launched role agent a
private journal file (`$TMPDIR/loom-pick-journal/<role>-<uuid>.jsonl`, `0700`
directory, path in `LOOM_PICK_JOURNAL`). Three writers in the agent's process
tree append JSON lines; outside a role tick the variable is unset and all three
are no-ops:

| Writer | Line | What it records |
|---|---|---|
| `loom-daemon pr-queue` (live mode) | `queue` | the ordered rows it prints, with labels and sort key; whether the agent's `gh` resolves to the agent `gh` front |
| agent `gh` front, served `issue\|pr list` | `listing` | the rows served, after the caller's `--jq` |
| agent `gh` front, every call | `act` | the forge writes in the argv: `issue\|pr edit --add-label`, `pr merge`, `pr review --approve\|--request-changes`, `gh api` label POSTs and merge PUTs |

At the end of the tick the daemon reads and deletes the file; a leftover from a
crashed tick is pruned after a day. Acts are writes the agent *issued*; the
record does not confirm the forge accepted them.

A `queue` line keeps at most 200 rows but also records the queue's uncapped
size, which becomes `candidates_total`. If the file is missing or unreadable
(an agent that never ran `pr-queue` or the `gh` front writes none) nothing was
observed: `decisions_observed` is `false` and unexplained candidates stay
undecided. A file that exists but is empty is an observed tick with no acts.

A `queue` line keeps at most 200 rows but also records the queue's uncapped
size, which becomes `candidates_total`. If the file is missing or unreadable
(an agent that never ran `pr-queue` or the `gh` front writes none) nothing was
observed: `decisions_observed` is `false` and unexplained candidates stay
undecided. A file that exists but is empty is an observed tick with no acts.

**Role actions (closed set).** `claimed` (`loom:reviewing`, `loom:treating`,
`loom:curating`, `loom:evaluating`, `loom:building`), `approved` (`loom:pr`, or
`pr review --approve`), `changes_requested`, `merged`, `curated`, `promoted`
(`loom:issue`), `blocked`, `escalated` (`loom:operator`, `loom:operator-only`,
`loom:operator-decision`), `labeled` (any other `loom:*` label).

**Role skip reasons.** With `decisions_observed`, a candidate the agent did not
write to is skipped with the first that applies: `operator_hold` (a hold label:
the role's park labels, `loom:operator`, `loom:operator-only`,
`loom:operator-decision`, `loom:operator-mechanical`), `blocked`
(`loom:blocked`, `loom:ci-failure`), `overlap_chain` (`loom:sequenced`),
`in_flight` (a claim label held by someone else), else `not_selected`. Without
it, only a hold label names a reason; other candidates are undecided.

## Skip reasons (closed set)

`skipped[].reason` is one of these, and nothing else:

| Reason | Meaning | Work-finder dispositions mapped to it |
|---|---|---|
| `overlap_chain` | overlaps an in-flight chain of stacked work | roles: the candidate carries `loom:sequenced` |
| `pr_open_skip` | the item already has an open PR | `open_pr`, `open_pr_backoff` |
| `operator_hold` | held for a human | `parked`, `hard_exclusion`, `declined`; roles: a merge-hold / park label on the PR |
| `quota` | token pool or quota would not admit it | roles: `skipped_no_token_pool`, `skipped_pool_exhausted` ticks |
| `cap` | a concurrency, ramp, per-repo, slice or saturation cap | `deferred_capacity`, `deferred_ramp_cap`, `deferred_saturation`, `deferred_out_of_slice`, `deferred_repo_cap` |
| `in_flight` | a live sweep or claim covers it | `in_flight`; roles: a claim label the agent did not write |
| `backoff` | inside a back-off, cooldown, recheck or retry window | `deferred_build_backoff`, `recheck_interval`, `dispatch_backoff`, `noop_cooldown`, `prless_retry` |
| `halted` | the repo's dispatch is halted (red main, gate, drain, breaker) | `workspace_halted` |
| `host_constraint` | not for this host | `host_constraint`, `host_class_refused`, `peer_claim` |
| `blocked` | blocked by something specific to the item | `workspace_commands_missing`, `quarantined`, `labelled_blocked`, `unknown`; roles: `loom:blocked`, `loom:ci-failure` |
| `error` | the dispatch attempt failed | `dispatch_error` |
| `tick_skipped` | the role tick did not run for another reason | roles: runtime rejected, model/runtime mismatch, load, queue gate |
| `not_selected` | the role ran, its writes were observed, and it did not act on this candidate | roles only, with `decisions_observed` |

`QueueDisposition::Dispatched` is the one disposition with no reason: it is
*acted*. The mapping is a total match in
`PickSkipReason::from_disposition`, so a new disposition must be classified
there.

## Rank of an item in each role's latest candidate list

For a record with `candidate_source = serving_queue` this is the item's rank in
the role's own `pr-queue` order; for `listing`, in the order the role read it;
for `gate_listing`, only the forge listing order (select
`JSONExtractString(body, 'candidate_source')` to tell them apart).

Run against `signoz_logs.distributed_logs_v2` (ClickHouse). Substitute the repo,
number and instant. `rank = 0` means the item was **not** in that role's latest
list at that instant. `candidates_total` says how many it had; an item beyond
50 reads `0` even though it was queued, so compare with `candidates_total`
before concluding it was absent.

```sql
SELECT
  attributes_string['loom.role']                    AS role,
  JSONExtractString(body, 'host')                   AS host,
  fromUnixTimestamp64Nano(timestamp)                AS tick_at,
  JSONExtractUInt(body, 'candidates_total')         AS candidates_total,
  JSONExtractString(body, 'candidate_source')       AS source,
  arrayFirstIndex(
    c -> JSONExtractString(c, 'repo') = 'rjwalters/loom'
     AND JSONExtractUInt(c, 'number') = 10212,
    JSONExtractArrayRaw(body, 'candidates'))        AS rank
FROM signoz_logs.distributed_logs_v2
WHERE attributes_string['loom.kind'] = 'pick.decision'
  AND timestamp <= toUnixTimestamp64Nano(toDateTime64('2026-10-04 12:00:00', 9))
  AND timestamp >= toUnixTimestamp64Nano(toDateTime64('2026-10-04 12:00:00', 9) - INTERVAL 1 DAY)
ORDER BY timestamp DESC
LIMIT 1 BY role, host
```

(`LIMIT 1 BY role, host` keeps each role's newest record per host at or before
the instant.) This query is **not yet run against a live SigNoz or the pinned
ClickHouse** (no `signoz_eta_queries`-style harness covers it); the function
shapes are the ones the ETA queries already use.

Service cadence and stalls per host: `max(tick_at) - lag(tick_at)` over the same
filter grouped by `role, host`.

## Record volume

Derived from the shipped default cadences, **not measured on a live fleet**:

| Source | Default cadence | Records/day per workspace root |
|---|---|---|
| work finder | 60 s per tick, one record per tick (all roots) | 1,440 per host |
| judge, doctor, curator | 300 s | up to 288 each |
| champion | 600 s | up to 144 |
| auditor, hermit | 600 s | up to 144 each |
| guide | 900 s | up to 96 |
| architect | 3600 s | up to 24 |

A role record is only written for a tick that was admitted and run; a root held
back at admission writes none. With 1 root and the default roles, that is about
1,440 + 288*3 + 144*3 + 96 + 24 = roughly 2.9 k rows/day per host; each extra
root adds roughly 1.5 k. Row size is bounded by the 50-entry cap: an empty tick
is under 400 bytes of JSON, a record with every list at its 50-entry cap under
20 KB (`pick_decision_record_size_is_bounded` pins both bounds). Worst case at
the cap on every tick is therefore under about 60 MB/day per host; typical
ticks (a handful of candidates) are around 1 KB.

The pick journal adds no rows: it is a local file of a few KB per launched role
tick (each line is capped at 200 rows and the file at 1 MiB), deleted when the
tick's record is built.
