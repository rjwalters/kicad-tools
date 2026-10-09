# Telemetry Replay Contract

Status: contract, emit-side facts and committed replay SQL (Issue #10196,
slices 1 and R3/R4). The `fleet.state` kind, `loom-daemon telemetry replay
--as-of <t>` and `--check` are later slices and may not exist yet.

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

## Not yet implemented

- `fleet.state` full-state snapshots with an hourly anchor (#10283).
- `loom-daemon telemetry replay --as-of <t>` and `--check`.
