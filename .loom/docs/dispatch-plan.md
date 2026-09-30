# Dispatch plan: per host, and across the fleet

The **dispatch plan** is the order Loom will work in, published as a
projection of the work finder's real tick. It has two layers, and they answer
different questions:

| Layer | Question | Issue | Implementation |
|-------|----------|-------|----------------|
| Host-local plan | "What is *this* daemon going to start next?" | #9288 | `loom-daemon/src/work_finder/dispatch_plan.rs` |
| Fleet merge | "What is *the fleet* working on, when several hosts list the same issue?" | #9310 | `loom-daemon/src/work_finder/dispatch_plan_merge.rs` |

The host-local layer is documented with the rest of the queue surface in
[`daemon-reference.md`](daemon-reference.md) ("Dispatch plan (#9288)") and
field-by-field in [`telemetry-schema.md`](telemetry-schema.md) under
`queue.snapshot`. This document is about the second layer: the fleet merge.

## Why a merge rule at all

Every host publishes its own plan. A row's `position` is a 1-based index into
*that host's* shaped pass-2 order, after its repo-slice and per-repo-cap
shaping — so two hosts' positions are not on one scale. Host A with three
repos and host B with thirty both have a `position: 1`, and they mean
completely different things.

Meanwhile the same issue appears on every host that manages its repo: host A
runs it (`plan_state: running`), host B sees it held by a peer
(`plan_state: blocked`). A fleet view that simply concatenated the hosts'
rows would count that issue twice, call it blocked, and interleave the two
hosts by whichever happened to see more work.

`merge_plans(&[HostPlan]) -> FleetPlan` is the one rule that resolves both.

## The rule

### 1. Fold by `repo#issue`

Rows are grouped by `owner/repo` plus issue number — not the bare issue
number, which collides across repos. A row with no repo or issue (a private
row withheld from the public dashboard view) has nothing to fold on and stays
one item per row.

### 2. Pick the primary

The **primary** is the observation that classifies the item for the fleet.
Among the hosts that list the same issue, in order:

1. **Most advanced `plan_state`** — `running` > `next` > `queued` >
   `blocked`. An `unknown` state (a newer daemon's state this build does not
   know) ranks last, so it never displaces a state that is understood.
2. **The shard owner** — the host whose `plan.shard.host_shard` equals the
   row's `owning_shard`. That is the host whose job the issue actually is
   (see [`dispatcher-repo-sharding.md`](dispatcher-repo-sharding.md)). An
   unsharded host owns nothing, so an unsharded fleet falls straight through
   to the next key.
3. **Lower `position`** — within one state band, the host that has it nearer
   the front of its own plan. A row with no position (outside that host's
   plan this tick) sorts behind every row that has one.
4. **Lower host id** — the final tie-break, so the result never depends on
   the order the hosts were merged in.

Every other host's observation stays attached to the item as `others`,
best-first by the same rule, so a UI can still show "host-b: held by a peer".

### 3. Order the fleet

1. **`plan_state` band** — every `running` item, then every `next`, then
   `queued`, then `blocked`, then `unknown`.
2. **Round-robin interleave** within each band. Each host's items are taken
   in that host's own `position` order and handed out round by round: round
   *k* of every host precedes round *k+1*, and hosts within a round go by
   host id.

The interleave is what keeps two hosts' positions from being compared as one
scale. It also means one host's long backlog never buries another host's
first row:

```
host-a (queued): #600 pos 1, #601 pos 2, #602 pos 3
host-b (queued): #700 pos 1

fleet order:     #600 (a, round 0), #700 (b, round 0), #601 (a, round 1), #602 (a, round 2)
```

## What the merge deliberately does not compare

**`workspace_priority` is per-host and unsynced.** It comes from each host's
own `.loom/config.json`, so host A's `priority: 100` and host B's
`priority: 100` are two unrelated operators numbering their own repos.
Comparing them across hosts would let one host's local numbering silently
reorder another host's queue. It is not a merge input — not in
`merge_plans`, and not in the dashboard port. Within a host it has already
done its work: it is one of the comparator keys that produced `position`,
which is exactly what the interleave consumes.

**Wall-clock inputs** (`created_at`, and how long ago an observation
arrived) are not merge inputs either, for the same reason: each host already
applied them inside its own comparator. Re-applying them across hosts would
double-count age and would make the merged order depend on telemetry
latency.

The merge is **pure, total and deterministic**: the output depends only on
the input slice, never on its order, and an empty fleet merges to an empty
plan. A single-host fleet is a pass-through — every row becomes one item with
no `others`, in that host's own plan order.

## Two implementations, one fixture

The rule is implemented twice, because the daemon and the browser both need
it:

- `loom-daemon/src/work_finder/dispatch_plan_merge.rs` — `merge_plans`, over
  the `HostPlan` / `FleetPlan` types in
  `loom-daemon/src/types/fleet_plan.rs`.
- `dashboard/web/src/workQueue.ts` — `mergeFleetQueue`, over the per-host
  `queue.snapshot` records the fleet backend stores.

They are pinned to the same JSON fixture,
`dashboard/test/fixtures/dispatch-plan-merge.json`, which
`loom-daemon/src/work_finder/dispatch_plan_merge_tests.rs` reads with
`include_str!` and `dashboard/web/test/workQueue.test.ts` imports. Neither
side owns the fixture: a change to the rule has to change the fixture, and
that fails the *other* language's test until it is ported too. (The same
cross-language fixture pattern as
`dashboard/test/fixtures/sweep-identity.json`.)

**To change the rule**: edit the fixture case that pins the behaviour, then
make both implementations agree with it, then update this document. Do not
add a second ordering rule in one language only — that is precisely the
drift the shared fixture exists to prevent.

## Pre-#9288 rows

A daemon older than #9288 sends rows with no plan fields at all. The
dashboard port treats those rows as follows, so a mixed-version fleet still
merges sensibly:

- `plan_state` falls back to the row's coarse `state`: `running` stays
  `running`, `ready` becomes `queued` (it is waiting in the plan), `blocked`
  stays `blocked`.
- `position` falls back to `rank`, the only dispatch order such a row
  reports. A row that *does* carry a plan but no `position` is genuinely
  outside its host's plan and sorts last instead.
