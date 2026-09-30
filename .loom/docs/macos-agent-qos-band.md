# Finding: the macOS agent-shell CPU QoS band (Issue #8478, item 4)

**Verdict: CONFIRMED, and by design — but the design has an unintended blast
radius, and one of the two causes is not the one the incident report guessed.**

This is a *finding*, not a change. Nothing in this document ships a behaviour
change; it exists so the next agent that measures a 3× CPU slowdown on a macOS
dispatch host knows where it comes from instead of rediscovering it and
[escaping it](long-running-compute.md).

## The observation being explained

A Builder on an 18-core M-series macOS dispatch host measured, for a fixed CPU
benchmark:

| Where it ran | User time |
|---|---|
| From the agent-session shell | 3.60 s |
| Under launchd (via `launchctl submit`) | 1.24 s |

and roughly **0.4 of a core in aggregate regardless of `-j`** from the agent
shell. See [`long-running-compute.md`](long-running-compute.md) for what it then
did about it, and why that was worse than the problem.

## Cause 1 (dominant, confirmed by code inspection): `ProcessType = Background`
## on the daemon's own launchd job, inherited by the whole process tree

`defaults/scripts/cli/loom-daemon-start.sh` renders the daemon's LaunchAgent
plist with:

```
<key>ProcessType</key>
<string>Background</string>
```

at `render_launchd_plist` (the daemon job) and again in the watchdog job's
renderer. `safehoused-service.sh` does the same for the safehouse service.

Per `launchd.plist(5)`, `ProcessType` "describes the intended purpose of the
job" and the system "applies resource limits based on what kind of job it is";
`Background` is documented as being for "processes that do work that was not
directly requested by the user", with limits "intended to prevent background
jobs from disrupting the user experience". On Darwin that is implemented as a
**task role / task policy** on the job's task, and task policy is inherited
across `fork`/`posix_spawn` — which is what makes it a property of the tree, not
of the one process.

Every Loom agent session on a launchd-managed host is inside that tree:

```
launchd (ProcessType=Background)
└── loom-daemon
    └── spawn-claude.sh          (role_runner.rs / sweep_registry spawn this)
        └── claude / claude-wrapper.sh
            └── cargo / rustc / ngspice / …
```

So the band applies to everything a Builder runs locally — its builds, its test
suites, and any compute harness it writes. `launchctl submit` produced a *new,
separate* launchd job with the default `ProcessType` (`Standard`), which is
exactly why it measured ~3× faster: it left the tree.

There is already a code comment adjacent to this, at
`loom-daemon-start.sh`'s `KeepAlive` block, flagging "lingering `claude`/`tee`/
`sleep` children under this job's `ProcessType=Background`" as a known concern
— filed under #4862, but scoped there to a `SuccessfulExit`-detection question,
not to CPU confinement. The inheritance was known; its performance consequence
was not written down anywhere an agent would find it.

## Cause 2 (secondary, confirmed): `nice -n 10` from `spawn-claude.sh`

`defaults/scripts/spawn-claude.sh` re-execs itself under `nice -n 10` by
default (#4233), deliberately above the build gate's `5` so sweeps yield to the
gate under contention. Because `exec` preserves the PID, the niceness survives
into the whole subsequent tree.

This contributes, but it cannot be the dominant term: `nice` is a *relative*
scheduling weight, so on an otherwise-idle 18-core host it should cost close to
nothing, and it certainly cannot explain "~0.4 of a core in aggregate regardless
of `-j`". An absolute cap on the tree's share is the signature of a task-policy
band, not of niceness.

## Not the cause: `taskpolicy`

The incident report's guess named `taskpolicy` as a candidate. Checked, and it
is **not** implicated by default: `spawn-claude.sh`'s `taskpolicy -c <class>`
mechanism (`LOOM_SWEEP_TASKPOLICY_CLASS` / `autonomous.spawnTaskpolicyClass`) is
opt-in and **unset at every tier by default**, and its own comment explicitly
avoids `-b`/`DARWIN_BG` because that throttles disk I/O and network too. On a
default host it never runs.

Worth keeping the distinction straight, because the two are different
mechanisms: `taskpolicy` is a per-process scheduling-class hint applied by a
caller; launchd's `ProcessType` is a job-level band inherited by an entire job
tree. Setting the former to `utility` would not undo the latter.

## Should the daemon plist set `ProcessType = Interactive` / `Standard`?

**Recommendation: not unconditionally, and not in this change.** The reasoning,
stated so the next person does not have to re-derive it:

- **`Background` is correct for the daemon process itself.** It is a polling
  supervisor doing work nobody asked for interactively. That is the literal
  definition in `launchd.plist(5)`, and it is why the value was chosen.
- **It is wrong for the *sweep* subtree**, which is a user-requested build doing
  latency-sensitive work. The bug is not the value, it is that one plist key
  governs two populations with opposite needs.
- **Flipping the daemon job to `Standard` would change every Loom host's
  scheduling behaviour at once**, including operators' personal laptops where
  `Background` is what keeps the fleet from making the machine feel slow. That
  is a fleet-wide behavioural change and it wants its own issue, its own
  rollout, and a measurement on real hardware — not a side effect of a logging
  PR.
- **The narrower fix is to un-band the subtree, not the daemon.** macOS exposes
  exactly that: `taskpolicy -c <class>` on the spawned process, and
  `spawn-claude.sh` *already has the hook wired* (opt-in, unset). Whether
  `taskpolicy -c utility` (or `standard`) actually escapes an inherited
  `ProcessType=Background` band, or merely adjusts a class within it, is the
  open empirical question — and it is the one worth measuring first, because if
  it does, the fix is a one-line default change on a knob that already exists
  and is already documented.

**Linux hosts are unaffected.** `render_systemd_unit` sets `Restart=on-success`
and `KillMode=mixed` and no `Nice=`/`CPUWeight=`/`CPUSchedulingPolicy=`, so
there is no systemd analog of this band. Only the `nice -n 10` from
`spawn-claude.sh` applies there.

## What was NOT verified, and how to verify it

**This finding is derived from code inspection plus `launchd.plist(5)`/Darwin
task-policy semantics. It was not measured — no macOS host was available to the
session that wrote it.** Specifically unverified:

1. That `ProcessType=Background`'s band is in fact inherited by `posix_spawn`ed
   descendants on current macOS, as opposed to applying only to the job's own
   task. This is the load-bearing claim.
2. The magnitude attribution between Cause 1 and Cause 2 (the reasoning above is
   an argument from the shape of the numbers, not a measurement).
3. Whether `taskpolicy -c utility`/`-c standard` on the spawned process escapes
   an inherited band.

Recipe, for whoever has the hardware. Run all of it on the **same** otherwise-
idle macOS host, same benchmark binary, and report user time plus aggregate CPU:

```bash
# 0. A fixed CPU benchmark. Anything deterministic and single-threaded-per-job.
BENCH='for i in $(seq 1 4); do (yes > /dev/null & ) ; done; sleep 5; pkill yes'
#    ...or better, a real `time`-able unit of work, e.g.:
BENCH='time openssl speed -seconds 5 sha256'

# 1. Baseline: a plain login shell (NOT under the daemon's job).
#    Expect: fast. This is the "under launchd / Standard" number.
ssh localhost "$BENCH"

# 2. Inside a live agent session on that host (the confined case).
#    Expect: ~3x slower if the band is inherited.
#    Run it as a Bash tool call from a real sweep, or reproduce the tree:
#      launchctl print gui/$UID/<loom daemon label> | grep -i 'process type\|role'
#    and confirm the daemon job's own band first.

# 3. Attribute the two causes. From inside the agent session:
nice -n 0 sh -c "$BENCH"      # removes Cause 2 only; if still slow => Cause 1 dominates
taskpolicy -c standard sh -c "$BENCH"   # tries to escape Cause 1; if this is fast, the
                                        # one-line fix is spawnTaskpolicyClass=standard

# 4. Confirm the band directly rather than inferring it from timing:
#    the daemon's own task role, and a sweep child's:
launchctl print gui/$UID/<label> | grep -iE 'process type|role|throttle'
ps -o pid,ppid,command -p <a claude child pid>
taskpolicy -p <that pid>       # prints the process's current policy, if supported
```

Report the numbers on #8478 (or a follow-up), and if step 3's
`taskpolicy -c standard` line is fast, that is the fix: flip
`autonomous.spawnTaskpolicyClass`'s default in `spawn-claude.sh`, which changes
one already-documented knob instead of every host's launchd job.

## Why this matters even though nothing changed here

The band is the *pressure* behind #8478. A Builder that finds its local builds
running at a third speed, with no documentation explaining why and no sanctioned
way to get normal priority, will try to escape — three consecutive sessions did,
escalating from `&` to a double-fork to `launchctl submit`. Writing the cause
down is half the mitigation; [`long-running-compute.md`](long-running-compute.md)
is the other half.

## Related

- [`long-running-compute.md`](long-running-compute.md) — the rule, the incident,
  and the sanctioned alternatives.
- [`cpu-budget.md`](cpu-budget.md) — `LOOM_SWEEP_CPU_BUDGET_CORES` and the
  `nice -n 10` / `systemd-run --scope CPUQuota` mechanisms (#4233, #5111, #5979).
- `defaults/scripts/cli/loom-daemon-start.sh` — `render_launchd_plist`,
  `render_watchdog_plist`, `render_systemd_unit`.
- `defaults/scripts/spawn-claude.sh` — the niceness re-exec and the opt-in
  `taskpolicy` hook.
