# Sweep-Owned Lease Renewal (Epic #6165, Phase 1: #6180; dispatch-time start: #7672)

Epic #6165 gives the `loom:building` claim a liveness dimension — a "lease".
Issue #6179 (a sibling Phase 1 issue) defines the write-only lease record
format and writes it once, at the moment a dispatch acquires
`loom:building`. **This document covers the other half: keeping that record
fresh for the lifetime of the sweep that holds the claim.**

## The lease record this renews

At the time this script was written, #6179 had not yet merged. The format
below is reproduced from #6179's own issue body (the epic's suggested
shape) so this renewal mechanism has a single, precise, testable contract
regardless of merge order — coordinate any format change with #6179's own
doc (`defaults/docs/lease-record.md`, once it lands).

A lease record is an issue comment whose body's literal first line is:

```
<!-- loom:lease host=<host> sweep=<sweep-id> -->
```

Everything after the marker's closing `-->` is free-form prose. Machine
readers — this renewal script included — must never depend on that prose,
only on locating the comment via
`startswith("<!-- loom:lease host=")`. **The liveness signal a reader must
consult is the comment's own forge-assigned `updated_at` timestamp, never
any value embedded in the marker text.**

## Why the sweep renews, not the daemon

This is the load-bearing, non-obvious constraint from the epic body: role
agents run as transient scopes parented to `systemd --user` and routinely
outlive the daemon process that spawned them (loom#6129). Supervisor
liveness (is `loom-daemon` up?) is therefore not the same thing as work
liveness (is the sweep still actively working this issue?). If the daemon
owned renewal, a daemon restart would let a live sweep's lease expire, and a
peer host would then have positive-looking "evidence" to reclaim work that
was never actually abandoned — reproducing the exact bug this epic exists to
fix, from a different direction.

Renewal must therefore be driven by the process actually doing the work:
sweep alive → lease renewed and fresh; sweep dead → renewal stops → lease
expires on its own → a reclaim by another host (Phase 2) is then justified
by positive evidence, not inference from a missing broadcast.

### …but the daemon *starts* the loop on its own dispatch path (#7672)

"The sweep renews" is a statement about **who the loop watches**, not about
which process typed the `start` command. Those were the same thing until
#7672, because `sweep.md`'s Step 1a asked the spawned session to run `start`
itself — and a step that only happens when a model remembers a sentence is
not a mechanism. One session skipping it produced ~25 claim/yield cycles
over 2.5 h across four hosts and a near-miss where a second Builder's claim
went live while the first was still working in the shared
`.loom/worktrees/issue-N` directory (the downstream incident cited in #7672):
the lease aged out, a peer's reclamation gate (#6286) correctly judged the
claim dead, and it reclaimed live work.

So for a daemon-dispatched `--claim-owned` sweep, `loom-daemon` now issues
the one-shot `start` itself, once per dispatch, from
`SweepRegistry::finish_issue_dispatch` — see "Where it is wired in" below.
The #6129 constraint above is untouched, because the *only* thing that moved
is the invocation:

- `start` forks one loop, `disown`s it, and returns. The loop is not a child
  the daemon supervises; nothing in the registry tracks, ticks or waits on
  it, and it is placed in its own process group so a process-group-targeted
  teardown of the daemon cannot reach it either.
- The daemon passes `--watch-pid <the sweep child's own pid>`, so the loop's
  lifetime is pinned to the **sweep**, exactly as when the session started
  it. It stops when the sweep stops — never when the daemon stops or
  restarts.

A daemon restart one second after dispatch therefore leaves the loop running
untouched. What the daemon must *never* acquire is ongoing responsibility —
no tick-loop renewal, no "re-arm the renewal for every live sweep on
startup" — because that is precisely what a restart would drop.

## Mechanism

`defaults/scripts/sweep-lease-renew.sh` (mirrored, via a symlink, into
`.loom/scripts/`) provides:

- **`start <issue> [--interval SECS] [--watch-pid PID] [--host H] [--sweep-id S]`**
  — resolve a liveness PID (the same ancestor-walk `sweep-run-registry.sh`
  uses to find the long-lived `claude -p /loom:sweep ...` orchestrator
  process, never the one-shot Bash-subshell PID of the tool call that
  invokes `start`), then spawn ONE detached background loop that, every
  `--interval` seconds (default 300 = 5 minutes, overridable via
  `SWEEP_LEASE_RENEW_INTERVAL_SECS` too — the epic's suggested cadence,
  pending #6181's real-world measurement), best-effort renews the lease for
  `<issue>` as long as the watched PID stays alive. Prints the loop's PID.
- **`renew-once <issue> [--host H] [--sweep-id S]`** — one synchronous
  renewal cycle: locate the newest comment on `<issue>` whose body starts
  with the lease marker (or, if `--host`/`--sweep-id` are both given, the
  comment whose marker line matches them exactly), and idempotently PATCH
  it. Exit 0 on success, 2 when no matching lease comment exists (a normal,
  silent no-op — not every sweep is daemon-dispatched), 1 on a `gh` failure.
- **`stop <PID>`** — best-effort kill of a loop PID. Not required for
  correctness; the loop already self-terminates.

### Renewal = idempotent PATCH, never a new comment

GitHub does not reliably advance a comment's `updated_at` on a byte-for-byte
identical PATCH, so `renew-once` rewrites a single trailing HTML-comment
line — its own sub-marker, `<!-- loom:lease-renewed at=... by=... -->` — with
a fresh timestamp on every call. This guarantees the body actually changes
(so `updated_at` genuinely advances) while leaving the first-line lease
marker byte-identical, so a `startswith()` reader never sees it move. Like
the primary marker, `loom:lease-renewed`'s `at=` value is for human
debugging only — no reader may treat it as authoritative; the forge's own
`updated_at` always is. A second (or Nth) renewal *replaces* this trailing
line rather than appending another copy, so a long-running sweep's lease
comment never grows unbounded and no duplicate comments ever accumulate.

### The PATCH must use `gh api -F`, never `-f` (#6320)

Real `gh api` applies the `@<path>` / `@-` read-from-file/stdin magic **only**
to `-F/--field`. `-f/--raw-field` sends the value as a literal string, so
`-f body=@-` PATCHes the comment body to the two characters `@-` — erasing
the first-line marker on the very first renewal and making a live claim look
lease-less to every reader (the daemon's reclamation gate #6286, the
dispatch-time ordering check #6287, the sweep-side fence #6309, and this
script's own next pass, which can no longer find the comment it just
destroyed). This shipped in the original #6180 implementation and was
observed live on real issues before #6360 fixed it. The regression is pinned
by `defaults/scripts/tests/test-sweep-lease-renew.sh`, whose `gh` stub now
reproduces gh's own per-flag semantics instead of reading stdin regardless
of the flag.

### Always pass `--host` / `--sweep-id`

Both callers below pass them, and both must: without the pair, `renew-once`
falls back to "newest lease wins" and can spend the whole sweep PATCHing a
*peer* dispatcher's more-recently-posted lease comment while this claim's own
`updated_at` never advances (#6470/#6485 — a live, correctly-working renewal
loop keeping the wrong claim alive). The daemon knows both values exactly (it
published them itself in `write_lease_comment`); `sweep-lease-publish.sh
publish` prints the resolved `<host> <sweep-id>` on stdout precisely so the
in-session caller can thread them into `start` too.

`start` also auto-resolves the pair from `$LOOM_TERMINAL_ID` /
`resolve_published_host` when a caller passes neither (#6485), which is what
keeps an older installed `sweep.md` correct. Explicit flags always win.

## Where it is wired in

**Daemon-dispatched `--claim-owned` sweeps — `loom-daemon` starts the loop
(#7672).** `SweepRegistry::finish_issue_dispatch`
(`loom-daemon/src/sweep_registry/dispatch.rs`) calls
`start_lease_renewal_loop`, which runs the equivalent of:

```bash
.loom/scripts/sweep-lease-renew.sh start "$N" \
  --watch-pid "$CHILD_PID" --host "$PUBLISHED_HOST" --sweep-id "$SWEEP_ID"
```

- **Once per dispatch**, inside the same `dispatch()` call that spawned the
  child — not from a tick, a timer, or any later daemon-driven step a restart
  could lose.
- **Off the registry lock.** `finish_issue_dispatch` runs holding the global
  `Arc<Mutex<SweepRegistry>>` (on a tokio worker thread, for the IPC and
  work-finder dispatch paths), so `start_lease_renewal_loop` reads what it
  needs out of the registry and hands the `start` handshake — the subprocess
  spawn plus its 10 s `LEASE_RENEW_START_TIMEOUT` wait — to a detached
  thread. The handshake is sub-millisecond in the normal case, but a
  pathological helper (a wedged filesystem, a `bash` that never execs) must
  not be able to pin that mutex and starve `list_sweeps` / `cancel` /
  concurrent dispatches behind it. Same reasoning, and same shape, as the
  #6592/#7307 split that moved the account-selection poll out from under the
  lock.
- **After** the #4689 immediate-preflight-death check, deliberately: that
  branch unwinds the whole claim (label, claim lock, peer-claim ad) for a
  child that is already dead, and a loop must never be left watching a pid
  that has already exited. The cost is a bounded gap — the
  account-selection poll, ≤ `TOKEN_NAME_CAPTURE_TIMEOUT` — during which the
  lease comment is seconds old and nowhere near any reclamation TTL.
- **Best-effort**, exactly like #6179's write-on-dispatch contract: a
  missing `.loom/scripts/sweep-lease-renew.sh`, a non-zero `start`, or a
  spawn error only logs. Dispatch proceeds; the lease then ages out just as
  it did before this hand-off existed, with `loom:building` still the
  authoritative claim.
- The helper's **stderr goes to the sweep's own log file**, not `/dev/null`,
  so the mid-sweep renewal failures #6541 made visible still land where an
  operator already looks. It must be a file, never a pipe — the detached
  loop holds its inherited copy (fd 9) open for the sweep's whole lifetime.

### The prompt's fallback, and why the withdrawal is conditional

`sweep.md`'s Step 1a no longer runs `start` unconditionally — but it does not
simply *stop* either. The dispatch also exports a capability marker into the
child:

```
LOOM_SWEEP_LEASE_RENEW_DISPATCHED=<N>
```

set for every `Issue` dispatch (never for a `PrSet`, which claims no issue and
holds no lease), and Step 1a runs `start` itself exactly when that marker does
not name the issue it is pre-flighting.

The marker exists because **the installed prompt and the daemon binary do not
roll together**. `.claude/commands/loom/sweep.md` is refreshed by an ordinary
`git pull` / `resync-installed.sh` pass; the `loom-daemon` binary is only
rebuilt by `loom update`. "New prompt, pre-#7672 daemon" is therefore a real,
reachable state — and under an unconditional withdrawal every sweep dispatched
during that skew would have no renewal loop from *either* side, which is
precisely the stale-lease reclamation this change exists to prevent, applied
fleet-wide. Gating on the marker makes all three combinations safe:

| Prompt | Daemon | Outcome |
|---|---|---|
| new | ≥ #7672 (marker set) | session skips — the daemon already started it |
| new | pre-#7672 (no marker) | session starts it itself: pre-#7672 behavior, unchanged |
| old | ≥ #7672 | session also starts one — a duplicate loop, harmless (an idempotent PATCH of the same comment, one extra call per interval) |

The marker is a **capability** signal, not a success receipt: it is set at
spawn time, before `start` has run, and `start` is best-effort even when it
does. So a marker-present dispatch whose `start` failed leaves no loop — the
daemon logs it, and the lease ages out exactly as it did before this hand-off
existed. Step 1a deliberately does not try to compensate for that: forking a
duplicate loop on every healthy dispatch to cover a rare, already-logged case
is a bad trade.

**In-session paths — the sweep still starts its own loop.** For any sweep
with no daemon-dispatched claim on this run (manual invocation, GH Actions
cron, `--no-daemon`) there is no dispatch code to do it mechanically, so
Step 1a's self-claim signal is never true and those candidates instead
publish their own record and start renewal at **Step 1b** (#6320,
`sweep-lease-publish.sh`), pinned to that record's `--host`/`--sweep-id`:

```bash
LEASE_IDENT="$(./.loom/scripts/sweep-lease-publish.sh publish "$N" --sweep-id "$RUN_ID")"
# shellcheck disable=SC2086
set -- $LEASE_IDENT
./.loom/scripts/sweep-lease-renew.sh start "$N" --host "$1" --sweep-id "$2" > /dev/null 2>&1 || true
```

## What this does not do (Phase 1 scope)

This document was written for Phase 1, when nothing read the lease. Phases 2
and 3 have since landed, so renewals are now load-bearing: the daemon's
reclamation gate (#6286) and dispatch-time ordering check (#6287) and the
sweep-side pre-push fence (#6309) all consume the freshness this loop
maintains. What is still out of scope here is the acquisition race #4028
documented — Phase 3 bounds its cost, renewal does not touch it.

See also: [`lease-record.md`](lease-record.md) — #6179's own doc, the
authoritative definition of the marker format and the dispatch-time write
this renewal loop keeps fresh. Also
[`lease-renewal-measurement.md`](lease-renewal-measurement.md) — the
write-volume measurement methodology and a projected (not yet measured)
estimate against this loop's `~5 min` default cadence and the forge's rate
limits (#6181).
