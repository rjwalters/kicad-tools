# Telemetry Replay Contract

Status: contract and emit-side facts only (Issue #10196, slice 1). The
`fleet.state` kind, `loom-daemon telemetry replay --as-of <t>` and `--check`
are later slices and do not exist yet.

The question this contract answers: **what did the fleet look like at instant
`t`, as a daemon running at `t` could have known it?** ETA backtesting
(#10193) and any retroactive analysis depend on it.

## Two clocks

Every log record has two instants. They are different facts and must never be
conflated.

| Clock | OTLP column | Meaning |
|-------|-------------|---------|
| **Event time** | `timestamp` (`time_unix_nano`) | When the thing happened (usually the envelope's `emitted_at`; `eta.*` and `session.output` override it with their own source instant). |
| **Knowable-at** | `observed_timestamp` (`observed_time_unix_nano`) | When the record became *available to a reader*. |

Delivery is batched, retried and at-least-once, so a `sweep.outcome` can reach
SigNoz long after its event time. A query over `timestamp < t` returns rows
that nothing could have seen at `t`.

### The rule: replay filters on knowable-at

A reconstruction at `t` uses only records whose **knowable-at instant is
`< t`**. Filtering on event time is a bug: it leaks the future into the past.
Event time orders and groups records inside the reconstruction; it never
decides membership.

### Current state of the knowable-at column (design decision)

Today the OTLP exporter sets `observed_time_unix_nano` to a copy of
`emitted_at` (`log_record_for` in `observability/otlp/mapping.rs`); only
`session.output` overrides it, with the producer's read time. That is a
**producer-side** value, so it is not an ingest-side knowable-at: a record
delayed in the export queue still claims to have been observed at its event
time. There is nothing ingest-side for the exporter to preserve.

Decision: a true knowable-at is a **collector-side receive stamp** (a
collector processor that writes the receive time, as an attribute or into
`observed_timestamp` for records where the producer value is only a lower
bound). A producer-side `exported_at` would be a lower bound at best and is not
adopted. Until the collector stamp lands, a reader MUST treat
`observed_timestamp` as a lower bound on knowable-at and may only claim
point-in-time correctness up to the export latency. The collector change is a
later slice; this slice records the decision so the replay reader is written
against the right column.

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

- `fleet.state` full-state snapshots with an hourly anchor.
- `loom-daemon telemetry replay --as-of <t>` and `--check`.
- The collector-side receive stamp.
