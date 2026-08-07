# Per-Sweep CPU Budget (issue #5111)

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
`max(1, total_logical_cores - reserved_cores)`, mirroring the daemon's own
`min(16, cpu_cores - 2)` agent-concurrency rule (default `reserved_cores =
2`). **If you are writing a driver that fans out CPU-bound work — a SPICE
corner sweep, a parallel build, anything that spawns more than a couple of
heavy child processes — read this env var and cap your own concurrency to
it.** This is the "published parallelism budget" direction: an explicit,
documented number instead of an implicit assumption about how many cores are
"probably" free.

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
| `LOOM_SWEEP_WALLCLOCK_CEILING_SECS` | `autonomous.spawnWallClockCeilingSecs` | `0` (disabled) | Adds `RuntimeMaxSec=<secs>` to the systemd scope when non-zero. |
| `LOOM_SWEEP_CPU_BUDGET_CORES` | — | *(output only)* | Exported into the child with the computed budget; read it, don't set it. |

Precedence for the two tunables: env > config > default, the same tier order
used throughout Loom (see `spawn-claude.sh`'s own niceness knobs, #4233).

## What this does NOT cover

- **Multiple concurrent sweeps oversaturating a host in aggregate** — each
  sweep's own scope is capped, but nothing here prevents several independently
  capped sweeps from summing to more than the host's core count. That is the
  admission side's job ([`admission_brake`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/admission_brake.rs),
  [`host_breaker`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/host_breaker.rs)),
  not this mechanism's.
- **Orphaned process trees that outlive their agent** — sibling issue #5110.
  Composes with this mechanism (killing the scope reaps everything inside it)
  but is a distinct problem: #5110 is about a process tree escaping teardown
  after its owning agent exits; this doc is about bounding CPU while the
  agent's work is still actively supervised.
