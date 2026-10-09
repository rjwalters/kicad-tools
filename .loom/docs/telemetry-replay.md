# Telemetry Replay Contract

Status: contract, emit-side facts, committed replay SQL (Issue #10196,
slices 1 and R3/R4), the `fleet.state` record (slice R1) and
`loom-daemon telemetry-replay --as-of <t>` (slice R6). `--check` is a later
slice and does not exist yet.

The question this contract answers: **what did the fleet look like at instant
`t`, as a daemon running at `t` could have known it?** ETA backtesting
(#10193) and any retroactive analysis depend on it.

## Two clocks

Every log record has two instants. They are different facts and must never be
conflated.

| Clock | OTLP column | Meaning |
|-------|-------------|---------|
| **Event time** | `timestamp` (`time_unix_nano`) | When the thing happened (usually the envelope's `emitted_at`; `eta.*` and `session.output` override it with their own source instant). |
| **Knowable-at** | `created_at` (SigNoz insert time) | When the record became *available to a reader*. The OTLP `observed_timestamp` is only a producer-side lower bound (see below). |

Delivery is batched, retried and at-least-once, so a `sweep.outcome` can reach
SigNoz long after its event time. A query over `timestamp < t` returns rows
that nothing could have seen at `t`.

### The rule: replay filters on knowable-at

A reconstruction at `t` uses only records whose **knowable-at instant is
`< t`**. Filtering on event time is a bug: it leaks the future into the past.
Event time orders and groups records inside the reconstruction; it never
decides membership.

### The knowable-at column (design decision, #10196 R3)

**A reader uses SigNoz's own insert clock, `created_at` on
`signoz_logs.distributed_logs_v2`, as the knowable-at instant.** It is set by
the backend when the row becomes queryable, so it cannot be earlier than the
moment a reader could have seen the record. `session-output.md` (Querying
end-to-end latency) already relies on it and measured it against the producer
clocks.

`observed_timestamp` is NOT the knowable-at column. The OTLP exporter sets it
to a copy of `emitted_at` (`log_record_for` in `observability/otlp/mapping.rs`;
only `session.output` overrides it with the producer's read time). That is a
producer-side value: a record delayed in the export queue still claims to have
been observed at its event time. Use it only as a lower bound.

This makes the collector-side receive stamp planned in slice 1 unnecessary: the
collector processor is superseded and no collector change is needed.

**Verification status: unverified on a live store.** The check below must
return `0` in the second column. It was not run from the worker that wrote this
change (no ClickHouse access), so treat `created_at` as verified only after its
first live run. If the count is not `0`, fall back to
`greatest(created_at, observed_timestamp)` and say so here.

```sql
SELECT count(),
       countIf(created_at < fromUnixTimestamp64Nano(toInt64(observed_timestamp)))
FROM signoz_logs.distributed_logs_v2
WHERE timestamp > toUnixTimestamp64Nano(now64(9) - INTERVAL 1 DAY);
```

The replay SQL is committed at
[`observability/signoz/replay-queries.sql`](https://github.com/rjwalters/loom/blob/main/defaults/observability/signoz/replay-queries.sql):
fleet state at `t` (anchors plus deltas, merged per `(repo, issue)`, preferring
the row that carries a `host`), the spread between hosts' views, coverage at
`t`, anchor completeness and volume, and outcome facts. It filters on
`created_at < t` and dedupes with `LIMIT 1 BY`. It introduces no row caps.

Per issue, the **last** operation on a host's chain wins, so an issue that is
removed, re-added and removed again stays deleted. A host's state is used only
when it is reconstructable: its base anchor (the one its newest chain names)
and every delta on the chain arrived with all their byte chunks, and each
delta's `prev_as_of` is the record before it. Anything else (a lost delta, a
missing chunk, a chain whose anchor never arrived) makes that host **unknown**
at `t`: it contributes no rows and coverage reports it as not covered. A lost
delta at the very end of a chain cannot be detected; coverage shows how fresh
each chain is. `loom-daemon/tests/signoz_replay_queries.rs` runs these queries
against a pinned ClickHouse over a fixture for each case.

## Identity and dedupe

Delivery is at least once. Every log record carries `loom.record_id`, a
content-derived id, so a reader dedupes with `LIMIT 1 BY loom.record_id`.

```
loom.record_id = derived_hex(["loom.record", kind, host_id, emitted_at, <record JSON>], 16)
```

It follows `trace-identity.md`: SHA-256 over NUL-terminated parts, never a
random value. A retried delivery of the same envelope hashes identically; a
re-snapshot of unchanged state is a distinct record because `emitted_at`
differs. `eta.*` records additionally keep their own `loom.eta.estimate_id`,
which is unchanged.

### Outcome facts and the cross-host id

`loom.record_id` includes `host_id`, so it dedupes repeated deliveries from one
host only. Two hosts that observe the same outcome emit two different record
ids. The outcome kinds therefore also carry `loom.fact_id`, built from the
natural key alone:

```
loom.fact_id = derived_hex(["loom.fact", kind, <natural key parts>], 16)
```

It has no `host_id` and no `emitted_at`. Readers dedupe facts with
`LIMIT 1 BY loom.fact_id`, keeping the earliest knowable-at;
`loom.record_id` still dedupes deliveries. Every host emits its own view and
nothing is elected, so duplicates across hosts are expected.

| Kind | Natural key | Event time | Knowable-at | Notes |
|------|-------------|------------|-------------|-------|
| `pr.resolved` | `(repo, pr_number, state)` | `resolved_at` | `created_at` | `state` is `merged` or `closed`. |
| `eta.stage_outcome` | `(repo, issue, stage, left_at)` | `left_at` | `created_at` | `left_at` is RFC 3339 UTC, nanosecond precision. After the ETA subsystem moves to loom-ui (#11098) the kind is re-homed; the wire kind name and `loom.eta.*` attribute keys are kept (stage 1 is additive only), and `loom.fact_id` is additive. |

State precedence for a PR: an external webhook outcome row is primary for the
merge or close instant, `pr.resolved` corroborates it, and a missing webhook
row is not a missing outcome.

## Export coverage

Absence of a record is ambiguous: nothing happened, or the host was not
reporting. Each `host.health` record therefore names what the emitting host was
exporting:

- `exporters`: exporter names that actually started in the process (`https`,
  `otlp`). An entry that never ran (misconfigured, e.g. `otlp` on a build
  without the feature or a rejected endpoint) is excluded.
- `exported_kinds`: the wire `kind` tags those exporters carry, derived from
  the kind registry (`telemetry/kinds.rs`).

Both are omitted when empty, and **empty means unknown** (no exporter
started, or a pre-#10196 daemon), never "exports nothing". A reader at
`t` treats a host as covered when it has a `host.health` record knowable
before `t` and recent enough, and reads silence for a kind in `exported_kinds`
as "nothing happened". `host.health` is exported as gauges, so the coverage
read path for it is the native-HTTPS side until a log form lands in a later
slice.

## Fleet state (`fleet.state`)

Every host with an OTLP exporter sends `fleet.state` log records on its
5-minute snapshot pass, whether or not ETA is enabled. **Each host emits its
own view; nothing is elected.** The field reference is in
[`telemetry-schema.md`](telemetry-schema.md#fleetstate). Per `(repo, issue)`
the host can see, it carries stage, entered-at and PR; a row for a sweep the
host runs also carries `host` and `slot`; a `ready_wait` row carries the
host's planner `rank` and the planner's inputs (star, starred-at, level,
fleet priority, creation instant). Per repo it carries the open-PR census,
which counts open PRs under a Loom review label, and `ready_complete`.

What the rows cover is exactly what the host's reads saw:

- **PRs under review**: every open PR under a review label. Each label's
  listing is walked page by page; a walk that fails, hits its page limit or
  sees the listing shift is a failed listing, so the repo's `census` is absent
  and its earlier PR rows are kept, never sent as `removed`.
- **Ready queue**: every row the planner saw on the host's last work-finder
  tick. A repo whose ready listing the work finder walked to its last page is
  `ready_complete: true` and its `ready_wait` rows are diffed (#11139). A repo
  whose listing came back partial (a later page failed, the page cap, a
  mid-walk change) is `ready_complete: false`: its `ready_wait` rows are not
  the repo's whole queue. Such a repo is sent with `ready_replace: true`, carrying its **entire** observed
  `ready_wait` set whenever it is named, and the reader replaces rather than
  diffs (step 3 below). A repo whose tick listing failed keeps its earlier
  `ready_wait` rows. Only a `ready_complete: true` repo's `ready_wait` rows
  can be read as its full ready queue.

A kept row (after a failed read) may be an item that has since left; the next
read of that repo replaces or removes it.

- **Anchor** (`loom.fleet.anchor = true`): the host's full view. Sent on the
  first pass of every daemon process, whenever the planner stamps change, and
  at least every 3600 s after that.
- **Delta** (`loom.fleet.anchor = false`): sent between anchors only when
  something changed. It holds the added or changed rows, the issues that left
  (`removed`), and the full census and `ready_complete` of each repo it names;
  a `ready_replace` repo's `rows` also hold its whole `ready_wait` set, and its
  `removed` names no `ready_wait` row. `anchor_as_of` names
  the anchor the delta belongs to, and `prev_as_of` names the record it applies
  on top of.
- **Chunks**: the emitter has no row cap; it drops none of the rows its reads
  saw (the bullets above say what they cover). A record over ~1 MB of JSON is split into
  `loom.fleet.chunk_count` log records sharing `as_of`, numbered by
  `loom.fleet.chunk_index`. Today's queue fits in one.
- **Regime stamps**: every record carries `planner_version`,
  `planner_config_hash` and (with a fleet store) `fleet_config_hash`. A change
  in any of them is a regime boundary; the emitter starts a new anchor there,
  and a reader fitting on a recent window cuts the window at it.

To reconstruct one host's state at `t`:

1. Keep only that host's `fleet.state` records knowable before `t`, deduped on
   `loom.record_id`. Group them by `as_of`; a group is usable only when it
   holds all `chunk_count` chunks. The union of a group's chunks is the
   record (a repo split across chunks contributes rows from each).
2. Take the newest complete anchor among them, A. Because anchors are hourly,
   A is at most about 65 minutes before `t` on a healthy host. With no
   complete anchor in that window, the host's state at `t` is **unknown**, not
   empty.
3. Apply, in `as_of` order, every complete delta whose `anchor_as_of` equals
   A's `as_of`. For each repo entry: if it has `ready_replace: true`, first
   drop **every** `ready_wait` row held for that repo (once per record, before
   any of the record's rows for the repo, since a repo's entries may span
   chunks); then drop the `removed` issues, upsert the `rows` by issue, and
   replace the census and `ready_complete`. A repo left with no rows and no
   census is dropped. Never infer a `ready_wait` removal from a row's absence
   except through this replace rule: without `ready_replace` (a
   `ready_complete: true` repo, or an older emitter) only `removed` removes.
4. Check the chain. Each applied delta's `prev_as_of` must equal the `as_of`
   of the record applied before it. On a break (a delta lost, incomplete, or
   not yet knowable), the state is exact only up to the break. Report it as
   partial rather than guess.

### Reconciling hosts

Hosts' views overlap by design: each manages a set of repos, sees their review
listings, and ranks the ready queue by its own planner. Per `(repo, issue)` at
`t`:

1. Take the row from the host that holds the item (the row with a `host`).
2. Else take any host's PR-stage row; else any host's `ready_wait` row. Its
   `rank` is that host's rank; ranks are per host, so two hosts' ranks are two
   true answers, not a conflict.
3. Record the spread between hosts' views, and between them and the
   webhook-derived label state, as a coverage/lag measure. A host whose view
   lags the forge (for example a rate-limited listing cache) is measured, not
   deduped away.

A host restart begins a new chain with a fresh anchor. Records from before the
restart never chain into it, because their `anchor_as_of` differs.

## How to run a replay

```bash
loom-daemon telemetry-replay --as-of 2026-10-04T13:00:00Z \
  --endpoint https://clickhouse.example:8443 --user reader \
  --credential-file ~/.config/loom/signoz-read.key   # owner-only (chmod 600)
```

It runs queries 1 (state) and 3 (coverage) of `replay-queries.sql` exactly as
committed (`include_str!`, nothing re-typed), binding `t`, `window`
(`--window-sec`, default 3900) and `repo` (`--repo`, default all). It prints
every emitting host as `covered`, or `unknown` with the SQL's reason
(`broken_chain`, `incomplete_anchor`, `incomplete_delta`, `missing_anchor`,
`no_anchor`), then every reconstructed item; `--json` prints the same as JSON.
An uncovered host is never shown as empty. No row is capped and no host is
elected; it computes no estimate or statistic.

- **Endpoint config**: flags, then `telemetry.signoz.{endpoint,user,credentialFile}`.
  The old `autonomous.eta.fleetRefresh.signoz.*` key is read as a fallback for
  one release, with a deprecation warning (it goes with #11098).
- **Offline**: `--print-sql` prints both queries for `clickhouse-client
  --param_t=… --param_window=… --param_repo= --format JSONEachRow`; feed the
  combined output back with `--from-file`.
- **`t` is UTC**, bound as a `DateTime64(3)` parameter; the store's server
  timezone must be UTC (as SigNoz deploys it).
- **The SQL decides.** Where the committed SQL and the prose above differ
  (a broken chain is `unknown` in the SQL, "partial" in step 4; the SQL does
  not yet apply `ready_replace`), replay reports what the SQL returns. Fix the
  SQL, not the reader.
- **Tests**: `tests/telemetry_replay_fixture_store.rs` runs the command's own
  queries and reader over R3's fixture store in a pinned ClickHouse.

The reader is the neutral client in `loom-daemon/src/signoz_read.rs` (#11127):
`ClickhouseHttp` (bound `param_*` parameters, credential read from an
owner-only file at call time and never logged) and `FileRows`. Nothing in it
or in the replay command depends on `eta/`, so both survive the ETA
subsystem's removal (#11098).

## Not yet implemented

- `loom-daemon telemetry-replay --check` (#11128).
- `fleet.state` hold and capacity facts (slice R8) and the committed
  volume/coverage ClickHouse query (bytes/day, rows per anchor, anchors
  missing chunks, hosts with no anchor in 2 h).
