# Dispatcher Repo-Sharding: preferred slice + work-conserving fallback (Issue #6243)

Multiple `loom-daemon` autonomous work-finders across hosts draw from the
same per-repo candidate list (`work_finder::tick_multi*`). Every host
evaluates every workspace on every tick, so as fleet size N grows, the
probability that two hosts pick the same top candidate in the same tick
window grows with it — even though the peer-claims advisory mesh
(`peer_claims.rs`) and `Dispatcher::collisions()` (#4085) already detect and
count these races after the fact. **Detection is not prevention.**

This document describes the structural mitigation: each dispatcher **prefers**
a deterministic, disjoint slice of the fleet's repos, and reaches outside it
only when its own slice has no eligible work — so "simultaneous tick on the
same issue" becomes the much rarer "simultaneous slice-exhaustion on the same
issue." It composes with (does not replace) the peer-claim advisory mesh and
epic #6165's lease-ordering work — see "Relationship to other coordination
mechanisms" below.

## Design decision: reuse #6374's host ring, do not derive a second one

The proposal's original inputs were "the manifest's `fleet_priority` ordering"
and "a stable host index (e.g. round-robin by rank)". Neither exists literally
in the codebase, and the issue asked for an explicit, documented decision on
what to use instead. Two candidate derivations were considered:

- **(a) Derive a live host rank from the advisory mesh's observed peer
  roster** — sort the hosts `peer_claims::PeerClaimView` has heard from, take
  this host's ordinal, and round-robin repos (ordered by
  `Workspace::priority`) across those ranks.
- **(b) Reuse the operator-declared host ring `role_shard` already
  establishes** (`loom-daemon/src/role_shard.rs`, Issue #6374): each host
  carries a `shardIndex` in `0..shardCount`, and a workspace is owned by the
  single host where `fnv1a64(shard_key) % shardCount == shardIndex`, keyed on
  the repo's cross-host-stable identity (its NWO — see
  `role_shard::resolve_shard_key`).

**This implementation takes (b).** The decisive argument against (a) is the
one #6374's own module docs make, and it applies verbatim here:

> two hosts with different views of roster membership disagree about the ring
> *size*, and therefore about every workspace's owner, not just the departed
> host's slice.

A roster-derived rank is only as disjoint as the roster is converged, and the
roster converges by observing peer traffic — precisely the eventually-
consistent channel whose races this issue exists to reduce. Under (b),
disjointness is **a property of arithmetic, not of a protocol that can race**:
every host computes the same hash over the same key, and the indices partition
`0..shardCount`, so exactly one host prefers each repo by construction.

Two further consequences of picking (b):

1. **`Workspace::priority` is not a slicing input at all.** The issue's
   curator note flagged that `Workspace::priority` is per-host-registry state
   (`~/.loom/workspaces.json`), populated independently per host and never
   synced — so using it as a slice-derivation key would have needed either an
   unverified fleet-consistency assumption or a whole new synced ordering.
   Reusing `role_shard`'s key sidesteps the question: the shard key is the
   repo's NWO (identical everywhere by construction) and the ring size is
   explicit, operator-declared fleet configuration. `Workspace::priority`
   keeps its existing, unchanged job — ordering candidates *within* whatever
   set this tick is allowed to dispatch from (`candidate_cmp`, #3946).
2. **No new configuration knobs.** `shardIndex` / `shardCount` / `shardKey`
   already exist with a reviewed operator contract (`shardIndex` host-local
   only; `shardCount` and `shardKey` fleet-wide and safe to commit; a
   `shardIndex` found in the *tracked* `.loom/config.json` is refused
   outright). Introducing a second, independent ring for the dispatcher would
   let one fleet declare two contradictory rings, with no demonstrated need.
   See `defaults/docs/daemon-reference.md` for the knobs themselves.

## The mechanism

### 1. Slice membership (`role_shard::decide`)

Once per tick, `work_finder::spawn_multi_work_finder_task` maps each managed
root to `role_shard::decide(root).owned` — a `Vec<bool>` mask parallel to the
tick's workspace list. Nothing new is computed here; this is the same verdict
the role runner consults.

`role_shard`'s fail-safe direction carries over unchanged and is exactly what
this use wants: **every** unconfigured, malformed, incomplete, single-shard,
out-of-range, or tracked-`shardIndex` case resolves to
`ShardPosture::Unsharded`, which owns **every** workspace. So a host that has
not opted into sharding gets an all-`true` mask, which the tick treats as
byte-for-byte pre-#6243 behavior.

### 2. Slice preference in the tick loop (`work_finder::tick_multi_with_sharding`)

`tick_multi_with_saturation_brake` (the pre-#6243 entry point every existing
caller and test uses) now delegates to a new
`tick_multi_with_sharding(..., preferred_slice: Option<&[bool]>)`, passing
`None` — behavior unchanged, so every existing `candidate_cmp` / `tick_multi`
priority-ordering test needed no modification.

When `preferred_slice` is `Some`, the already-globally-sorted candidate list
is partitioned into in-slice / out-of-slice, preserving each partition's
relative order:

- **In-slice partition non-empty** — pass 2 dispatches ONLY from it this tick;
  every out-of-slice candidate is deferred and counted in
  `TickReport::deferred_out_of_slice`. This is a hard preference, not a
  sort-key nudge: a lower-priority in-slice repo holds a shared dispatch slot
  ahead of a higher-priority out-of-slice repo for as long as this host has
  ANY in-slice candidate at all.
- **In-slice partition empty** — pass 2 falls back to the full out-of-slice
  queue. **Work-conservation**: an idle slice must never leave ready work
  undispatched while sitting on spare concurrency budget. This is the #6243
  AC's "empty-slice host continues to drain the global queue" requirement.

`candidate_cmp`'s ordering guarantees hold **within** either partition
unchanged — sharding only ever changes *which* candidates are eligible this
tick, never how they are ordered against each other. A deferred candidate is
never dropped: it stays ready and is re-evaluated on the next tick.

Since #9090 both partitions live in `work_finder::repo_cap::shape_queue`
(`work_finder.rs` is at its file-size ratchet), and
`tick_multi_with_sharding` is itself the thin `max_concurrent_per_repo: None`
wrapper over `tick_multi_with_repo_cap`. Nothing above changes.

### How this composes with the per-repo cap (#9090)

`autonomous.workFinder.maxConcurrentPerRepo` adds a second, independent
repo-level term at this same layer. The two are applied in a fixed order and
answer different questions:

| | Repo sharding (#6243) | Per-repo cap + affinity (#9090) |
|---|---|---|
| Question | *which host* should prefer this repo | *how much of one host's budget* one repo may hold |
| Scope | cross-host (a shard ring over repo NWO) | single-host (this daemon's own budget) |
| Mechanism | stable partition: in-slice ahead of out-of-slice, hard preference | stable partition: hot repos ahead of cold (*ordering*) **plus** a deferral at admission (*bound*) |
| Order applied | **first** — on the globally sorted list | **second** — affinity reorders the slice partition's result; the cap gates admission last, in pass 2 |
| Counter | `TickReport::deferred_out_of_slice` | `TickReport::deferred_repo_cap` |
| Default | unsharded ⇒ every repo in-slice (no-op) | absent ⇒ uncapped, affinity off (no-op) |

They cannot fight: sharding decides the candidate *set* for this host,
affinity reorders within that set, and only the cap refuses an admission. A
sharded fleet with no `maxConcurrentPerRepo` behaves exactly as it did before
#9090, and an unsharded host with a cap set gets the cap without any slice
partition running at all. Full narrative: `daemon-reference.md` §"Per-repo
dispatch cap + track affinity (#9090)".

### Preference here, hard filter there

This is the one place the dispatcher deliberately diverges from the role
runner over the same `owned` verdict:

| | `role_runner` (#6374) | `work_finder` (#6243) |
|---|---|---|
| Unowned workspace | never ticks here | deferred behind owned ones |
| If this host owns nothing | it runs no role ticks | it still drains the global queue |
| Failure direction | duplicate, never drop | reorder, never drop |

The role runner can afford a hard filter because a role tick is periodic and
idempotent — skipping one costs a cadence, not a unit of work. Dispatch is
not: refusing to dispatch a ready issue because no local repo hashes to this
host's index would idle a host with free capacity while work sits in the
queue. Hence the fallback, and hence a sharding misconfiguration can only ever
*reorder* this host's dispatch, never stop it.

## Limits: sharding is repo-granular, so it cannot help a single-repo fleet

The slice unit is a **repo**, not an issue. A fleet of `H` hosts managing `R`
repos therefore only narrows the collision window when `R > 1`; the degenerate
cases behave as follows, both by design:

- **Unsharded host (including any single-dispatcher fleet)** — owns every
  workspace, all-`true` mask, pre-#6243 behavior exactly.
- **`R == 1` with `shardCount > 1`** — the one repo hashes to a single index.
  Every other host computes an empty slice and takes the work-conservation
  fallback on **every** tick, i.e. dispatches from the global queue exactly as
  it did before #6243. Sharding is a no-op here, correctly: there is nothing
  to shard, and starving `H-1` hosts of the fleet's only repo would be a
  severe work-conservation regression for no collision benefit.

Reducing collisions *within* a single repo needs issue-granular partitioning
(e.g. by `issue_number % shardCount`), which trades away FIFO/priority
ordering *within* a repo — a materially different and more invasive design.
Fleets in that shape are covered by the pre-existing layers instead: the
peer-claim advisory mesh, `detect_and_record_collision`, and (when it lands)
epic #6165 Phase 2's atomic claim authority.

## The new counter: same-issue cross-host claims

`Dispatcher::collisions()` / `TickReport::collisions` (#4085) is a
**monotonic, never-pruned, per-process-lifetime total**, summed across every
workspace this daemon manages — useful as a baseline collision rate, but
unusable for this issue's own verification AC ("zero cross-host claims on the
SAME issue over a 24h observation window"), since it never resets and carries
no per-issue or per-time detail.

`peer_claims::PeerClaimView` therefore gained a second, purpose-built counter:

- `record_same_issue_collision_at(repo, issue, now)` — pushes a
  `(now, repo, issue)` triple into a rolling window (default 24h,
  `DEFAULT_SAME_ISSUE_COLLISION_WINDOW`), pruning entries already outside the
  window on every write so a long-running daemon stays bounded. Called from
  `SweepRegistry::detect_and_record_collision` (`sweep_registry/guards.rs`) on
  every CONFIRMED `CollisionClass::Collision` — the identical detection event
  that already increments `collision_count`, just also recorded here with its
  issue number and timestamp. A no-op when no view is attached (safehouse
  disabled), matching that function's existing fail-open posture.
- `same_issue_collision_count(now)` — how many entries fall within the rolling
  window as of `now`.

Surfaced on the wire as `PeerClaimStatus::same_issue_collisions` (`types.rs`,
`#[serde(default)]` so older clients and stored payloads still deserialize)
and rendered by `health::assess_peer_coordination`:

- JSON detail: `same_issue_collisions_24h`, always present.
- Human summary: appended **only when non-zero**, so a healthy fleet's
  rendering — and every pre-#6243 assertion on that wording — is unchanged,
  while a fleet that IS colliding says so without needing `--json`.

The section's DEGRADED/Green **verdict is deliberately unchanged**. That
verdict answers #6157's mesh-liveness question ("is the peer-claim receive
path alive"), a different question from "did sharding actually keep same-issue
cross-host claims rare". Building a verdict for this counter (e.g. DEGRADED
above N collisions in 24h) belongs to #6242 and is intentionally not
duplicated here.

## Relationship to other coordination mechanisms

- **`role_shard` (#6374)** — the host ring this reuses. Same key, same
  arithmetic, same fail-safe direction; only the strength of the verdict
  differs (see the table above).
- **Peer-claim advisory mesh (`peer_claims.rs`, #4028)** — a fast,
  eventually-consistent soft-claim broadcast. Sharding reduces how often two
  hosts *reach* the point of racing a claim on the same issue; the mesh still
  catches races that do occur, unchanged.
- **`Dispatcher::collisions()` / forge pre-flip detection (#4085)** — detects
  a race that got past the mesh, after the fact. Unchanged; sharding reduces
  its baseline rate, not its mechanism.
- **Epic #6165 Phase 2 (atomic claim authority / CAS semantics)** — not yet
  landed. When it does it becomes the final arbiter for a claim race; sharding
  composes with it (frequency reduction vs. final-arbiter correctness) and
  does not block on it.
- **Dynamic ring membership (#6704)** — reassigning a dead host's slice
  automatically is deferred there, for `role_shard` and therefore for this
  consumer too. Until it lands, a host leaving the fleet does not shrink the
  ring; for the dispatcher that is harmless, because the departed host's repos
  simply become out-of-slice-for-everyone and are picked up by the
  work-conservation fallback.

## Verification status

- **Automated (in this change)**: slice-preference over a higher-priority
  out-of-slice repo; empty-slice fallback (work-conservation); a short/missing
  mask defaulting to in-slice; `tick_multi_with_saturation_brake` proven to be
  a sharding no-op; the collision counter's window, its wiring from
  `detect_and_record_collision`, and its health rendering (zero and non-zero).
  `role_shard`'s own disjointness/coverage properties are already covered by
  its 1300-line test module from #6374 and are not re-tested here.
- **NOT performed**: the issue's "2 dispatchers, full queue, zero same-issue
  cross-host claims over a 24h window" criterion requires a real multi-host
  live run and cannot be demonstrated inside one PR's review cycle. The
  mechanism and the counter that observation reads are delivered here; the
  observation itself is follow-up operational work.

## Files touched

- `loom-daemon/src/work_finder.rs` — `tick_multi_with_sharding` (new;
  `tick_multi_with_saturation_brake` delegates to it with `None`),
  `TickReport::deferred_out_of_slice`, and the per-tick mask in
  `spawn_multi_work_finder_task`. (#9090 moved the partition itself into
  `work_finder/repo_cap.rs` and made `tick_multi_with_sharding` a wrapper
  over `tick_multi_with_repo_cap`; the behavior described above is unchanged.)
- `loom-daemon/src/peer_claims.rs` — the same-issue collision window
  (`record_same_issue_collision_at` / `same_issue_collision_count`).
- `loom-daemon/src/types.rs` — `PeerClaimStatus::same_issue_collisions`.
- `loom-daemon/src/sweep_registry/guards.rs` —
  `detect_and_record_collision` feeds the new counter alongside the existing
  monotonic one.
- `loom-daemon/src/health.rs` — `assess_peer_coordination` surfaces the
  counter (JSON always, summary when non-zero); no verdict change.
