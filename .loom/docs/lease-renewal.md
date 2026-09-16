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

- **`start <issue> [--interval SECS] [--watch-pid PID] [--watch-ident TOKEN]
  [--max-age SECS] [--host H] [--sweep-id S]`**
  — resolve a liveness PID (the same ancestor-walk `sweep-run-registry.sh`
  uses to find the long-lived `claude -p /loom:sweep ...` orchestrator
  process, never the one-shot Bash-subshell PID of the tool call that
  invokes `start`), pin that PID's start-time **identity**, then spawn ONE
  detached background loop that, every `--interval` seconds (default 300 =
  5 minutes, overridable via `SWEEP_LEASE_RENEW_INTERVAL_SECS` too — the
  epic's suggested cadence, pending #6181's real-world measurement),
  best-effort renews the lease for `<issue>` for as long as that PID is
  still running **the same process** and the loop is under its absolute age
  cap. Prints the loop's PID. See
  [The loop's four exits](#the-loops-four-exits-7825) below.
- **`renew-once <issue> [--host H] [--sweep-id S]`** — one synchronous
  renewal cycle: locate the newest comment on `<issue>` whose body starts
  with the lease marker (or, if `--host`/`--sweep-id` are both given, the
  comment whose marker line matches them exactly), and idempotently PATCH
  it. Exit 0 on success, 2 when no matching lease comment exists (a normal,
  silent no-op — not every sweep is daemon-dispatched), 1 on a `gh` failure.
- **`stop <PID>`** — best-effort kill of a loop PID. Not required for
  correctness; the loop already self-terminates.

### The loop's four exits (#7825)

**Nothing outside the script reaps this loop.** The daemon's orphan-process
reaper attributes processes to work exclusively by worktree, and a renewal
loop's cwd is the workspace root while its argv names no worktree — so it is
invisible to that pass. The crash-path process-group reap cannot reach it
either: the dispatch deliberately puts it in its own process group, and it
fires only once, at the moment the registry entry goes terminal. An
in-session sweep has no registry entry at all. Self-termination is the *only*
reaping mechanism there is, which is why it has four independent legs rather
than one:

| Exit | Trigger | Added |
|---|---|---|
| Watched PID died | `pid_is_live` reports dead at the next wake-up | #6180 |
| Watched PID **is not the same process** | its start-time identity no longer matches the token recorded at `start` | #7825 |
| **Absolute age cap** | the loop has been running longer than `--max-age` / `SWEEP_LEASE_RENEW_MAX_AGE_SECS` (default `86400` = 24 h; `0` = unbounded) | #7825 |
| Own-yield guard | a `renew-once` cycle exits 4 — this dispatcher's own lease target already posted a `loom:lease-yield` record | #6485 |

In every case the lease comment is **left in place** to age out. A
terminating loop never deletes it — the epic requires positive evidence on
the reclaim side, and the renew side simply stops broadcasting.

#### Identity, not just a PID number

A bare PID is not a durable handle on a process. The kernel recycles PID
numbers, and on a busy worker polled every five minutes over a multi-day
horizon, wraparound is a certainty rather than an edge case. Once an
unrelated process inherited the watched PID number, the pre-#7825 liveness
test flipped back to "alive" **permanently** — the loop could never observe
its sweep's death again. Six such loops (the oldest 18 days old, most with no
worktree left behind them) were found on a single worker, each keeping a
long-dead sweep's `loom:building` lease fresh so that no peer host would ever
reclaim the claim.

The watch is therefore the **pair** `(pid, start-time identity)`:
`/proc/<pid>/stat` field 22 on Linux, `ps -o lstart=` elsewhere. Both are
re-probed on every cycle, and a mismatch — or an identity that can no longer
be read at all — reads as dead. `--watch-ident` lets a caller that already
captured the token at spawn time pin it explicitly and close the microsecond
race between capturing a child's PID and probing it.

Two related defects were fixed in the same pass, in the shared liveness code:

- A **zombie now reads as dead.** `kill -0` *succeeds* for a `Z`-state
  process, so the old `kill -0` fast path returned "alive" and the
  documented `state == Z` branch below it was unreachable. `ps` is now
  consulted first; `kill -0` survives only as the fallback for a host with
  no usable `ps`, never as the whole decision.
- **The ancestor walk refuses to hand back a session supervisor.**
  `resolve_liveness_pid` previously returned whatever it landed on with no
  validation, so when a sweep's intermediate ancestors had already been
  reaped it could return `systemd --user`, `launchd`, a `tmux` server, or
  `sshd` — an immortal watch target by construction. It now neither ascends
  into a supervisor nor returns one; when no valid work handle exists it
  returns its own short-lived PID, so the loop stops within a tick and the
  lease ages out.

#### Why the age cap exists anyway

The identity check should make an immortal loop impossible. The cap is there
because "should" is what the pre-#7825 code also claimed. It is deliberately
independent of every other exit: even if the liveness test is defeated again
by something nobody has anticipated, the blast radius is one day instead of
eighteen. A sweep that legitimately outruns 24 hours loses lease *renewal*,
not its work — the claim simply becomes reclaimable, which is the right
outcome for a sweep nobody can distinguish from a dead one. Raise
`SWEEP_LEASE_RENEW_MAX_AGE_SECS` (or set it to `0`) on a host that genuinely
runs multi-day sweeps.

**These functions are duplicated, byte-identically, in
`defaults/scripts/sweep-lease-renew.sh` and
`defaults/scripts/sweep-run-registry.sh`**, between
`# --- BEGIN shared pid-liveness block` / `# --- END …` delimiters.
`sweep-run-registry.sh` calls `main "$@"` unguarded at the bottom of the
file, so sourcing it would also run its CLI dispatch; and ADR-0018 /
`scripts/shell-allowlist.txt` admits no category for a *new* shared shell
library (`contract` is baseline-only). Test case `(q)` in
`defaults/scripts/tests/test-sweep-lease-renew.sh` diffs the two copies and
fails the suite on any drift — that machine check is what stands in for
`source`.

#### Operator note: killing an orphan loop by hand

An orphan renewal loop from before this fix is safe to `kill`. It holds no
lock and writes nothing but the lease comment's `updated_at`; the lease it
stops renewing simply ages out. Enumerate candidates with:

```bash
pgrep -af 'sweep-lease-renew\.sh start' | while read -r pid rest; do
  printf '%s\t%s\t%s\n' "$pid" "$(ps -o etime= -p "$pid" | tr -d ' ')" "$rest"
done
```

Anything older than the longest sweep you believe is actually running is an
orphan.

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
# `read` splits the "<host> <sweep-id>" line identically in bash and zsh.
read -r LEASE_HOST LEASE_SWEEP <<<"$LEASE_IDENT"
./.loom/scripts/sweep-lease-renew.sh start "$N" --watch-pid "$PPID" \
  --host "$LEASE_HOST" --sweep-id "$LEASE_SWEEP" > /dev/null 2>&1 || true
```

Three details of that invocation are load-bearing, and all three were learned
the hard way (#7876):

- **Never split `LEASE_IDENT` with `set -- $LEASE_IDENT`.** That relies on
  bash's word-splitting of an unquoted parameter, and the orchestrator's Bash
  tool runs the operator's **login shell** — zsh on macOS, where
  `SH_WORD_SPLIT` is off and an unquoted parameter is *not* split. Under zsh
  `$1` becomes the whole `"<host> <sweep-id>"` line and `$2` is empty, so the
  loop forks with a bogus `--host` and an empty `--sweep-id`, its exact-match
  targeting (#6485) never finds its own lease comment, and the lease ages out
  at the 15-minute TTL while the operator believes renewal is running. Two
  sweeps lost a build to the pre-push fence (#6309) this way before it was
  found. `read -r … <<<"$…"` behaves identically in both shells.
  `sweep-lease-renew.sh start` now also **refuses** a whitespace-bearing or
  half-empty `--host`/`--sweep-id` pair rather than forking a doomed loop.
- **Pass `--watch-pid "$PPID"` explicitly**, for the same reason Step 0a's
  `sweep-run-registry.sh new --pid "$PPID"` does (#4691): the tool-call shell
  is reaped the instant the call returns, so leaving `start` to resolve a
  liveness PID by walking its own ancestry from inside a tool call is fragile.
- **Keep `> /dev/null 2>&1`.** The loop is disowned but inherits whatever
  stdout it was given; attach a pipe (e.g. `| tail`) and the calling tool call
  blocks until the loop exits — i.e. for the lifetime of the sweep.

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
