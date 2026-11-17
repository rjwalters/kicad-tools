# Issue-filing lock (#6714)

Every `gh issue create` in Loom goes through **one machine-wide lock**, so two
issue-creating agents can never interleave their filing bursts.

This document is the reference for the mechanism, its two tiers, its knobs, and
the one boundary it deliberately does not pretend to cross.

## Why a lock, and not a rule

[#3707](https://github.com/rjwalters/loom/issues/3707) identified that parallel
issue-creating agents race on `gh issue create` and cross-contaminate bodies. It
shipped a **documentation-only** mitigation: *"Do not run concurrent Architects
— serialize issue creation."*

Seventeen days later, on **2026-08-08T02:49:04Z–02:49:23Z**, it happened anyway.
Two Architects filing 5-issue bursts into **different** repos
(`2AMLogic/gf180-sram` and `2AMLogic/sky130-modexp`) overlapped. gf180-sram's
five bodies were overwritten with sky130-modexp's — one-directionally, off by
one (sram #7 got modexp #6's body, sram #8 got modexp #7's, …). **Titles were
unaffected**, so every issue looked plausible in its listing and nothing
surfaced the problem for **13 days**. Three of the five were eventually closed
`NOT_PLANNED`; two needed a human-authored body replacement; an operator triage
pass read four of them as ordinary parked work and released them into the ready
queue, where agents claimed them within minutes.

The convention could not hold, for three structural reasons — none of which are
operator error:

1. **The in-progress guard is per-workspace.** `role_collision.rs`'s
   `InProgressGuard` serializes `(root, role)` ticks. By construction it cannot
   serialize an Architect in one repo against an Architect in another.
2. **Concurrent cross-repo Architects are normal operation.** The daemon is the
   scheduler and no human is in the loop at dispatch time, so "do not run
   concurrent Architects" is not a thing an operator can comply with.
3. **`IssueCreationMutex` is in-process.** `issue_creation_mutex.rs` (#3707's
   own Phase-2 primitive) is a `tokio::sync::Mutex`. The agents that race are
   separate OS processes spawned by `spawn-claude.sh`, so nothing it guards is
   in the same address space as the `gh issue create` that actually races.

## Where the lock lives

The single acquire/release call site is
[`create-issue.sh`](../scripts/create-issue.sh) — the single-sourced entry point
every issue-creating role already goes through (Architect, Auditor,
Curator-decomposition, Builder-decomposition, Doctor, Hermit, Judge). Filing
through a bare `gh issue create` bypasses the lock **and** the GraphQL-exhaustion
REST fallback (#5047); don't.

Two halves implement the identical on-disk protocol, exactly like the
[build slot](build-gate.md)'s `build_slot.rs` / `lib/build-slot.sh` pairing:

| Half | Path | Used by |
|---|---|---|
| Bash | `defaults/scripts/lib/filing-lock.sh` | `create-issue.sh`, i.e. every agent |
| Rust | `loom-daemon/src/filing_lock.rs` | the daemon, incl. the peer-hold mirror |

## Two tiers, and an honest boundary

| Tier | Scope | Strength |
|---|---|---|
| **Host** | every workspace + every agent process on one machine | **hard** mutual exclusion (POSIX-atomic `mkdir`) |
| **Fleet** | other hosts | **soft**, TTL-bounded backoff |

The host tier is what actually closes the 2026-08-08 hazard: two processes can
only overwrite each other's *body text* through shared memory or a shared
filesystem, which means one machine. Note that "host-wide" is already strictly
stronger than what failed — the gap #3707's mitigation left was
*cross-workspace*, not cross-machine.

The fleet tier covers the weaker, genuinely cross-host hazard #3707 also named:
a burst binding the wrong freshly-minted issue **number** into a `Part of #N`
cross-reference. It rides the existing peer-claims transport (#4028):

1. A filer that takes the host lock publishes a `filing_lock` claim ad over the
   safehouse claims room, via `fleet-send.sh`. (Published by the filer itself,
   not by a daemon tick — a burst lasts seconds, and the reaper's 30s
   re-advertisement cadence would miss almost all of them.)
2. Every peer daemon's `safehouse::PeerClaimSink` folds it into
   `PeerClaimView`'s filing-hold map **and mirrors it to disk** at
   `<store>/peers/<host>`.
3. That mirror is what the *other host's* `filing-lock.sh` reads — the daemon is
   the bridge between the cross-host transport and the cross-process lock.
4. Release publishes `filing_unlock`, which clears both.

One consequence of keying the host tier on `$HOME`: agents running in separate
containers (the `loom-worker` image) each get their own store, so they serialize
within a container, not across containers on the same physical host. Mount a
shared `LOOM_FILING_LOCK_DIR` if you run several worker containers per host and
want them to serialize; otherwise they fall back to the fleet tier, exactly like
two separate machines.

**This tier is soft on purpose, and that is not a shortcut.** `peer_claims.rs`'s
own module documentation states the contract: *"A room broadcast is eventually
consistent, so this is a fast backoff, not a lock."* A true cross-host mutex
needs an atomic authority — the peer-claims Phase 2 CAS that has never been
built — not a broadcast. A host with safehouse disabled simply degrades to
host-only serialization, with no error anywhere.

## Safety properties

All four are load-bearing; each is covered by a test in
`defaults/scripts/tests/test-filing-lock.sh` and `filing_lock.rs`'s test module.

1. **A crashed holder cannot wedge fleet-wide issue creation.** Three
   independent reap legs:
   - the holder directory aging past `LOOM_FILING_LOCK_STALE_SECS`;
   - an owner recorded on *this* host whose PID is not running — reaped
     immediately (the same `live_claim` discipline the `loom:building` lease
     uses). A PID number from *another* machine says nothing about a process
     here, so it is never used for liveness;
   - a peer hold aging past its own (shorter) TTL, so a peer that dies mid-burst
     and never sends `filing_unlock` frees within a minute.
2. **Bounded and fail-SAFE.** The wait is bounded; on expiry the filer
   **defers** — exit code `75` (`EX_TEMPFAIL`), nothing filed, a log line saying
   so, retry on the next tick. This is the one place the filing lock
   deliberately does *not* copy `build-slot.sh`, which degrades open: an
   unserialized build wastes CPU, an unserialized filing burst corrupts issue
   bodies.
3. **Degrades open only when there is no lock to take.** An unusable store (no
   `$HOME`, a file in the way, no write permission) proceeds unserialized —
   because a store nobody can write is a store nobody is serialized by, and
   refusing would convert a corruption risk into a total filing outage.
4. **Re-entrant.** `LOOM_FILING_LOCK_HELD` is exported to children, so a role
   that wants to hold the lock across an *entire* burst can source
   `lib/filing-lock.sh` and acquire once — the per-call acquire inside
   `create-issue.sh` then becomes a no-op inside that hold.

## On-disk protocol

```text
<store>/                  # ~/.loom/locks/issue-filing by default
  holder/                 # the lock itself — mkdir-atomic
    owner.json            # {"host","pid","label","acquired_at"}
  peers/
    <host>                # a peer's advertised hold; mtime = LOCAL receipt time
```

`flock` is deliberately avoided (unavailable on stock macOS), matching the
token-pool lock, the per-issue claim lock and the build slot. Peer-hold TTLs are
measured against **local receipt**, never the advertiser's wall clock — clock
skew makes cross-host timestamps incomparable, the same rule `peer_claims.rs`
applies to its own TTLs.

## Knobs

Env-only, and identical on both halves (a knob with an independent Bash-side
reader stays env-only rather than honoring `.loom/config.json` on one path and
silently ignoring it on the other — the rule `LOOM_PER_WORKTREE_GB` followed
while the daemon still read it, before #12406).

| Variable | Default | Meaning |
|---|---|---|
| `LOOM_FILING_LOCK` | `1` | `0`/`false`/`off`/`no` disables serialization entirely |
| `LOOM_FILING_LOCK_WAIT_SECS` | `120` | bounded wait before deferring |
| `LOOM_FILING_LOCK_STALE_SECS` | `300` | age at which a holder is reaped |
| `LOOM_FILING_LOCK_PEER_TTL_SECS` | `60` | age at which a mirrored peer hold expires |
| `LOOM_FILING_LOCK_DIR` | `~/.loom/locks/issue-filing` | store location |
| `LOOM_FILING_LOCK_LABEL` | `create-issue` | label recorded in `owner.json` |
| `LOOM_FILING_LOCK_HELD` | unset | re-entrancy sentinel (set by the holder) |

The daemon-side peer-hold TTL additionally honors
`LOOM_PEER_FILING_LOCK_TTL_SECS` / `safehouse.peerFilingLockTtlSecs` for the
in-memory `PeerClaimView` copy; keep it in lock-step with
`LOOM_FILING_LOCK_PEER_TTL_SECS`.

## What a role should do on exit code 75

Stop filing. Say in your output that the burst was **deferred** and why. Do not
retry in a tight loop, and do not work around the lock by calling `gh issue
create` directly — filing unserialized is the exact behavior that corrupted five
issues.

## Known gap

The corruption survived 13 days because **nothing looked**. A detection backstop
— flagging an issue body whose content does not reference the repo it lives in
(the Curator's own `git grep` test, mechanized at filing time) — is *not* part of
this mechanism and is tracked in
[#6771](https://github.com/rjwalters/loom/issues/6771). The lock prevents the
corruption; the backstop would catch it if the lock ever has a gap — which is
exactly how #3707's mitigation failed.
