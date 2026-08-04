# Post-Builder Quality Gate (`buildGate`)

The post-builder quality gate is a deterministic, orchestrator-side check that runs **after the builder agent exits but before any PR is opened**. It short-circuits PR creation when the builder's work obviously isn't shippable, releases the issue claim, and lets the next builder re-attempt the issue.

See issue [#3347](https://github.com/rjwalters/loom/issues/3347) for the original proposal.

## Why a gate?

Builder agents (Claude Code as well as external engines invoked by parallel swarms) occasionally ship PRs that should never have been opened:

- broken builds,
- commits containing only logfiles / scratch files,
- no commits at all.

Without a gate, the Judge phase has to catch every one of these post-hoc, which wastes review cycles and pollutes the queue. The gate moves that filter ~30s of CPU instead of a multi-minute Judge cycle, and on parallel-sweep fleets the savings compound.

## The three checks

The gate runs three checks in order. Any failure short-circuits PR creation:

1. **has-commits** — `git rev-list --count origin/main..HEAD > 0` in the worktree.
2. **has-real-changes** — at least one changed file matches the configured `realChangeGlobs` (or the default scratch-exclusion list when no globs are configured).
3. **build-passes** — the configured `buildGate.command` exits with code 0 inside the worktree.

When all three pass the builder phase proceeds normally to PR creation. When any one fails the orchestrator:

- Atomically releases the claim: `loom:building` -> `loom:issue`.
- Logs an `error` milestone with `reason=build_failed_post_builder` and `check=<failed_check>`.
- Cleans up the stale worktree.
- Returns a `FAILED` `PhaseResult` so the sweep does not progress to Judge.

## Configuration

The gate is **opt-in**. Repos with no `buildGate` block in `.loom/config.json` see zero behavior change — the gate returns immediately.

```json
{
  "nextAgentNumber": 1,
  "terminals": [],
  "buildGate": {
    "enabled": true,
    "command": "cargo build --workspace",
    "realChangeGlobs": ["*.rs", "*.toml", "Cargo.lock"],
    "timeoutSeconds": 600
  }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `true` when block is present | Set to `false` to disable the gate without removing the block. |
| `command` | string | _(none)_ | Shell-style command run in the worktree (parsed with `shlex.split`). When omitted, the build check is skipped but the has-commits and has-real-changes checks still run. |
| `realChangeGlobs` | array of strings | _(default exclusions)_ | Positive globs. A changed file must match at least one to count as "real." When omitted, every changed file counts unless it matches one of the default scratch exclusions: `.loom-*`, `*.log`, `.no-changes-needed`. |
| `timeoutSeconds` | integer | `600` | Timeout for the `command` run. |
| `loadThreshold` | number | `0.9` | Daemon main-health gate only (#4259): 1-minute load average per logical CPU at/above which the gate DEFERS instead of running the full suite. Env override `LOOM_BUILD_GATE_LOAD_THRESHOLD`. See "Tiered gate + load-aware deferral". |
| `maxDeferSeconds` | integer | `1800` | Daemon main-health gate only (#4259): after this many seconds of consecutive load-deferred ticks, the FAST tier runs regardless of load so a permanently-loaded host still reaches a verdict. Env override `LOOM_BUILD_GATE_MAX_DEFER_SECS`. |
| `fastCommand` | string | _(derived)_ | Daemon main-health gate only (#4259): the command run for the fast tier. When omitted, the base `command` is run with `LOOM_BUILD_GATE_TIER=fast` prefixed. |

> **Not a `buildGate` key:** the daemon-side main-health gate's optional forge
> verification-workflow name (`autonomous.mainHealthGate.ciWorkflow` /
> `LOOM_GATE_CI_WORKFLOW`, #3987) lives under `autonomous.mainHealthGate`, **not**
> here. `buildGate` is the builder-side worktree quality gate; it has no business
> knowing about forge CI. See [Optional named verification workflow](daemon-reference.md#optional-named-verification-workflow-loom_gate_ci_workflow-3987).

## Examples

### Rust workspace

```json
{
  "buildGate": {
    "command": "cargo build --workspace",
    "realChangeGlobs": ["*.rs", "*.toml", "Cargo.lock"]
  }
}
```

### Python project with pytest

```json
{
  "buildGate": {
    "command": "python -m pytest -x",
    "realChangeGlobs": ["*.py", "pyproject.toml"]
  }
}
```

### Node.js project

```json
{
  "buildGate": {
    "command": "pnpm check:ci",
    "realChangeGlobs": ["*.ts", "*.tsx", "*.js", "package.json"],
    "timeoutSeconds": 900
  }
}
```

### Disable without removing config

```json
{
  "buildGate": {
    "enabled": false,
    "command": "cargo build"
  }
}
```

### This repo's configuration (polyglot backstop)

Loom's own `.loom/config.json` points `buildGate.command` at a committed
wrapper script rather than a single-language one-liner, because this repo is
polyglot (Rust + bash) and no single build tool covers it:

```json
{
  "buildGate": {
    "enabled": true,
    "command": "bash .loom/scripts/build-gate.sh",
    "realChangeGlobs": ["*.rs", "*.toml", "Cargo.lock", "*.py", "*.sh"],
    "timeoutSeconds": 600
  }
}
```

The wrapper lives at [`defaults/scripts/build-gate.sh`](../scripts/build-gate.sh)
(the installer-template source of truth; `.loom/scripts/build-gate.sh` resolves
to it via the `.loom/scripts -> ../defaults/scripts` symlink). It runs these
stages in order under `set -euo pipefail`, aborting on the first non-zero exit:

1. `cargo test --workspace --lib --bins` — the Rust crates' **unit tests**
   (`loom-daemon`, `loom-api`). The integration test **targets** under
   `loom-daemon/tests/` are deliberately excluded here — see "Local gate vs.
   CI" below.
2. `bash scripts/test-installer.sh` — the 131-case bash installer suite.
3. `bash scripts/test-changelog.sh` — `scripts/changelog.sh`'s unit suite
   (#5196), against a disposable scratch repo (`CHANGELOG_REPO_ROOT`); no
   network, no dependency on this repo's own history.

**The gate requires no Python toolchain at all (epic #4081 Phase 4, #4557;
finished by #4970).** Stage 2 used to be `cd loom-tools && uv run pytest
tests/ -q --ignore=tests/integration` over the whole `loom_tools` package.
That package was retired — everything it did is native in `loom-daemon`
now — so that stage would fail against a deleted path. A third, best-effort
stage covering the opt-in `loom-search` carve-out (`loom-tools/`'s last
surviving module) survived that retirement, but `loom-search` itself was
retired in #4970 per the operator's RETIRE decision on #4608 — see
[semantic-search.md](semantic-search.md) — so there is nothing left for a
Python-conditional stage to run, and it was removed rather than left as a
permanent no-op. See
[ADR-0013](https://github.com/rjwalters/loom/blob/main/docs/adr/0013-loom-tools-python-retirement.md).

**`mcp-loom` (TypeScript) is intentionally excluded** from the gate: it needs
`npm install`/`npm ci` in a fresh worktree (no guaranteed warm `node_modules`),
which would add unpredictable latency to a gate that also runs once per PR. CI
(`.github/workflows/ci.yml`) still gates the `mcp-loom` build. `timeoutSeconds:
600` gives ~2x headroom over the measured ~210s warm-cache total to absorb a
cold `target/` in a fresh worktree.

Beyond the per-wave step-8 gate, this same command runs after **every** builder
exit (the "Post-Builder Quality Gate" above), so it is deliberately kept fast
and free of network/npm dependencies. This is a repo-specific,
self-hosting-only config; `defaults/config.json` (the generic install template)
ships with no `buildGate` block. See issue
[#3749](https://github.com/rjwalters/loom/issues/3749).

### Local gate vs. CI: the environment-sensitivity boundary (#3985)

The local gate and GitHub CI run *nearly* the same Rust command, but they
measure different things, and that difference is deliberate:

| | Command | Measures |
|---|---------|----------|
| **CI** (`.github/workflows/ci.yml`) | `cargo test --workspace` (all targets, incl. integration) | **the commit**, in a controlled runner with a guaranteed-live tmux |
| **Local gate** (`build-gate.sh`) | `cargo test --workspace --lib --bins` (unit tests only) | **the commit**, on whatever host is actively running Loom |

The local gate runs on the machine that is *also running the sweeps* — a busy,
sometimes headless, sometimes tmux-less host. Any assertion in the gate that
depends on that host's configuration measures the **host**, not `main`, and so
can go red for a reason that has nothing to do with the code being gated. That
is inverted: a gate is supposed to be green when `main` is correct, not green
only when the host happens to be idle and fully provisioned.

Two concrete failure classes motivated the split (#3985):

- **Dead tmux server.** The `loom-daemon/tests/integration_basic.rs` terminal
  tests create real tmux sessions and assert they exist. On a host with no
  reachable tmux server they can only fail. These live in integration test
  **targets**, which `--lib --bins` excludes — so the gate never runs them,
  and CI (which always has tmux) still does. As belt-and-suspenders, those
  tests now **skip cleanly** (via a `require_tmux!()` probe) rather than fail
  when no tmux is available, so even a full local `cargo test --workspace`
  stays green on a tmux-less host.
- **Fixture-child wait bounds.** A few `sweep_registry` unit tests wait on a
  fixture child with a wall-clock deadline. Those bounds are now generous
  (`FIXTURE_CHILD_WAIT_MS`, 60s). This was originally attributed to *CPU
  starvation* under host load, but that attribution was **falsified**: #4044 /
  #4046 established the timing-test failures were macOS `syspolicyd`
  exec-latency artifacts, not contention (968/968 passed later with **no code
  change**). The generous bounds are still correct; the "sweeps starve the
  gate's timing tests" story that motivated them is not. For the operator-facing
  diagnostic — how to recognize `syspolicyd` saturation live and unwedge it — see
  [`troubleshooting.md`](troubleshooting.md) → "Several unrelated things hang at
  once (macOS Gatekeeper / `syspolicyd`)".

**The gate runs at a mild throttle relative to sweeps (#4020, revises #3985).**
`build-gate.sh` now defaults to `nice 5` — a mild positive niceness, a real but
small step down from the sweep children's `nice 0`. It previously re-exec'd
itself at `nice -n 19` (the lowest priority) on the rationale that a long
`cargo` compile would otherwise starve the sweeps and the timing-sensitive
tests it shares a host with. That rationale is **withdrawn**: #4044 / #4046
showed the timing-test failures were `syspolicyd` exec-latency artifacts, not
CPU contention, and the one real gate timeout was cold-compile cost (settled by
#4048 raising the budget 600→1200s; the gate then produced its first
determinate verdict, Green at ~726s). Handicapping the gate to the *bottom* of
the run queue solved a problem that was never demonstrated — and the gate is the
reliability substrate that halts dispatch when `main` goes red, so a gate
starved into `UNEVALUATED` is a gate that is not gating.

Why `nice 5` and not `nice 0` parity? Giving the gate a *strictly higher*
priority than sweeps is not possible from `build-gate.sh` on the daemon host
(macOS, non-root): a negative nice requires privilege the daemon does not hold
(`nice -n -5` → `setpriority: Permission denied`), so `0` is the best
unprivileged priority. Dropping the gate to `nice 0` would leave gate and sweeps
at the *same* value — an indistinguishable parity with no measurable gap, which
#4020's AC2 explicitly calls a failure ("A patch that leaves both at the same
value fails this AC"). `nice 5` is the smallest defensible choice that keeps a
real, non-zero, unprivileged-achievable gap in the achievable direction
(gate `5` > sweeps `0`): the gate yields *slightly* under contention rather than
starving the sweeps, without being drastically deprioritized as it was at 19.
The alternative — niceing sweep *children* up in the spawn path to force a
strict gate-below-sweep gap — was deliberately not done here at the time, since
the issue directed the spawn path be left untouched and the contention it would
brace against is the one the evidence above withdrew.

**Update (#4233): the inversion is now implemented, from the OTHER side.**
Issue #4231 (a 2026-07-27→28 host meltdown under 6-way sweep fan-out) diagnosed
*starvation*, not raw load, as the real failure — sweep worker processes (and
every `cargo`/`rustc`/test binary they spawn) ran at default niceness (0),
competing head-to-head with interactive/system processes. `spawn-claude.sh` now
re-execs itself at a mild positive niceness (default `nice 10`, configurable via
`LOOM_SWEEP_NICENESS` env / `autonomous.spawnNiceness` config, master disable
`LOOM_SWEEP_NICE=0`) using the same re-exec-with-sentinel pattern documented
above (`LOOM_SWEEP_NICED` mirrors `LOOM_BUILD_GATE_NICED`). An optional macOS
`taskpolicy -c <class>` scheduling-class layer (`LOOM_SWEEP_TASKPOLICY_CLASS` /
`autonomous.spawnTaskpolicyClass`, off by default) is available on top of nice.
See `spawn-claude.sh`'s own header comment for the full precedence chain. With
sweep children now at `nice 10` and the gate at `nice 5`, the gate has a real,
measured, unprivileged-achievable *higher* priority than sweeps — the reverse of
the `gate 5 > sweeps 0` relationship described above — so the gate now
structurally wins CPU contention against concurrently-dispatched sweep builds,
addressing the #4084 premise (below) as a side effect rather than requiring the
dispatch-suppression workaround alone.

The re-exec mechanism and its knobs are preserved: `LOOM_BUILD_GATE_NICENESS`
overrides the value (e.g. `=0` for exact sweep parity, `=19` to restore the old
handicap), `LOOM_BUILD_GATE_NICE=0` disables the re-exec entirely, and the
`LOOM_BUILD_GATE_NICED` sentinel prevents a re-exec loop. The re-exec is skipped
when the effective niceness is 0 (a `nice -n 0` re-exec is a no-op fork); with
the default of 5 the gate re-execs once under `nice -n 5`. If `nice` is
unavailable the gate proceeds at normal priority.

**Dispatch is suppressed while a gate run is in flight (#4084).** The mild
`nice 5` above was necessary but *not sufficient*: a gate build concurrent with
two sweep builds on a 28-core host still blew past the 1200s timeout — a >65%
wall-clock inflation attributable to CPU contention alone (the same repo compiled
green in ~726s idle). Niceness only reorders the run queue; it does not stop the
gate's own build from racing freshly-dispatched sweep builds for cores. So the
**daemon's work-finder holds new dispatch off a root while that root's
build-gate run is in flight**: the gate sets a per-root `gate_in_flight` flag for
exactly the lifetime of its `spawn_blocking` run (cleared on return *or* panic),
and the work-finder treats it as a dispatch suppressor alongside the
verified-red halt flag. Suppression is strictly **per root** — a sibling
workspace with no gate in flight keeps dispatching (the #3930 isolation
contract) — and it does **not** touch the timeout kill path: a genuinely hung
gate is still killed at `buildGate.timeoutSeconds` and reported `UNEVALUATED`.

The suppressor is controlled by `autonomous.mainHealthGate.suppressDispatchDuringGate`
(default **`true`**), with precedence **env > config > default**:

| Layer | How | Effect |
|-------|-----|--------|
| Env | `LOOM_MAIN_HEALTH_GATE_SUPPRESS_DISPATCH=1\|true\|yes\|on` (or any other value to disable) | Master override — wins over config |
| Config | `"autonomous": { "mainHealthGate": { "suppressDispatchDuringGate": false } }` | Used when the env var is unset |
| Default | — | On: dispatch is held during a gate run |

Set the knob (or `LOOM_MAIN_HEALTH_GATE_SUPPRESS_DISPATCH=0`) to `false` to
restore the pre-#4084 always-dispatch behavior. The cost of leaving it on is a
brief dispatch pause (~one gate build) each interval; the benefit is that the
gate's build reaches a determinate Green/Red verdict instead of timing out under
self-inflicted contention. Raising `buildGate.timeoutSeconds` is deliberately
*not* the lever here: the gate's cost scales with however many sweeps happen to
be in flight, so any fixed budget large enough for the worst case makes a truly
hung gate invisible for that long.

### Tiered gate + load-aware deferral (#4259)

`nice` (#4020/#4084) reordered the run queue but could not stop the gate's own
`cargo` build from *racing* freshly-dispatched sweep builds for cores. Under
6–7 concurrent sweeps the full suite blew past even the 1200s budget and was
killed — so the main-health gate reported `not evaluated (timeout …)`
**permanently**, silently disabling the red-main protection it exists for. Two
composed mechanisms fix that so the gate always produces *some* signal:

**1. A cheap FAST tier in `build-gate.sh`.** `LOOM_BUILD_GATE_TIER` selects the
stage set the wrapper runs:

| Tier | `LOOM_BUILD_GATE_TIER` | Stages | Cost |
|------|------------------------|--------|------|
| **full** (DEFAULT) | unset / `full` | `cargo test --lib --bins` + installer suite | minutes, cold |
| **fast** | `fast` | `cargo build --workspace --lib --bins` (compile) + a `loom-daemon --version` startup smoke | a few minutes cold, no test execution |

The default is unchanged when the variable is absent, so **CI parity, manual
invocations, and the per-builder post-builder gate all behave exactly as
before**. The fast tier verifies the compile/startup breakage class (#3647
step-8 — a `cargo build --workspace` catch) plus that the binary it just built
actually starts.

The fast tier's smoke step was `cd loom-tools && uv run python -c "import
loom_tools"` until epic #4081 Phase 4 (#4557) retired the Python package; it is
now `cargo run --package loom-daemon --bin loom-daemon -- --version`, which
catches the same "compiles but won't start" class (a panic in a static
initializer, a broken clap command tree, a missing dynamic dependency) with no
Python involved. `--version` touches no repo state, no forge, and no daemon
socket.

> **A fast-tier GREEN is NOT equivalent to a full-suite GREEN.** It does **not**
> run the Rust unit tests or the installer suite. `loom-daemon
> status` labels a fast-tier verdict (`clear(fast)` / `… [fast tier — compile+smoke
> only, NOT a full-suite green]`) precisely so the two are never confused.

**2. Bounded, visible load-aware deferral in the daemon.** Each gate tick,
`run_gate_tick` reads the host's 1-minute load average (reusing
`cpu_headroom::read_loadavg_1m()` / `logical_cpu_count()`) and decides:

- **load below threshold** (or **no load reading** — fail safe) → run the **full**
  tier;
- **host saturated, within the max-defer window** → **defer**: no command runs,
  and `loom-daemon status` reports `deferred (load …)` — distinct from
  `not evaluated (timeout …)`. A deferred tick is a *scheduling* decision, not
  an evaluation: it never records the SHA memo, never arms the indeterminate
  backoff, and never touches the halt flag;
- **host saturated past the max-defer window** → run the **fast** tier regardless
  of load, so a permanently-loaded host still reaches a verdict.

Deferral is **bounded** on purpose: an unbounded defer on a host that is *always*
at cap would re-disable the gate exactly like the timeout did, only more cheaply.
After `maxDeferSeconds` (default 30 min) of consecutive deferred ticks the fast
tier runs, guaranteeing a real (if narrower) Green/Red at least that often — a
genuinely red `main` is still caught within one such cycle. A fast-tier Red halts
dispatch with the same semantics as a full-tier Red.

All three knobs honor **env > config > default**:

| Knob | Env | Config (`buildGate.*`) | Default |
|------|-----|------------------------|---------|
| Saturation threshold (1m load ÷ logical CPUs) | `LOOM_BUILD_GATE_LOAD_THRESHOLD` | `loadThreshold` | `0.9` |
| Max-defer window (seconds) | `LOOM_BUILD_GATE_MAX_DEFER_SECS` | `maxDeferSeconds` | `1800` (30 min) |
| Fast-tier command | — | `fastCommand` | `LOOM_BUILD_GATE_TIER=fast <command>` |

`fastCommand` is optional: when omitted the daemon runs the base `command` with
`LOOM_BUILD_GATE_TIER=fast` prefixed (the shipped `build-gate.sh` honors it). The
load average overstates consumption on macOS (see "measured idle fraction",
#4031), so an operator on a chronically-loaded host may raise `loadThreshold`.

Explicitly **out of scope** here (separable follow-ups): a dedicated gate cargo
target-dir to isolate contention (never shipped by #4020); per-test timeouts
(would require adopting cargo-nextest); and any change to sweep spawn priority
(that is #4233). The **shared build lock** half of that first item did land
later — see the next section.

### Machine-wide build slot (#4512)

`nice` (#4020) reorders the run queue and the gate-in-flight flag (#4084) holds
*dispatch* off one root, but neither stops the genuinely concurrent case: N
sweep worktrees each running their own post-Builder `build-gate.sh` in separate
processes, plus the daemon's own main-health gate, all compiling at once. Until
#4512 the daemon's admission formula tried to prevent that up front with a CPU
estimate (`estCoresPerSweep × cpuUtilizationTarget` minus live idle) — which
priced *every* sweep as a build and throttled a 95%-idle 8-core host to 2
concurrent sweeps. #4512 deleted that term and moved the protection here, to the
stage that actually burns the cores.

**Every invocation of `build-gate.sh` takes one machine-wide build slot before
compiling**, and releases it on exit (via an `EXIT` trap, so a failed or killed
gate never leaks the slot). N sweeps run concurrently; at most
`LOOM_BUILD_SLOTS` (default **1**) of them build at any moment.

- **Mechanism**: `defaults/scripts/lib/build-slot.sh`, sourced by
  `build-gate.sh` (`loom_build_slot_acquire` / `loom_build_slot_release`). A
  slot is a lock **directory** created with `mkdir` — POSIX-atomic and available
  on stock macOS, unlike `flock` — at `~/.loom/locks/build-slot/slot-<i>`,
  machine-wide rather than per-repo, because cores are a machine-level resource
  shared by every workspace and worktree on the host. The daemon's Rust half
  (`loom-daemon/src/build_slot.rs`) implements the identical protocol and wraps
  the main-health gate's own command, so the two serialize against each other.
- **Never blocks the gate indefinitely**: the acquire waits at most
  `LOOM_BUILD_SLOT_WAIT_SECS` (default 300s) and then **degrades open**,
  proceeding unserialized. A wedged holder costs one build's worth of
  serialization, never the gate's liveness.
- **Never fails the gate**: an unusable lock directory (unwritable, missing
  parent, a file in the way) also degrades open, with a warning. `mkdir` locks
  carry no heartbeat, so an abandoned slot is reaped after
  `LOOM_BUILD_SLOT_STALE_SECS` (default 1h — deliberately long, so a peer cannot
  reap a healthy in-progress build).
- **Re-entrant**: when the daemon already holds a slot around this gate command
  it exports `LOOM_BUILD_SLOT_HELD=1`, and the acquire inside `build-gate.sh` is
  a logged no-op instead of a wait on its own parent's slot.
- **Opt out** with `LOOM_BUILD_SLOTS=0` (every acquire degrades open — exactly
  the pre-#4512 behavior). If `lib/build-slot.sh` is missing from an install the
  gate prints a note and runs unserialized.
- **Interaction with the timeout**: the daemon acquires the slot **outside** the
  `buildGate.timeoutSeconds` window, so queueing behind another build never eats
  into the gate's own budget and turns contention into a false `UNEVALUATED`.
- **Interaction with deferral**: the load-aware deferral above is a *scheduling*
  decision made before any command runs; the slot is a *mutual-exclusion*
  decision made at the moment of compiling. They compose — a deferred tick never
  reaches the acquire at all.

Full rationale, telemetry, and the env-knob table live in
[`daemon-reference.md` → Machine-wide build slot](daemon-reference.md).

> **Rule of thumb:** if a check's outcome can differ between an idle host and a
> busy one — or between a host with tmux and one without — it is
> **environment-sensitive** and belongs in CI (which controls its
> environment), not in the local post-builder gate. Keep the gate scoped to
> checks that are deterministic on any host.

**Forge-CI corroboration (deferred).** The curated issue's fourth acceptance
criterion — when the local gate disagrees with a *green* forge CI result on the
same evaluated SHA, prefer the forge signal and log the divergence loudly
rather than halting — is a distinct, larger daemon-side feature (it belongs
with the RED-classification work in #3974 AC4, not the test/scope hardening
here). It is intentionally **not** implemented in this change; the scope split
+ test hardening + `nice` above already break the self-reddening loop this
issue targets. Tracked as follow-up under #3974.

## Failure semantics

A gate failure is **not** the same as a builder failure: the issue is automatically re-queued (`loom:issue`) and a future builder can take a fresh attempt. The `PhaseResult.data` block carries:

```python
{
  "post_builder_gate_failed": True,
  "gate_check": "has_commits" | "has_real_changes" | "build_passes",
  "gate_detail": "<human-readable failure reason>",
  "reason": "build_failed_post_builder",
  "claim_released": True,
}
```

These fields are available in sweep logs (`.loom/logs/sweep-issue-N.log`) and sweep checkpoints (`.loom/sweep-checkpoint/issue-N.json`) for postmortem analysis.

## Why orchestrator-side?

The gate intentionally lives in the orchestrator's builder phase (the `/loom:sweep` skill's Builder step), not in the builder *role* prompt. The point is deterministic enforcement independent of agent self-discipline: an agent that crashed, was rate-limited, or simply ignored its prompt should still not produce a PR.
