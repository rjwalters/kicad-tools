# Dispatcher Repo-Sharding (Issue #6243) — retired 2026-10-13

**Status: retired from dispatch by operator ruling on 2026-10-13
([#11682](https://github.com/rjwalters/loom/issues/11682)).** The shard ring
itself is not retired: role runners still use it
([`role-runner-roster.md`](role-runner-roster.md), #6374).

## What it was

Each host's work finder preferred a disjoint *slice* of the fleet's repos: the
repos whose key hashed to this host's `shardIndex` on the `role_shard` ring
(#6374). After the workspace draw, `repo_cap::apply_slice` deferred every
out-of-slice candidate (`deferred_out_of_slice`, gate `out_of_slice`) whenever
the slice had at least one candidate at the top of the tick, and fell back to
the whole queue only when the slice was empty. The aim was to make two hosts
picking the same issue in the same tick rarer.

## Why it went

1. **It starved hosts that had free slots.** The slice decided "my slice is
   non-empty" before pass 2 ran. Pass 2 could then reject every in-slice
   candidate without dispatching any: an open linked PR (#8865, moved into pass
   2 on 2026-10-11), the per-repo cap (#9090), or the build back-off (#9410).
   The host then dispatched nothing while out-of-slice work waited. On
   2026-10-13 around 00:40Z, three workers with 7–12 free slots each had
   152–234 out-of-slice candidates deferred and dispatched 0–2 per tick. Per-
   worker dispatch fell from about 14/h to 0–3/h.
2. **Slices are uneven by construction.** The hash spreads repo *names*, not
   ready work. A large backlog lands on one host, and a broken owner takes its
   whole slice down with it (#11681).

## What prevents duplicate dispatch now

- **The weighted workspace draw** (#11103) decides dispatch order on every
  host. Each tick draws from a fresh seed, so two hosts rarely reach the same
  candidate first.
- **The forge lease** (#11112) is the single cross-host claim. A host that
  loses the lease race is refused at dispatch (`lease_order_lost`), so two
  hosts cannot both run the same issue.
- The peer-claim mesh (#4028) and the collision counters (#4085,
  `same_issue_collisions_24h`) still detect any race that gets through.

## What changed on the wire

The retired values are no longer emitted. No field or metric name was
removed (see [`telemetry-schema.md`](telemetry-schema.md)):

- `deferred_out_of_slice` (queue disposition) and `out_of_slice` (plan gate)
  are never emitted. Readers parse a historical record carrying them as
  `unknown`.
- `in_slice` on dispatch-plan rows is absent.
- The `loom.dispatch.decisions` counter has no `reason="out_of_slice"` series
  (it only ever emitted non-zero reasons).
- The tick line and the `axis` line no longer print a slice count.

`owning_shard` on plan rows and the plan's `shard` block are kept: the fleet
merge uses `owning_shard` to choose a row's primary host
([`dispatch-plan.md`](dispatch-plan.md)).
