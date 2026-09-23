#!/usr/bin/env bash
# build-gate.sh - buildGate.command for this repo: fast build+test backstop
# across Rust and bash. Runs in the worktree; exits non-zero on the first
# failing stage (set -e) so buildGate.command's single exit code is
# meaningful. See .loom/docs/build-gate.md.
#
# Scope decisions (issue #3749):
#   - The Rust crates (loom-daemon, loom-api) are covered by `cargo nextest run
#     --profile ci` when cargo-nextest is installed — the same runner and
#     profile CI uses — falling back to `cargo test` with a loud warning when it
#     is not (#8326; the shared-process race is #4385). Doctests are a separate
#     `cargo test --workspace --doc` step because nextest does not run them. As
#     of #3985 the Rust step is scoped to `--lib --bins` (crate unit tests only) and
#     deliberately EXCLUDES the integration test TARGETS under
#     loom-daemon/tests/ (integration_basic.rs et al). Those spin up a real
#     tmux server and are therefore host-dependent — green only where a live
#     tmux is reachable. The local gate runs on the very host that is actively
#     running Loom (a busy, sometimes tmux-less machine), so a host-dependent
#     assertion there measures the host, not `main`. CI (.github/workflows/
#     ci.yml) controls its environment and still runs the FULL
#     `cargo nextest run --workspace --profile ci`, so the integration targets
#     are covered exactly where their environment is guaranteed. See
#     "Local gate vs. CI" in
#     .loom/docs/build-gate.md.
#   - The gate is ZERO-PYTHON as of epic #4081 Phase 4 (#4557). It used to run
#     `cd loom-tools && uv run pytest tests/` (full tier) and
#     `uv run python -c "import loom_tools"` (fast tier); the Python package was
#     retired, so both stages would now fail against a deleted path. The
#     package's last Python residue, the opt-in `loom-search` carve-out, was
#     itself retired in #4970 (per the operator's RETIRE decision on #4608) —
#     there is now no Python anywhere in the repo, load-bearing or otherwise,
#     and no Python-conditional stage left to run.
#   - bash scripts/test-installer.sh runs the 131-case installer suite.
#   - bash scripts/test-changelog.sh runs scripts/changelog.sh's unit suite
#     (#5196) against a disposable scratch repo (CHANGELOG_REPO_ROOT) -- no
#     network, no dependency on this repo's own history, sub-second.
#   - bash scripts/test-install-local-mode.sh (#5276) covers install-loom.sh
#     --local/--gitignore mode -- no daemon build, no network, sub-second.
#   - bash scripts/test-migrate-consumer.sh (#5276) covers scripts/install/
#     migrate-consumer.sh (Epic #3835 Phase 6) against throwaway git fixtures
#     -- no network, no real daemon, sub-second.
#   - bash scripts/test-daemon-liveness.sh (#5548) regression-tests
#     scripts/stop-daemon.sh's and scripts/start-daemon.sh's daemon-liveness
#     pgrep matcher against a decoy fixture literally named `loom-daemon` --
#     no real daemon build, PATH-stubs `pgrep` for the one case that would
#     otherwise touch the live process table, sub-second.
#   - mcp-loom (TypeScript) is intentionally EXCLUDED: it needs npm install/ci
#     in a fresh worktree (no guaranteed warm node_modules), which adds
#     unpredictable latency to a gate that also runs once per PR. CI still
#     gates the mcp-loom build.
set -euo pipefail

# Gate/sweep scheduling priority (#4020, revises #3985).
#
# #3985 re-exec'd the gate at `nice -n 19` (the lowest priority) so it "could
# never starve the sweeps it shares a host with." That rationale rested on a
# now-FALSIFIED premise: that concurrent sweep builds were starving the gate's
# (and sweeps') timing-sensitive `cargo` tests. #4044/#4046 established those 17
# red daemon tests were macOS `syspolicyd` exec-latency artifacts, not CPU
# contention (968/968 passed later with no code change), and the one real gate
# timeout was cold-compile cost, settled by #4048 raising the budget 600->1200s
# (the gate then produced its first determinate verdict, Green at ~726s). So the
# gate was handicapped to the bottom of the run queue to solve a problem that was
# never real. The extreme 19-point handicap is withdrawn.
#
# The gate is NOT restored to `nice 0` parity with sweeps, though. Sweep children
# spawn at the default niceness (0); making the gate also 0 leaves both at the
# same value, and on the daemon host (macOS, non-root) a strictly-higher gate
# priority is not achievable from here — a lower (negative) nice requires
# privilege the daemon does not hold (`nice -n -5` => "setpriority: Permission
# denied"), so gate and sweeps would sit at an indistinguishable nice 0 with no
# measurable gap between them (AC2 of #4020 calls that a failure: "A patch that
# leaves both at the same value fails this AC").
#
# So the gate defaults to a MILD POSITIVE niceness (5): high enough above the
# sweep default (0) to be a real, non-zero, unprivileged-achievable gap — the
# gate yields slightly under contention so it can never starve the sweeps it
# shares a host with — but nowhere near the extreme nice-19 bottom-of-the-queue
# handicap that starved the gate itself into UNEVALUATED (the gate is the
# reliability substrate that halts dispatch when `main` goes red; a gate that
# never gets scheduled is a gate that is not gating). gate=5 > sweeps=0 is a
# measured one-sided gap in the achievable direction, satisfying AC2.
#
# The alternative — niceing sweep children *up* in the spawn path (loom-daemon
# spawn_child + spawn-claude.sh) to force a strict gate<sweep gap — is
# deliberately not done here: the issue directed the spawn path be left
# untouched, and the contention it would brace against is the one the evidence
# above withdrew.
#
# The re-exec mechanism and its knobs are preserved: LOOM_BUILD_GATE_NICENESS
# overrides the value (e.g. =0 for parity, =19 to restore the old handicap),
# LOOM_BUILD_GATE_NICE=0 disables the re-exec entirely, and the
# LOOM_BUILD_GATE_NICED sentinel guards against a re-exec loop. The re-exec is
# skipped when the effective niceness is 0 (a `nice -n 0` re-exec is a no-op
# fork); with the default of 5 the gate re-execs once under `nice -n 5`.
# Best-effort: if `nice` is absent we just proceed at normal priority.
_gate_niceness="${LOOM_BUILD_GATE_NICENESS:-5}"
if [[ -z "${LOOM_BUILD_GATE_NICED:-}" \
      && "${LOOM_BUILD_GATE_NICE:-1}" != "0" \
      && "${_gate_niceness}" != "0" ]] \
   && command -v nice >/dev/null 2>&1; then
  export LOOM_BUILD_GATE_NICED=1
  exec nice -n "${_gate_niceness}" "$0" "$@"
fi

# Machine-wide build slot (#4512).
#
# #4512 removed the CPU-headroom term from the daemon's admission formula (it
# priced every sweep as a build and throttled a 95%-idle host to 2 concurrent
# sweeps) and moved the protection HERE, to the stage that actually burns the
# cores. Every invocation of this gate — the daemon's main-health gate, and each
# sweep's post-builder quality gate in its own worktree — takes one machine-wide
# slot before compiling, so N sweeps can run while at most LOOM_BUILD_SLOTS of
# them build. Without this, deleting the admission-time CPU term would leave
# nothing between N concurrent `cargo test --workspace` runs and the host.
#
# The lease NEVER blocks indefinitely and NEVER fails: it waits at most
# LOOM_BUILD_SLOT_WAIT_SECS (default 300) and then degrades open, so a wedged or
# crashed holder costs one build's worth of serialization, never this gate's
# liveness. LOOM_BUILD_SLOTS=0 opts out entirely. When the daemon already holds
# a slot around this command it exports LOOM_BUILD_SLOT_HELD=1 and the acquire
# below is a re-entrant no-op rather than a wait on our own parent's slot.
_build_slot_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/build-slot.sh"
_build_slot_available=false
if [[ -f "$_build_slot_lib" ]]; then
  # shellcheck source=lib/build-slot.sh
  source "$_build_slot_lib"
  _build_slot_available=true
else
  echo "[build-gate] note: lib/build-slot.sh not found — running unserialized (#4512)" >&2
fi

# Per-step toolchain timeout + process-group self-reap (Issue #6192).
#
# 2026-08-14 incident: a wedged build volume left every `cargo` invocation
# blocked forever inside an uninterruptible `open()`. Nothing here bounded an
# individual toolchain invocation, so a stuck build just sat there — and each
# later gate run (or Builder retry) piled a NEW `cargo` on top of the still-
# hung one instead of noticing and backing off. `bounded_run` (below) gives
# each stage of THIS gate a generous, configurable wall-clock budget so a
# hung command fails loudly (naming itself + its elapsed time) instead of
# hanging the gate indefinitely; `loom_reap_own_process_group` (below) sweeps
# any child this gate leaves behind — a killed-but-still-D-state build, a
# pipe-holding `tail`, etc. — when the gate itself exits, so nothing it
# spawned outlives it as an orphan re-parented to launchd.
_bounded_run_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/bounded-run.sh"
_bounded_run_available=false
if [[ -f "$_bounded_run_lib" ]]; then
  # shellcheck source=lib/bounded-run.sh
  source "$_bounded_run_lib"
  _bounded_run_available=true
else
  echo "[build-gate] note: lib/bounded-run.sh not found — stages run unbounded (#6192)" >&2
fi

_reap_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/reap-process-group.sh"
if [[ -f "$_reap_lib" ]]; then
  # shellcheck source=lib/reap-process-group.sh
  source "$_reap_lib"
fi

_build_gate_exit_cleanup() {
  if [[ "$_build_slot_available" == "true" ]]; then
    loom_build_slot_release
  fi
  if declare -F loom_reap_own_process_group >/dev/null 2>&1; then
    loom_reap_own_process_group "build-gate"
  fi
}
# Armed BEFORE the slot acquire below, preserving the pre-#6192 ordering
# invariant (`trap loom_build_slot_release EXIT` came first): a gate that dies
# between the acquire and the first stage must still release its slot. The
# #6192 self-reap is folded into the SAME handler rather than a second `trap
# ... EXIT` — bash keeps only one EXIT trap, so a second `trap` would silently
# replace the slot release and leak a machine-wide slot on every run.
trap _build_gate_exit_cleanup EXIT

if [[ "$_build_slot_available" == "true" ]]; then
  loom_build_slot_acquire "build-gate(${LOOM_BUILD_GATE_TIER:-full})"
fi

# Generous, configurable per-step budget (issue #6192 suggests 30-60min — real
# release builds are slow; the point is bounding *forever*, not policing
# slowness). Default 1800s (30min). LOOM_BUILD_GATE_STEP_TIMEOUT_SECS=0
# disables per-step bounding entirely (falls back to plain unbounded execution
# — the pre-#6192 behavior).
_gate_step_timeout="${LOOM_BUILD_GATE_STEP_TIMEOUT_SECS:-1800}"

# Best-effort: arm the per-issue dispatch backoff (#4485) when a step timed
# out, so this issue's next dispatch is deferred instead of racing a fresh
# retry against a still-wedged host. Never fails the gate — a missing
# LOOM_SWEEP_CLAIM_OWNED (not a daemon-dispatched sweep), a missing daemon
# binary, or an unreachable daemon socket are all silently skipped.
_arm_dispatch_backoff_on_timeout() {
  local step_desc="$1" elapsed="$2"
  # Only a daemon-dispatched sweep has an issue to back off; a manual gate run
  # (or the daemon's own main-health gate) has no claim and is skipped.
  if [[ -z "${LOOM_SWEEP_CLAIM_OWNED:-}" ]]; then
    return 0
  fi
  local _locate_lib
  _locate_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/locate-daemon-bin.sh"
  if [[ ! -f "$_locate_lib" ]]; then
    return 0
  fi
  # shellcheck source=lib/locate-daemon-bin.sh
  source "$_locate_lib"
  local _daemon_bin
  _daemon_bin="$(loom_locate_daemon_bin "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" 2>/dev/null || true)"
  if [[ -z "$_daemon_bin" || ! -x "$_daemon_bin" ]]; then
    return 0
  fi
  echo "[build-gate] arming dispatch backoff for issue #${LOOM_SWEEP_CLAIM_OWNED} (#4485/#6192)" >&2
  "$_daemon_bin" dispatch-backoff record \
    "$LOOM_SWEEP_CLAIM_OWNED" \
    --reason "build-gate timeout: ${step_desc} (${elapsed}s elapsed, budget ${_gate_step_timeout}s)" \
    >/dev/null 2>&1 || true
  return 0
}

# Run one gate stage under the per-step budget. On a genuine timeout, fails
# LOUDLY with a distinct message naming the hung command and its measured
# elapsed time (rather than silently falling through to a generic non-zero
# exit that looks identical to an ordinary test failure), arms the dispatch
# backoff, then exits 124 — distinguishable from an ordinary build/test
# failure in logs even though both are gate failures to the caller. Any
# OTHER non-zero exit propagates via `set -e` exactly as before.
run_gate_step() {
  local step_desc="$1"; shift
  if [[ "$_bounded_run_available" != "true" || "$_gate_step_timeout" == "0" ]]; then
    "$@"
    return $?
  fi
  local start_ts elapsed rc=0
  start_ts=$(date +%s)
  # `|| rc=$?` is load-bearing under `set -e`: without it, a non-zero exit
  # from `bounded_run` (including the timeout case, 124) would abort THIS
  # script immediately at this line — before the 124-vs-genuine-failure check
  # below ever runs — exactly the plain `cargo test ...` behavior this
  # function exists to add a distinct timeout path on top of.
  bounded_run "$_gate_step_timeout" "$@" || rc=$?
  elapsed=$(( $(date +%s) - start_ts ))
  if [[ "$rc" -eq 124 ]]; then
    echo "[build-gate] TIMEOUT after ${elapsed}s (budget ${_gate_step_timeout}s) — hung command: $* (#6192)" >&2
    echo "[build-gate] stage '${step_desc}' did not finish in time; killing it rather than retrying alongside a still-hung process." >&2
    # `|| true` so a surprising non-zero from the best-effort backoff arm can
    # never replace the distinct 124 the caller is meant to see (`set -e`
    # would otherwise exit 1 here, one line before `exit 124`).
    _arm_dispatch_backoff_on_timeout "$step_desc" "$elapsed" || true
    exit 124
  fi
  return "$rc"
}

cd "$(git rev-parse --show-toplevel)"

# Tiered gate mode (#4259). LOOM_BUILD_GATE_TIER selects the stage set:
#
#   - unset / "full" (the DEFAULT): the full three-stage suite below. CI parity,
#     manual invocations, and the per-builder post-builder quality gate are all
#     byte-for-byte unchanged when the variable is absent.
#   - "fast": a cheap, bounded compile+smoke subset. The daemon's main-health
#     gate selects this tier (by setting LOOM_BUILD_GATE_TIER=fast) when the host
#     is saturated past the max-defer bound and the full suite would otherwise
#     time out under concurrent-sweep contention (the #4020/#4084 recurrence).
#
# A fast-tier GREEN is NOT equivalent to a full-suite GREEN: it verifies only the
# compile/startup breakage class (#3647 step-8 — a `cargo build --workspace` catch)
# plus a daemon-binary startup smoke, NOT the Rust unit tests or the installer
# suite. See .loom/docs/build-gate.md "Tiered gate (#4259)".
_gate_tier="${LOOM_BUILD_GATE_TIER:-full}"
if [[ "${_gate_tier}" == "fast" ]]; then
  echo "[build-gate] FAST tier (compile + smoke only — NOT a full-suite verdict, #4259)"
  echo "[build-gate] cargo build --workspace --lib --bins (compile check — catches #3647 step-8-class breakage)"
  run_gate_step "cargo build --workspace --lib --bins" cargo build --workspace --lib --bins
  # Startup smoke. This slot used to hold `cd loom-tools && uv run python -c
  # "import loom_tools"` — a Python-importability check that became a hard
  # failure the moment epic #4081 Phase 4 (#4557) deleted the package. The
  # like-for-like replacement is running the binary the gate just built: it
  # catches the same "compiles but won't start" class (a panic in a static
  # initializer, a broken clap command tree, a missing dynamic dependency) with
  # no Python toolchain in the picture. `--version` is chosen deliberately: it
  # touches no repo state, no forge, and no daemon socket.
  echo "[build-gate] loom-daemon startup smoke (cargo run -- --version)"
  run_gate_step "cargo run --package loom-daemon -- --version" \
    cargo run --quiet --package loom-daemon --bin loom-daemon -- --version
  echo "[build-gate] fast tier passed (compile + startup smoke)"
  exit 0
fi

# Rust unit tests — PREFER `cargo nextest run`, the runner CI uses (#8326).
#
# `cargo test` executes every test in a binary in ONE shared process on shared
# threads. That is exactly the execution mode `.config/nextest.toml` and
# loom-daemon's crate-level "Test isolation convention" (#4385) exist to avoid:
# the daemon's unit tests mutate the process environment (`std::env::set_var` /
# `remove_var`) at ~774 call sites while other tests in the same binary
# `Command::spawn` children, and `Command::spawn` reads the environ
# non-atomically — so an env mutation racing a spawn can corrupt a child's
# environment mid-flight. nextest runs each test in its own process, closing
# that window by construction.
#
# Until #8326 this gate ran plain `cargo test` while CI ran nextest, which made
# the LOCAL gate both slower and less reliable than the runner it is supposed to
# predict — the origin of #8170's false red (8 `sweep_registry::watchdog` "failures"
# that were 90/90 green under nextest on the same commit). Every false red the
# gate produces trains agents to distrust a real signal, so the gate now runs the
# same runner and the same `--profile ci` as .github/workflows/ci.yml.
#
# `nextest-daemon-guard.sh` (#6528) is deliberately NOT used here: it guards the
# host-global tmux sweep performed by the `integration_*` test TARGETS, and
# `--lib --bins` never builds or runs those targets at all (#3985).
#
# The fallback degrades LOUDLY, never silently: a `cargo test` verdict from this
# gate is a weaker signal than CI's, and whoever triages a red needs to know that
# BEFORE they spend an investigation on a failure that only reproduces here.
# Written as one `printf` and one `||` rather than an if/else block of `echo`s
# purely to stay inside the `shell-budget --check` portable ratchet: this file is
# `contract` in scripts/shell-allowlist.txt, so its code-line count may shrink
# but never grow (see .loom/docs/shell-language-policy.md).
_rust_unit_cmd=(cargo nextest run --workspace --lib --bins --profile ci)
command -v cargo-nextest >/dev/null 2>&1 || { _rust_unit_cmd=(cargo test --workspace --lib --bins); printf '[build-gate] WARNING: cargo-nextest is NOT installed -- falling back to cargo test, which shares ONE process\n[build-gate] WARNING: across every test in a binary, so env mutation can race Command::spawn and produce flaky FALSE\n[build-gate] WARNING: REDS that do not reproduce under the runner CI uses (#4385; see .config/nextest.toml, and\n[build-gate] WARNING: #8170 for an investigation spent on exactly such a false red).\n[build-gate] WARNING: Install the runner CI uses for a trustworthy gate:  cargo install cargo-nextest --locked\n' >&2; }
echo "[build-gate] ${_rust_unit_cmd[*]} (workspace unit tests; host-dependent integration targets are CI-only, #3985)"
run_gate_step "${_rust_unit_cmd[*]}" "${_rust_unit_cmd[@]}"

# Doctests are their OWN step because nextest does not run them (#4385) — CI
# keeps a separate "Run doctests" step for exactly this reason. Unconditional:
# identical on both runner branches, so doctest coverage never depends on which
# unit-test runner a host happens to have.
echo "[build-gate] cargo test --workspace --doc (doctests -- nextest does not run these, #4385)"
run_gate_step "cargo test --workspace --doc" cargo test --workspace --doc

# The bash suites, in order. A loop rather than five echo/run_gate_step pairs
# for the same portable-ratchet reason noted above — the stage set and its order
# are unchanged, and each step is still its own bounded `run_gate_step`. The one
# behavioural difference is cosmetic: the announced label is now the script path
# ("bash scripts/test-installer.sh"), which names the thing a reader would go
# run, rather than a prose description ("bash installer suite").
for _bash_suite in test-installer test-changelog test-daemon-liveness test-install-local-mode test-migrate-consumer; do
  echo "[build-gate] bash scripts/${_bash_suite}.sh"
  run_gate_step "bash scripts/${_bash_suite}.sh" bash "scripts/${_bash_suite}.sh"
done

echo "[build-gate] all stages passed"
