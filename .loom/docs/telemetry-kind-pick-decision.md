# `pick.decision` (Issue #10212)

One record per **role tick** (Judge, Doctor, Champion, Curator and every other
role-runner role) and per **work-finder tick**: what it looked at, in the order
it ranked it, what it acted on, and why it skipped the rest. A queue-aware ETA
needs an item's position *as the serving role sees it*; this is the only record
of that, and it cannot be reconstructed afterwards.

> **Scope (gate-observation slice of #10212).** For **role** ticks this record
> observes the daemon's admission-gate listing, not the queue the role agent
> consumes through `pr-queue`, and it records no acted items and no Curator
> candidates. Work-finder ticks are fully covered. The remaining acceptance
> work is tracked in a follow-up issue linked from PR #10268.

OTLP-only log record (`otlp: Logs`, `native: false`), envelope
`schema_version: 12` (`NEW_KIND_SCHEMA_VERSION`). The log **body** is the
record's JSON; the attributes are `loom.kind` (`pick.decision`, which the
queries below filter on) and `loom.role` (both already on the collector's
`keep_keys`, so no collector change). Implementation:
`telemetry/kinds/pick_decision.rs` (payload, caps, reason set),
`observability/pick_decision.rs` (builders and emitters),
`observability/otlp/mapping/pick_decision.rs` (OTLP).

**No forge calls are added.** The work-finder record reads the
`WorkFinderTickSummary` the tick already published. A role record reads the
queue listing the role's own gate probe already fetched (the same rows that feed
the demand ledger). Nothing is emitted when no OTLP exporter runs.

## Fields

| Field | Type | Notes |
|---|---|---|
| `schema_version` | integer | payload version, currently `1` (not the envelope gate) |
| `role` | string | `work_finder`, `judge`, `doctor`, `champion`, `curator`, ... |
| `host` | string | the deciding host |
| `tick_id` | string | role ticks: the role-execution id (`role-<role>-<instant>`), the same id as the tick's trace `loom.sweep_id` when it launched; work finder: `work_finder-<instant>` |
| `started_at` / `ended_at` | RFC 3339 | tick bounds. Present on **empty ticks too**, so cadence per host and gaps (stalls) are measurable |
| `outcome` | string | role: the `role_tick.outcome` result (`success`, `skipped_queue_empty`, ...); work finder: `dispatched`, `halted`, `idle`, `none_dispatched` |
| `candidates_total` | integer | candidates considered, before the cap |
| `candidates[]` | array | in the ranker's order, **capped at 50**: `rank` (1-based), `repo` (forge slug, or `repo_unresolved`; never a local path), `number`, `stage`, `sort_key` (`name`, `value`) |
| `acted[]` | array | `repo`, `number`, `action` (`dispatched`), capped at 50 |
| `skipped[]` | array | one `{repo, number, reason}` per skipped candidate listed in `candidates` |

## What "ranking" means per source

- **Work finder.** `candidates` are the ready-queue rows in the daemon's real
  dispatch order (`work_finder::candidate_cmp`). `stage` is `loom:issue`;
  `sort_key` is `candidate_cmp`, valued with the plan's comparator keys
  (`name=value,...`) or the rank. Each row is *acted* (`dispatched`) or *skipped*
  with a reason mapped from its `QueueDisposition`.
- **Roles.** The daemon gates a role on its queue but does not choose among the
  items: the role agent does, following its prompt. So `candidates` are the
  gate's listing in **forge listing order** (`sort_key.name = listing_order`),
  `stage` is the listing label (`loom:review-requested` for Judge,
  `loom:changes-requested` for Doctor, `loom:pr` for Champion), and `acted` is
  empty: what the agent then touched is on `role_tick.outcome`'s actions and
  targets. Curator has no gate listing and emits an empty candidate list; the
  record still marks the tick. Champion's list is its `loom:pr` queue only
  (not `loom:curated` promotions). A Judge/Doctor tick whose queue was empty was
  never spawned and carries `outcome = skipped_queue_empty`.

## Skip reasons (closed set)

`skipped[].reason` is one of these, and nothing else:

| Reason | Meaning | Work-finder dispositions mapped to it |
|---|---|---|
| `overlap_chain` | overlaps an in-flight chain of stacked work | (reserved; no source emits it yet) |
| `pr_open_skip` | the item already has an open PR | `open_pr`, `open_pr_backoff` |
| `operator_hold` | held for a human | `parked`, `hard_exclusion`, `declined`; roles: a merge-hold / park label on the PR |
| `quota` | token pool or quota would not admit it | roles: `skipped_no_token_pool`, `skipped_pool_exhausted` ticks |
| `cap` | a concurrency, ramp, per-repo, slice or saturation cap | `deferred_capacity`, `deferred_ramp_cap`, `deferred_saturation`, `deferred_out_of_slice`, `deferred_repo_cap` |
| `in_flight` | a live sweep or claim covers it | `in_flight` |
| `backoff` | inside a back-off, cooldown, recheck or retry window | `deferred_build_backoff`, `recheck_interval`, `dispatch_backoff`, `noop_cooldown`, `prless_retry` |
| `halted` | the repo's dispatch is halted (red main, gate, drain, breaker) | `workspace_halted` |
| `host_constraint` | not for this host | `host_constraint`, `host_class_refused`, `peer_claim` |
| `blocked` | blocked by something specific to the item | `workspace_commands_missing`, `quarantined`, `labelled_blocked`, `unknown` |
| `error` | the dispatch attempt failed | `dispatch_error` |
| `tick_skipped` | the role tick did not run for another reason | roles: runtime rejected, model/runtime mismatch, load, queue gate |

`QueueDisposition::Dispatched` is the one disposition with no reason: it is
*acted*. The mapping is a total match in
`PickSkipReason::from_disposition`, so a new disposition must be classified
there.

## Rank of an item in each role's latest candidate list

For role ticks this is the rank in the gate's **listing order**, which can
differ from the role's `pr-queue` order (operator priority, interactive
preference, fallback admission); do not read it as the serving role's rank.

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
is a few hundred bytes of JSON, a full 50-candidate record under about 12 KB
(`pick_decision_record_size_is_bounded` pins both bounds). Worst case at the
cap on every tick is therefore low tens of MB/day per host; typical ticks are
well under 1 KB.
