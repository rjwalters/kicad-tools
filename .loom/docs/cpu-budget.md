# Per-Sweep CPU Budget (issues #5111, #5979)

Nothing used to bound how much CPU an agent's background work could consume. On
`loom-worker-1` (8 cores) an agent-written driver (`sim/.work/cal/run_all.sh`,
gitignored scratch — never a committed harness) ran 8 concurrent `ngspice`
processes at ~95% CPU each: 100% of the host, zero headroom, each sim bounded
only by its own `timeout 21600s` with no overall bound on the driver. The
host's own legitimate sweep was starved to 0.6% CPU for 5h33m while holding a
forge claim it could not advance.

`spawn-claude.sh` now computes and enforces a per-sweep CPU budget at the
worker spawn path — the natural extension point, since it already re-execs
the whole `claude`/`claude-wrapper.sh` process tree for the `#4233` niceness
mechanism.

## What every sweep gets, unconditionally

`LOOM_SWEEP_CPU_BUDGET_CORES` is exported into every spawned session:

```
max(1, floor( max(1, total_logical_cores - reserved_cores) / in_flight_sweeps ))
```

`total_logical_cores - reserved_cores` is the host's usable share (default
`reserved_cores = 2`, mirroring the daemon's own `min(16, cpu_cores - 2)`
agent-concurrency rule). `in_flight_sweeps` is how many sweeps are running on
**this host** at the moment of the spawn — see "Host-wide, not per-sweep"
below. A solo sweep therefore still gets the full `total - reserved`.

**If you are writing a driver that fans out CPU-bound work — a SPICE corner
sweep, a parallel build, anything that spawns more than a couple of heavy
child processes — read this env var and cap your own concurrency to it.**
This is the "published parallelism budget" direction: an explicit, documented
number instead of an implicit assumption about how many cores are "probably"
free. It is also the answer to "how many cores may I use right now" — a
harness that reads it needs no other coordination primitive, and gets
host-wide sharing for free.

## Host-wide, not per-sweep (issue #5979)

The original #5111 budget was a pure function of the host's core count, which
made it correct for one sweep and wrong for several. On `loom-worker-1`
(8 cores, reserved 2) three sweeps ran concurrently in three worktrees, and
each one independently and correctly computed "6 cores are mine" — 18 cores
of claimed budget on an 8-core box. Each launched a harness at `-j 8`, giving
19 live `ngspice` processes, load average **133.87**, 0% idle, and the host
halting its own dispatch for all 21 repos it manages.

No per-process rule can fix that, because every caller's view of "the machine"
is the whole machine. The budget now divides by the number of sweeps actually
in flight on the host, so the shares sum to the host instead of to N times the
host:

| Concurrent sweeps | 8-core host, `reserved = 2` | Sum |
|---|---|---|
| 1 | 6 cores | 6 |
| 2 | 3 cores each | 6 |
| 3 | 2 cores each | 6 |
| 12 | 1 core each (the floor) | 12 |

Division floors deliberately — three sweeps on six usable cores get 2 each,
never 2.67 rounded up — so the shares can only ever under-subscribe the host.
The one exception is the 1-core floor: a budget below 1 core would deadlock a
sweep, so more sweeps than usable cores still get 1 each. That is a bounded
overshoot (N cores, not N x host) and it is the admission side's job to not
get there — see [`admission_brake`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/admission_brake.rs).

### Where the concurrent-sweep count comes from

`lib/cpu-budget.sh`'s `loom_cpu_inflight_sweeps` resolves it, in order:

1. `LOOM_SWEEP_INFLIGHT_SWEEPS` — explicit override (a test hook, and the
   escape hatch for a host running concurrent agents the local daemon does
   not track).
2. `loom-daemon status --json` → `in_flight` + `unregistered_locked`
   (`unregistered_locked` entries are #4214's "demonstrably alive but the
   registry lost track of it" sweeps — they burn CPU like any other). This is
   the daemon's union across **every** managed root, so it is a host-wide
   count and not a per-repo one. The probe is hard-bounded by
   `LOOM_SWEEP_INFLIGHT_PROBE_TIMEOUT_SECS` (default 10s).
3. `1` — the fail-safe. No `jq`, no daemon binary, no running daemon, an
   unparseable payload, or a probe timeout all land here, and a divisor of 1
   reproduces pre-#5979 behavior exactly. **A host without a daemon behaves
   byte-for-byte as it did before this feature existed.**

The caller's own sweep is always counted. The daemon inserts a sweep's
registry entry *after* `Command::spawn()` returns, so a fast-starting child
can read a snapshot that does not list itself yet; the count is therefore
computed as `others + 1`, subtracting the caller's own issue
(`LOOM_SWEEP_CLAIM_OWNED`) out of the snapshot first if it is present. Both
orderings produce the same answer. A sweep with no claimed issue (a `--prs`
Mode C sweep, or a hand-run `spawn-claude.sh`) simply never matches, so it is
counted as the "+1".

### Design decision: spawn-time snapshot, not live tracking

**The divisor is sampled once, at spawn, and never revised for the life of
the sweep.** A sweep keeps a share sized for the sibling count at its own
start: if its siblings finish early, it leaves the freed headroom unclaimed;
if new siblings start later, only those newer sweeps divide by the larger
count.

The alternative — continuously re-deriving every sweep's share as the host's
population changes — was considered and rejected for this version. It would
require re-applying an already-installed systemd `CPUQuota` to a running
scope and re-publishing `LOOM_SWEEP_CPU_BUDGET_CORES` into an
already-started agent process, neither of which any mechanism in Loom does
today; that is new machinery, not a divisor. The snapshot also errs in the
safe direction by construction: stale-high divisors leave a host
**under**-subscribed, and the failure this issue exists to prevent is
over-subscription. Live re-balancing is a natural follow-up, not a
prerequisite.

## Where the budget is actually enforced (not just documented)

On a host with a reachable `systemd --user` manager (checked via
`lib/systemd-user.sh`'s `is_linux_systemd`), the final exec is wrapped in:

```
systemd-run --user --scope --quiet -p CPUQuota=<budget*100>% -- claude ...
```

This is a real kernel cgroup quota on the whole scope — every process the
sweep spawns, however many, collectively cannot exceed the budget. An agent
that ignores `LOOM_SWEEP_CPU_BUDGET_CORES` and forks 8 CPU-bound children
anyway is still contained: the cgroup throttles the group, not any one
process. Killing the scope (the orphan-reaping fix in sibling issue #5110)
reaps every process inside it in one shot, since they all live in the same
cgroup.

On a host with no systemd --user manager — every macOS worker in this fleet
today — there is no cgroup-equivalent primitive available without extra
tooling, so this degrades to **advisory-only**: the budget is still exported,
but nothing kernel-side enforces it. Tracked as a known gap / natural
follow-up, not attempted in the same change (see #5111 for the design
discussion).

## Optional: a wall-clock ceiling on the whole batch, not just each leaf

`LOOM_SWEEP_WALLCLOCK_CEILING_SECS` (env) / `autonomous.spawnWallClockCeilingSecs`
(`.loom/config.json`) adds a `-p RuntimeMaxSec=<secs>` property to the same
systemd scope — a hard bound on the ENTIRE spawned session's wall-clock time,
not just each leaf process's own `timeout`. **Default: `0` (disabled)** —
this mirrors `spawn-claude.sh`'s own `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`
precedent: a healthy sweep can legitimately run for hours, and this ceiling
has no notion of "still making progress", so it is a blunt backstop for a
genuinely runaway/orphaned batch, meant to be opted into per-repo (e.g. a
sim-heavy repo bounding its whole driver at, say, the same 6h a single leaf
process might already use as its own per-process timeout) rather than a
default that could kill a legitimate long build.

## Config reference

| Env var | Config key | Default | Effect |
|---|---|---|---|
| `LOOM_SWEEP_CPU_QUOTA` | — | `1` (enabled) | `0` disables the entire mechanism (no budget export, no quota wrap). |
| `LOOM_SWEEP_RESERVED_CORES` | `autonomous.spawnReservedCores` | `2` | Cores subtracted from the host total before computing the budget. |
| `LOOM_SWEEP_SHARED_CPU_BUDGET` | `autonomous.spawnSharedCpuBudget` | `1` (enabled) | `0`/`false` skips the #5979 host-wide division, so each sweep independently claims the whole `total - reserved` (pre-#5979 behavior). |
| `LOOM_SWEEP_INFLIGHT_SWEEPS` | — | *(auto)* | Override the concurrent-sweep divisor instead of asking the local daemon for it. |
| `LOOM_SWEEP_INFLIGHT_PROBE_TIMEOUT_SECS` | — | `10` | Hard bound on the `loom-daemon status --json` probe. On timeout the divisor falls back to `1`. |
| `LOOM_SWEEP_WALLCLOCK_CEILING_SECS` | `autonomous.spawnWallClockCeilingSecs` | `0` (disabled) | Adds `RuntimeMaxSec=<secs>` to the systemd scope when non-zero. |
| `LOOM_SWEEP_MEMORY_MAX` | — | *(auto)* | Per-scope `MemoryMax` (#11094). Unset = derive from the daemon's observed per-repo peak (`~/.loom/ram-peaks.json`): `max(LOOM_SWEEP_MEMORY_MAX_MIN_MB, high-water * LOOM_SWEEP_MEMORY_MAX_PCT/100)`, and **no limit at all without history** for that repo. `0`/`off` disables; a positive integer is an explicit MiB limit. Paired with `OOMPolicy=continue`, an over-budget build is killed inside its own cgroup instead of by the kernel's global OOM killer. `MemoryMax` rather than `MemoryHigh`: `MemoryHigh` only throttles, so a runaway build on a no-swap host would stall rather than fail. Skipped when the limit would be >= host RAM or systemd rejects it (memory controller not delegated). Computed by `loom-daemon ram-scope-limit`; a binary without that subcommand means no limit. |
| `LOOM_SWEEP_MEMORY_MAX_PCT` / `_MIN_MB` | — | `200` / `4096` | Multiple of the observed peak, and floor in MiB, for the derived limit. |
| `LOOM_SWEEP_CPU_BUDGET_CORES` | — | *(output only)* | Exported into the child with this sweep's computed share; read it, don't set it. |

Precedence for the config-backed tunables: env > config > default, the same
tier order used throughout Loom (see `spawn-claude.sh`'s own niceness knobs,
#4233).

## What this does NOT cover

- **Live re-balancing as sweeps start and finish** — the divisor is a
  spawn-time snapshot (see the design-decision section above). A sweep whose
  siblings all finish keeps its smaller share until it exits.
- **Compute consumed by anything that is not a sweep** — a human's own
  `cargo build`, an unrelated service on the box. The divisor counts sweeps,
  not load; the load-aware side is
  [`admission_brake`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/admission_brake.rs)
  / [`host_breaker`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/host_breaker.rs),
  which gate whether a *new* sweep starts (they never resize a running one —
  that gap is what #5979 closed).
- **Hosts whose concurrent agents the local daemon does not track** — the
  count comes from `loom-daemon status --json`. Agents launched outside the
  daemon (a hand-run `claude` that never went through `spawn-claude.sh`) are
  invisible to it; set `LOOM_SWEEP_INFLIGHT_SWEEPS` explicitly there.
- **Orphaned process trees that outlive their agent** — sibling issue #5110.
  Composes with this mechanism (killing the scope reaps everything inside it)
  but is a distinct problem: #5110 is about a process tree escaping teardown
  after its owning agent exits; this doc is about bounding CPU while the
  agent's work is still actively supervised.
