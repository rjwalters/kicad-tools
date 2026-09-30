# Long-Running Compute on a Dispatch Host (Issue #8478)

**The rule: a Builder must never leave a detached long-running process on the
dispatch host.** Not backgrounded with `&` and disowned, not double-forked, and
above all not handed to `launchctl submit`. When your session ends, nothing you
started may still be running.

This is not a style preference. It is the conclusion of a real 12-hour outage,
and the reason the rule reads as an absolute rather than a "be careful" is that
three separate sessions were each individually careful and each produced the
same failure.

## What happened

On 2026-09-20 a Builder on a macOS dispatch host (18-core M-series) needed a
multi-hour `ngspice` corner grid. It measured that children of the agent-session
shell were confined to a background CPU scheduling band — 3.60 s user for a
fixed benchmark from the agent shell vs 1.24 s for the same benchmark under
launchd, and roughly 0.4 of a core in aggregate regardless of `-j` — and escaped
it with:

```bash
launchctl submit -l my-sim-job -- /path/to/run-grid.sh    # DO NOT DO THIS
```

`launchctl submit` creates a **KeepAlive** launchd job. `launchctl print` on it
shows `properties = keepalive`. launchd therefore re-runs the program **every
time it exits**, forever, and the submitted scripts were one-shots. Found ~12
hours later:

- the completed 45-point `lt` grid had been restarted and was running a
  duplicate grid concurrently with the next deck (`runs = 2`);
- a finished single-point probe was on `runs = 3`, each rerun truncating its own
  log and timing output;
- the sequencing script would have re-run its whole chain forever.

25 `ngspice` processes on 18 cores → load average 58. The owning sweep had ended
long before (the issue was back to `loom:issue`, no process in the worktree), so
nothing was ever going to stop it. `loom-daemon`'s `admission_brake` held every
admission on that host for the full twelve hours (`187 deferred (host
saturated)`), `gh` calls hit their 10 s timeout, and `build_daemon_status` ran
over budget under the same load.

**Why it was not a one-off.** Three earlier sessions on the same issue tried
plain backgrounding, then a hand-written double-fork daemonizer, then
`launchctl submit`. Each is an agent independently reinventing "run a long job
that outlives my session at normal priority", and each left an unowned process
on a shared dispatch host. The escalation is the tell: the pressure is real, so
the answer has to be a *sanctioned path*, not only a prohibition.

## The decision ladder

When you have compute that will not fit in your session, take the first of these
that applies:

1. **Use the repo's batch/remote compute backend, if it has one.** Some repos
   ship an elastic batch runner, a CI job that accepts a long run, or a
   dedicated compute host. Work dispatched there runs off the dispatch host
   entirely and is owned by that system's own lifecycle. This is always the
   right answer when it exists. (Loom itself has no such backend today — an
   `ephemeral_compute` telemetry record type is proposed but unbuilt, #8257 —
   so in *this* repo the ladder starts at step 2.)

2. **Scope the run to fit the session.** Read
   `LOOM_SWEEP_CPU_BUDGET_CORES` (see [`cpu-budget.md`](cpu-budget.md)) and cap
   your own fan-out to it, then narrow the *work*: a 3-corner smoke grid that
   proves the harness in 20 minutes is a shippable increment; a 45-point grid
   that needs 6 hours is not, in one session. Land the harness plus the reduced
   run, and file the full sweep as a follow-up issue naming exactly what is left
   to compute.

3. **Hand off with `loom:blocked`, naming the compute gap.** If the issue's
   acceptance criteria genuinely cannot be met without multi-hour compute and
   the repo has no batch backend, that is a real blocker and it is *information*
   — say so rather than engineering around it:

   ```bash
   gh issue comment 303 --body 'Blocked on compute: the acceptance criteria need
   a 45-point ngspice corner grid (~6h wall at this host'"'"'s available share).
   No batch/remote backend is configured for this repo, and a detached local run
   is forbidden (.loom/docs/long-running-compute.md). Needs either a batch
   backend or an operator-run grid.'
   gh issue edit 303 --add-label "loom:blocked"
   ```

   A `loom:blocked` issue with a named compute gap is a solvable operator
   problem. An unowned process on a dispatch host is not — nobody knows it
   exists.

**"Just background it and end my turn" is not on this ladder.** It is also
already forbidden from the other direction: `builder.md`'s "Never End Your Turn
on a Background Build or CI Monitor" rule says a check you started must be
resolved inside the same turn. That rule and this one are the two halves of one
constraint — your *turn* may not end on pending work, and your *process* may not
outlive your session. A Builder facing a genuinely multi-hour job that satisfies
the first rule by daemonizing has violated the second, which is precisely the
move this document exists to close off.

## If launchd/systemd dispatch is ever legitimately needed

Rare, and only with an operator's explicit involvement. If it happens:

- **Never `launchctl submit`.** There is no way to submit a non-KeepAlive job
  through it — the KeepAlive is baked into the subcommand, not a flag you
  forgot. Write a plist with explicit `RunAtLoad`/`KeepAlive` keys instead
  (`loom-daemon-start.sh`'s `render_launchd_plist` is the worked example of
  getting those keys right: `KeepAlive: {SuccessfulExit: true}`, not bare
  `KeepAlive`).
- **The dispatched script must remove its own job as its last line.** This is
  the self-termination contract that makes a one-shot actually one-shot:

  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  LABEL="my-one-shot-job"
  trap 'launchctl remove "$LABEL" 2>/dev/null || true' EXIT   # runs on failure too
  # ... the actual work ...
  ```

  A `trap ... EXIT` rather than a literal last line, so an early failure or a
  signal still unregisters the job. Without it, a non-zero exit under KeepAlive
  is exactly the infinite-rerun loop above.
- **Tell somebody.** Name the label in a PR or issue comment. A job whose label
  is written down can be found with `launchctl print gui/$UID/<label>` and
  stopped; one that is not written down is found by load average, hours later.

## Cleaning up an escape that already happened

```bash
launchctl list | grep -i <suspected-label>        # find it
launchctl print gui/$UID/<label>                  # confirm `properties = keepalive`, `runs = N`
launchctl remove <label>                          # unregister; stops the reruns
pkill -f <program>                                # then reap what is still running
```

`launchctl remove` first, always. Killing the processes while the job is still
registered just triggers the next KeepAlive rerun.

## Diagnosing it from the daemon side

Since #8478 the daemon names the culprit instead of only reporting saturation:

- **In the log.** `admission_brake: STARVING …` and `STARVATION ESCAPE HATCH …`
  lines carry a best-effort `ps` attribution of the top host-wide CPU consumers
  by command, instance count, and parent process — e.g. `ngspice ×25 (1843%
  cpu, parent launchd[1], reparented to pid 1)`. **A compute process whose
  parent is pid 1 has been reparented to `launchd`/`init`: no live Loom session
  owns it.** That is the escape signature.
- **Off-host.** `host.health` telemetry carries an `admission_brake` object with
  the starvation duration and a `dispatch_suppressed_by_foreign_load` flag, and
  such a host also reports `dispatch_halted: true` — so a fleet-level view shows
  it as *dispatch suppressed by foreign load* rather than as a quiet host. See
  [`telemetry-schema.md`](telemetry-schema.md) → `host.health`.
- **Host-locally.** `loom-daemon status --json` → `admission_brake` carries
  `starving_secs` alongside the pre-existing `starving_since`/`starving_ticks`.

## Related

- [`cpu-budget.md`](cpu-budget.md) — `LOOM_SWEEP_CPU_BUDGET_CORES`, the
  published per-sweep parallelism budget a fan-out harness must read, and the
  earlier incidents (#5111, #5979) where an agent-written driver oversubscribed
  a host *while still supervised*. This document is the complement: the process
  that outlives supervision.
- [`macos-agent-qos-band.md`](macos-agent-qos-band.md) — the QoS confinement
  that created the pressure to escape in the first place, and why the daemon's
  own launchd job sets `ProcessType = Background`.
- `loom-daemon/src/admission_brake.rs` / `loom-daemon/src/foreign_load.rs` — the
  hold, the starvation escape hatch (#5715), and the attribution probe.
