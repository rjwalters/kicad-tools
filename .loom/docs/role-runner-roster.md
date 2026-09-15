# Role-Runner Roster: forge-backed membership + generation-fenced ring (Issue #6704)

**Status: design record.** This document is the decision half of Issue #6704.
It picks **one** roster backend and **one** fencing rule, states the windows
that rule leaves open, and argues they are acceptable. It ships **before** any
behavior change, so the choice is reviewable on its own — the implementation
lands in two follow-up PRs (see [Phasing](#phasing-two-implementation-phases)).

Nothing in this document is live yet. `role_shard.rs` on `main` is still the
static ring described in
[`daemon-reference.md` → Role-runner host sharding](daemon-reference.md#role-runner-host-sharding-6374).

## The gap this closes

[#6374](https://github.com/rjwalters/loom/issues/6374) shipped a **static**
shard assignment: each host carries `LOOM_ROLE_RUNNER_SHARD_INDEX` within a
fleet-wide `autonomous.roleRunner.shardCount`, and a workspace's role rotation
runs on the host where `fnv1a64(shard_key) % count == index`. Exactly one host
owns each workspace **by construction** — no election, no lease, no window with
zero or two owners. That arithmetic guarantee is what makes it a structural fix
for the #6332 / #6352 cross-host duplication class.

The cost: the ring does not know which hosts are alive. **Killing a host does
not reassign its slice.** Its workspaces stop rotating until an operator lowers
`shardCount` or points a survivor at the vacated index. #6374's AC1 asked for
automatic reassignment "within a bounded window"; that half was deferred here
because the naive implementations reintroduce exactly the races the static
scheme rules out:

> two hosts with different views of roster membership disagree about the ring
> *size*, and therefore about **every** workspace's owner, not just the
> departed host's slice.
>
> — `loom-daemon/src/role_shard.rs` module docs

Any design here has to say what happens *during* that disagreement. "Duplicates
briefly" is a regression against #6374's entire point.

## Decision

1. **Backend: forge-backed roster.** Each host publishes a heartbeat record as
   a comment on one designated **roster issue**, following the lease-record
   idiom already in production ([`lease-record.md`](lease-record.md), Epic
   #6165). Membership is a pure function of those comments' **forge-assigned
   `updated_at`** timestamps.
2. **Fencing: a generation-fenced ring.** The membership set carries a
   *generation* that is itself derived from forge-assigned timestamps, so every
   host that has read the same comment set computes the identical generation. A
   host may admit a role tick only under the **newest generation it has
   observed**, and only once that generation has been **settled** for a full
   role-tick interval — so during a membership disagreement the host holding
   the newer view *yields* rather than both hosts acting.

Rank and ring size derived from the roster feed the **existing**
`ShardPosture::Sharded { index, count }`. The hashing (`fnv1a64`), the
`owns(key)` arithmetic, the shard-key resolution, the `status` rendering, and
#6243's dispatcher consumer are all unchanged — only the *source* of
`(index, count)` becomes dynamic.

## Why forge-backed

The forge is the coordination substrate **every host already reads and writes**
for the work it is coordinating. Three properties decide it:

- **A shared clock for free.** Every host reads the *same* server-assigned
  `updated_at` for the same comment. `peer_claims.rs` timestamps at local
  receipt and has to correct for skew; a forge comment does not have that
  problem. This is the same argument [`lease-record.md`](lease-record.md) makes
  for the reclamation gate, and it is what makes the eviction instant
  *computable identically on every host* (see
  [Why the fence holds](#why-the-fence-holds)).
- **No new dependency.** No shared filesystem, no new port, no new service,
  nothing to provision on a host that is already running a daemon and a `gh`
  credential.
- **Proven in this repo.** The same marker-comment idiom already carries lease
  records (`<!-- loom:lease host=… sweep=… -->`), peer-claim standdowns
  (`<!-- loom:standdown claim=… -->`), and Champion's merge-risk hold digest
  (a pinned issue overwritten in place, #6877). Phase 2 of Epic #6165 made a
  forge comment's `updated_at` a **load-bearing** liveness signal for claim
  reclamation; this reuses the mechanism that is already trusted with that
  decision.

### Rejected: a file on shared storage

A lease file on NFS / S3 / a shared volume.

**Rejected** because the fleet has no such storage and would have to grow one.
The hosts are on different networks (a studio Mac, a MacBook Pro, EC2 workers);
introducing a shared mount to coordinate *role ticks* adds a dependency whose
own failure modes (stale NFS handles, unreliable POSIX locking over NFS,
credential rotation for an object store) are strictly worse than the failure
mode being fixed. Critically, it also **loses the shared clock**: file mtimes
come from whichever host wrote them, so a skewed writer's heartbeat is
indistinguishable from a fresh one, and the eviction-determinism argument
below collapses.

### Rejected: daemon-to-daemon (the safehouse peer mesh)

Deriving the roster from `peer_claims.rs` — the hosts this daemon has recently
heard advertisements from over the safehouse room.

**Rejected on four independent grounds:**

1. **This repo already rejected it, in writing, for the adjacent problem.**
   [`dispatcher-repo-sharding.md`](dispatcher-repo-sharding.md) (#6243)
   considered deriving host rank from the observed peer roster and chose
   #6374's declared ring instead — "a roster-derived rank is only as disjoint
   as the roster is converged, and the roster converges by observing peer
   traffic — precisely the eventually-consistent channel whose races this issue
   exists to reduce." Building the opposite answer here would leave the two
   sharding consumers on contradictory foundations.
2. **Its liveness signal was removed from load-bearing use on purpose.**
   Epic #6165 Phase 4 (#6317) deleted the peer-coordination-DEGRADED gate
   (#6157) from the reclamation decision path and replaced it with the lease
   record. The mesh is *advisory* by current design; promoting it back to
   load-bearing for the ring would reverse a shipped decision.
3. **It is unreliable in practice.** The recurring peer-claim DEGRADED alerts
   on the fleet's Linux worker are exactly the "I stopped hearing from peers"
   false signal that would silently resize the ring.
4. **It is off by default.** `safehouse.enabled` defaults to `false`, so a
   roster built on it would be unavailable on a default install — and the
   escape hatch for that would be… the static ring we are trying to replace.

### Rejected: leader election / compare-and-swap on a shared record

A single elected coordinator assigning slices, or a CAS-guarded ring document.

**Rejected** because the forge offers no compare-and-swap primitive on an issue
body or comment, so a real CAS needs a new store — which lands back in the
shared-storage rejection above. Election also makes the coordinator a new
single point of failure whose own liveness needs the very roster it is
supposed to publish.

### Rejected: status quo (operator re-shards by hand)

Keep the static ring; the operator lowers `shardCount` when a host dies. This
is what happens today, and it fails AC2 outright: the reassignment window is
"until a human notices," which on a fleet running unattended overnight is
hours.

## The roster

### Record shape

One designated **roster issue**, configured fleet-wide
(`autonomous.roleRunner.roster.issue`, e.g. `owner/repo#1234`). Each host owns
exactly **one** comment on it, whose literal first line is the marker:

```
<!-- loom:roster host=<opaque-host-id> serves=<key-digest>,<key-digest>,… -->
```

- **`host`** — the same opaque id lease records publish
  (`opaque_host_id(host_identity())`, #6322): `host-` + the first 8 hex chars
  of `sha256("loom-lease-host-id-v1:" + host_identity())`. Readers only ever
  need `==`, never readability, and a roster comment is permanent public record
  on a public repo exactly like a lease comment.
- **`serves`** — the set of workspace shard keys this host has registered, as
  `fnv1a64` digests in lowercase hex, sorted ascending. Digests rather than
  names for the same privacy reason as `host`, and because equality is all a
  reader needs. ~27 workspaces × 17 bytes ≈ 460 bytes — one comment, no
  pagination concern.

Everything after the marker is free-form prose for a human reading the issue.
Machine readers locate the record by `.starts_with("<!-- loom:roster host=")`
and **must never** parse the prose — the same contract
[`lease-record.md`](lease-record.md) establishes.

**`serves` is load-bearing, not decoration.** A fleet-wide ring that ignored it
would be *wrong*: host B, registered only for repo X, would be handed a slice
of repo Y's rotation and Y would silently stop rotating. Ring membership is
therefore computed **per shard key**: the ring for key `k` is the set of live
hosts whose `serves` contains `digest(k)`. This fits the existing API shape
exactly — `role_shard::decide(root)` is already per-root.

### Heartbeat and the liveness signal

- A host refreshes its own record every `roster.heartbeatSecs` (default
  `300`, matching the lease-renewal cadence) by PATCHing the comment. As with
  lease renewal, the body must change *something* for a forge to reliably
  advance `updated_at` — see [`lease-renewal.md`](lease-renewal.md).
- **The liveness signal is the comment's own forge-assigned `updated_at`, never
  a timestamp embedded in the text.** Same rule, same reason, as
  [`lease-record.md`](lease-record.md).
- The read that locates this host's own comment **is** the roster read. That
  coupling is deliberate: a host that cannot read the roster cannot heartbeat
  it either, so a host with a stale view evicts *itself* within the TTL. It is
  structurally impossible to be a confidently-stale live member.

### Membership and generation, as pure functions

Let `C` be the set of roster comments and `t` an instant. With
`ttl = roster.ttlSecs` (default `900` — 3× the heartbeat, matching the lease
TTL):

```
members(C, t) = { h : created_at(c_h) <= t  AND  t < updated_at(c_h) + ttl }
ring(C, t, k) = { h in members(C, t) : digest(k) in serves(c_h) }   # sorted by host id
index(k)      = ordinal of this host in ring(C, t, k)
count(k)      = |ring(C, t, k)|
```

The **generation** is the most recent membership-boundary instant at or before
`t`:

```
boundaries(C) = { created_at(c_h) }  ∪  { updated_at(c_h) + ttl }   over all h
gen(C, t)     = max { b in boundaries(C) : b <= t }
```

Three consequences make the fence work:

- **`gen` is a forge-assigned instant**, so it is **totally ordered** and
  directly comparable across hosts — no counter to agree on, no coordinator to
  increment it, no split-brain over "which gen 7."
- **Two hosts holding the same `C` compute the same `gen` and the same ring.**
  Disagreement can only come from one host holding an *older* `C`.
- **Eviction boundaries need no fresh read.** `updated_at(c_D) + ttl` is
  computable by any host that has *ever* read D's last heartbeat. Every such
  host evicts D at the **same absolute instant**, modulo clock skew — not
  modulo read cadence. Only *joins* require a fresh read to observe.

## The fencing rule

A role tick for workspace root `r` (shard key `k`) is admitted on this host
only when **all** of the following hold. This predicate sits in
`role_shard::decide`, so both gated dispatch surfaces —
`role_runner::decide_root_tick` (interval cadence) and
`plan_idle_runs` (the `onIdle` edge) — inherit it unchanged.

1. **Self-liveness.** This host's own roster comment was successfully refreshed
   within `ttl`. Otherwise the host cannot know whether the fleet has evicted
   it, so it **yields** (runs no roster-gated role ticks) until its next
   successful heartbeat.
2. **Generation monotonicity.** The host never acts under a generation older
   than the newest it has observed in this process. A read that returns an
   older view (a cached/ETag-stale response, a retry against a lagging replica)
   is discarded, not acted on.
3. **Settle.** `now - gen(C, now) >= roster.settleSecs` (default `900`, one
   full role-tick interval at the longest built-in cadence). A ring that has
   just changed is not yet actionable by anyone.
4. **Join fence.** A host does not act under a ring that includes *itself* as a
   new member until its own first roster comment is `ttl` old. Symmetrically, a
   newly observed peer is admitted into the ring immediately (it only ever
   *shrinks* this host's slice), which is the safe direction.
5. **Ownership.** The existing check: `posture.owns(k)`, i.e.
   `fnv1a64(k) % count(k) == index(k)`.

Only then does the tick run.

### Why the fence holds

The property to establish: **no two hosts act under different rings at the same
instant.**

- **A host stops acting under ring `N` the moment it observes `N+1`** —
  condition 2, with no grace period.
- **A host starts acting under ring `N+1` only `settleSecs` after observing it**
  — condition 3.
- **Every live host observes a boundary within `ttl`.** For an *eviction*, no
  observation is needed at all: the boundary is computable from data every host
  already holds, so all hosts cross it simultaneously (± clock skew). For a
  *join*, condition 4 holds the joiner out for a full `ttl`, and any host that
  failed to read for `ttl` has stopped heartbeating and has self-fenced under
  condition 1.

So the timeline around an eviction is: *(pre-boundary)* everyone acts under
ring `N` → *(boundary)* everyone stops → *(boundary + settle)* everyone acts
under ring `N+1`. The overlap window is empty by construction; what exists
instead is a **gap** — D's slice, and only during `settleSecs`, the whole ring,
runs nowhere.

**The fail-safe direction inverts here, deliberately.** #6374's rule is "when in
doubt, duplicate; never drop," because its doubt is about *configuration* and
the fallback is the pre-#6374 status quo the fleet already survives. Roster
transitions are different: the doubt is about *liveness*, and the two errors are
not symmetric.

| Error | Consequence |
|---|---|
| Brief gap (nobody ticks) | A periodic, idempotent role pass runs one interval later. Self-healing; costs minutes of latency. |
| Brief duplicate (two hosts tick) | Two `claude` sessions race the same forge queue — #6332's docs-PR race, #6352's narration duplication — and burn two accounts' tokens. Not self-healing; produces artifacts that must be cleaned up. |

Given that asymmetry, every roster-mode ambiguity resolves toward **yield**.

## The windows this leaves (AC1, AC2)

### Residual two-owner window: one in-flight role session

The fence is evaluated at **tick admission**. A role session already spawned
cannot be un-spawned. So if a boundary falls immediately after host A admitted a
tick for workspace `w`, and the new ring gives `w` to host B, B may admit its
own tick for `w` once `settleSecs` has elapsed while A's session is still
running.

- **Bound:** one role-session duration, and only for sessions admitted before
  the boundary. With `settleSecs` at one full interval, B's tick starts at
  least `settleSecs` after A's — so overlap requires a session running longer
  than a full role interval.
- **Why it is acceptable:** this is not a *new* exposure. The static ring has
  exactly the same one today whenever an operator re-shards a live fleet
  (changing `shardCount` while sessions are in flight), and the fleet already
  runs that way. It is also the exposure `role_collision.rs` (#4623) was built
  to measure, so it is observable rather than silent.
- **Why it is not worth closing:** closing it means cancelling an in-flight
  agent session mid-run, i.e. discarding partial work and possibly leaving a
  half-finished forge mutation — strictly worse than letting a duplicate pass
  complete and be detected.

### Reassignment window (AC2)

Worst case, from a host's death to a survivor running its slice:

```
ttl  (last heartbeat expires)
  + settleSecs  (the new ring must be quiet)
  + one role interval  (next tick alignment)
```

At the proposed defaults — `ttl = 900s`, `settleSecs = 900s`, interval 300–900s
per role — that is **30–45 minutes**. All three terms are config knobs, and the
floor is set by `ttl`, which cannot safely shrink below ~3× the heartbeat
without making a single missed forge round-trip look like a death.

This is the number to state in `status` and in the release note: *a dead host's
role rotation resumes elsewhere within ~45 minutes, not "never."*

### Total forge unavailability

If no host can reach the forge, every host self-fences under condition 1 and no
role ticks run anywhere. That is the correct outcome, not a degradation: role
ticks *are* forge work — a Curator or Judge pass with no forge does nothing but
burn a token.

## Escape hatches and precedence (AC3)

Nothing in this design removes a knob. Resolution order, highest first:

1. **`LOOM_ROLE_RUNNER=0`** — checked *before* any shard or roster state, as
   today. The blunt kill switch is never weakened or second-guessed.
2. **`LOOM_ROLE_RUNNER_SHARD_INDEX` + `shardCount`** — if the static pair
   resolves to a valid `Sharded` posture, it **wins over the roster**. This is
   the documented escape hatch: an operator who needs a deterministic ring
   (debugging, a deliberately partitioned fleet, a roster outage) keeps the
   #6374 behavior verbatim by setting the env pair.
3. **Roster** — used only when `roster.enabled` is true *and* the static index
   is absent.
4. **Unsharded** — every other case, including `roster.enabled` with an
   unreachable or unconfigured roster issue *at startup*. This preserves the
   #6374 fail-safe for the "never got a roster at all" case; the yield-on-doubt
   rule above applies only once a host has successfully joined a roster and
   then lost it.

The two existing regression tests in `role_runner.rs` —
`role_runner_env_zero_still_disables_the_host_that_owns_the_slice` and
`an_unsharded_host_still_ticks_every_workspace` — pin rungs 1 and 4 and must
pass unmodified.

## Blast radius: the dispatcher also reads this ring

`role_shard::decide(root).owned` has a second consumer:
`work_finder::tick_multi_with_sharding` uses it as the dispatcher's **preferred
repo slice** ([`dispatcher-repo-sharding.md`](dispatcher-repo-sharding.md),
#6243). Making the ring dynamic therefore changes dispatcher slice preference
too.

That is **safe by construction and in scope**: the dispatcher's use is a
*preference* with a work-conserving fallback (a host reaches outside its slice
when its own slice has no eligible work), not a hard filter. A ring that
resizes mid-tick can only shuffle which host *prefers* a repo; coverage is
guaranteed by the fallback, and same-issue races remain guarded by the
lease-order dedup (#6287). The implementation must not, however, let a
roster-mode *yield* (condition 1) reach the dispatcher as "owns nothing" — for
the dispatcher, yield must degrade to the work-conserving fallback, i.e. the
`ShardDecision` handed to `work_finder` keeps the pre-roster semantics. Phase B
must carry an explicit test for this.

## Config surface (proposed)

| Key | Env | Default | Meaning |
|---|---|---|---|
| `autonomous.roleRunner.roster.enabled` | `LOOM_ROLE_RUNNER_ROSTER` | `false` | Opt-in. Off ⇒ byte-identical to #6374. |
| `autonomous.roleRunner.roster.issue` | `LOOM_ROLE_RUNNER_ROSTER_ISSUE` | *(unset)* | `owner/repo#N` of the roster issue. Fleet-wide, so the tracked config is a fine home (same argument as `shardCount`). Unset with `enabled: true` ⇒ unsharded + one `error!`. |
| `autonomous.roleRunner.roster.heartbeatSecs` | `LOOM_ROLE_RUNNER_ROSTER_HEARTBEAT_SECS` | `300` | Refresh cadence for this host's record. |
| `autonomous.roleRunner.roster.ttlSecs` | `LOOM_ROLE_RUNNER_ROSTER_TTL_SECS` | `900` | Liveness TTL. Floored at 3× heartbeat. |
| `autonomous.roleRunner.roster.settleSecs` | `LOOM_ROLE_RUNNER_ROSTER_SETTLE_SECS` | `900` | Quiet period a new ring must survive before anyone acts under it. |

**Forge cost.** Per host per heartbeat: one REST `GET .../issues/N/comments`
(paginated, the #4637 NDJSON workaround) plus one REST `PATCH` of its own
comment. At the default 300s that is **24 REST calls/hour/host** — 96/hour for
a 4-host fleet, ~2% of the 5,000/hour REST budget and ~1% of an App
installation's 8,250/hour. **REST only, never GraphQL** — the GraphQL pool is
the one that exhausts under concurrent agents (#5047).

## Status rendering (AC4)

`loom-daemon status` already prints `Role runner (sharding): …` whenever
sharding is configured. Roster mode extends it, never replaces it:

```
Role runner (sharding): roster — shard 1 of 3 (index from roster, count from roster)
  Roster: issue owner/repo#1234 · 3 live / 4 seen · gen 2026-09-15T09:41:07Z (settled 22m)
    host-a3f9c1d2  fresh   (last beat 41s ago)   serves 27
    host-d9142cf3  fresh   (last beat 2m ago)    serves 27   ← this host
    host-7c1e0b55  fresh   (last beat 4m ago)    serves 12
    host-e1d4c843  EXPIRED (last beat 31m ago)   serves 27   — slice reassigned
```

`--json` carries the same under `role_runner_shard.roster`. The existing
`ValueSource` enum gains a `Roster` variant so `describe()` says where
`(index, count)` came from. An expired member stays *visible* after eviction —
silence about a dead host is exactly how the `LOOM_ROLE_RUNNER=0` mitigation
became invisible pre-#6374.

## Phasing: two implementation phases

| Phase | Scope | ACs |
|---|---|---|
| **A** | Roster publication, heartbeat, expiry, and `status`/`--json` rendering. **Observational only** — `decide()` behavior is byte-identical to today; the ring is still static. Mirrors Epic #6165 Phase 1, which shipped the lease record write-only before anything read it. | AC4 |
| **B** | Rank-from-roster + the fencing rule + bounded reassignment, behind `roster.enabled`. Static env pair kept as the escape hatch; `LOOM_ROLE_RUNNER=0` untouched. | AC1, AC2, AC3 |

Phase A is separable because its failure mode is bounded to "status prints
something wrong," and it lets the fleet publish a roster for a full cycle —
producing real `updated_at` data to size `ttl`/`settleSecs` against — before any
ring depends on it.

### Verification plan (both phases)

- **Split-view test.** Two simulated hosts with deliberately divergent roster
  reads; assert no key is claimed by two hosts at the same instant, and that
  every key has an owner outside the settle window.
- **Kill-host test.** Expire a member's record; assert a survivor picks up its
  slice within `ttl + settleSecs + interval` and not before.
- **Determinism test.** The eviction boundary computed from a fixed comment set
  is identical across hosts — the arithmetic property the fence rests on.
- **Escape-hatch regressions.** `role_runner_env_zero_still_disables_the_host_that_owns_the_slice`
  and `an_unsharded_host_still_ticks_every_workspace` pass unmodified;
  a new test pins that a set `LOOM_ROLE_RUNNER_SHARD_INDEX` beats an enabled
  roster.
- **Dispatcher non-regression.** A roster-mode yield must not starve
  `work_finder`'s preferred-slice fallback.
- **Production measurement.** `role_collision.rs` (#4623) is the field
  instrument: if the fence holds, enabling roster mode must not raise the
  cross-host role-tick collision counter above its static-ring baseline.

## Assumptions, stated plainly

- **Clock skew is small relative to `settleSecs`.** Eviction boundaries are
  absolute instants compared against each host's local clock; hosts skewed by
  more than `settleSecs` (15 minutes) could cross a boundary far enough apart to
  overlap. NTP-synced hosts are sub-second, so this holds with ~3 orders of
  magnitude of margin. A cheap hardening, available to Phase B: take `now` from
  the forge response's `Date` header on the roster read, removing local clock
  skew from the fence entirely.
- **`fnv1a64` digests in `serves` do not collide meaningfully.** A 64-bit
  digest over a few dozen keys; a collision would put one extra host in one
  key's ring — a duplicate, not a gap. Acceptable at this probability.
- **A host's registered-workspace set changes rarely.** It is republished on
  every heartbeat, so a change is picked up within `heartbeatSecs` and fenced
  like any other boundary.

## See also

- [`daemon-reference.md` → Role-runner host sharding (#6374)](daemon-reference.md#role-runner-host-sharding-6374) — the static ring this extends
- [`dispatcher-repo-sharding.md`](dispatcher-repo-sharding.md) (#6243) — the second consumer of the same ring, and the precedent for rejecting a mesh-derived rank
- [`lease-record.md`](lease-record.md) / [`lease-renewal.md`](lease-renewal.md) — the marker-comment + `updated_at` liveness idiom this reuses
- `loom-daemon/src/role_shard.rs`, `loom-daemon/src/role_runner.rs`, `loom-daemon/src/role_collision.rs`
