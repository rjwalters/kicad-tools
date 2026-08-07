#!/usr/bin/env bash
# build-gate.sh - buildGate.command for this repo: fast build+test backstop
# across Rust and bash. Runs in the worktree; exits non-zero on the first
# failing stage (set -e) so buildGate.command's single exit code is
# meaningful. See .loom/docs/build-gate.md.
#
# Scope decisions (issue #3749):
#   - cargo test covers the Rust crates (loom-daemon, loom-api). As of #3985
#     the Rust step is scoped to `--lib --bins` (crate unit tests only) and
#     deliberately EXCLUDES the integration test TARGETS under
#     loom-daemon/tests/ (integration_basic.rs et al). Those spin up a real
#     tmux server and are therefore host-dependent — green only where a live
#     tmux is reachable. The local gate runs on the very host that is actively
#     running Loom (a busy, sometimes tmux-less machine), so a host-dependent
#     assertion there measures the host, not `main`. CI (.github/workflows/
#     ci.yml) controls its environment and still runs the FULL
#     `cargo test --workspace`, so the integration targets are covered exactly
#     where their environment is guaranteed. See "Local gate vs. CI" in
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
if [[ -f "$_build_slot_lib" ]]; then
  # shellcheck source=lib/build-slot.sh
  source "$_build_slot_lib"
  trap loom_build_slot_release EXIT
  loom_build_slot_acquire "build-gate(${LOOM_BUILD_GATE_TIER:-full})"
else
  echo "[build-gate] note: lib/build-slot.sh not found — running unserialized (#4512)" >&2
fi

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
  cargo build --workspace --lib --bins
  # Startup smoke. This slot used to hold `cd loom-tools && uv run python -c
  # "import loom_tools"` — a Python-importability check that became a hard
  # failure the moment epic #4081 Phase 4 (#4557) deleted the package. The
  # like-for-like replacement is running the binary the gate just built: it
  # catches the same "compiles but won't start" class (a panic in a static
  # initializer, a broken clap command tree, a missing dynamic dependency) with
  # no Python toolchain in the picture. `--version` is chosen deliberately: it
  # touches no repo state, no forge, and no daemon socket.
  echo "[build-gate] loom-daemon startup smoke (cargo run -- --version)"
  cargo run --quiet --package loom-daemon --bin loom-daemon -- --version
  echo "[build-gate] fast tier passed (compile + startup smoke)"
  exit 0
fi

echo "[build-gate] cargo test --lib --bins (workspace unit tests; host-dependent integration targets are CI-only, #3985)"
cargo test --workspace --lib --bins

echo "[build-gate] bash installer suite"
bash scripts/test-installer.sh

echo "[build-gate] bash changelog generator suite"
bash scripts/test-changelog.sh

echo "[build-gate] bash daemon-liveness pgrep suite (#5548)"
bash scripts/test-daemon-liveness.sh

echo "[build-gate] bash install --local/--gitignore mode suite"
bash scripts/test-install-local-mode.sh

echo "[build-gate] bash migrate-consumer suite"
bash scripts/test-migrate-consumer.sh

echo "[build-gate] all stages passed"
