#!/usr/bin/env bash
# spawn-claude.sh - Token-rotating launcher for Claude Code.
#
# This script is a thin layer that:
#   1. Selects a Claude Code OAuth token from .loom/tokens/ via the native
#      `loom-daemon tokens select` CLI (issue #4228, epic #4081 Phase 2 — the
#      historical interpreted-language selector call has been retired from
#      this path entirely; see loom-daemon/src/tokens_pool/select.rs).
#   2. Exports CLAUDE_CODE_OAUTH_TOKEN.
#   3. exec's the underlying CLI (`claude` by default, or
#      `claude-wrapper.sh` if --use-wrapper is passed for retry behavior).
#
# It does NOT replace the existing 1700-LOC `.loom/scripts/claude-wrapper.sh`,
# which provides retry, backoff, auth-cache, and error classification.
# Use `claude-wrapper.sh` directly when you need that behavior; use this
# script when you want pure token rotation in front of either `claude` or
# the wrapper.
#
# Behavior on missing tokens:
#   Token selection resolves the effective pool (issue #3938): the per-repo
#   pool at `<repo>/.loom/tokens/` when it holds `*.token` files, else the
#   shared machine-level pool at `~/.loom/tokens/` (override
#   `LOOM_SHARED_TOKENS_DIR`; set it empty to disable the fallback). This lets a
#   consumer repo the daemon dispatches into — which has no pool of its own —
#   spawn against the shared pool instead of hard-failing. All pool STATE
#   (`.bad_tokens`/`.ranking`/`.allowlist`/`.failure_counts`) lives in whichever
#   pool was selected, so it is never forked per repo.
#   When NEITHER pool exists/has tokens (or all tokens are bad), this script
#   exits 78 (EX_CONFIG) with a message instructing the user to run
#   `loom-daemon tokens bootstrap` (or `--shared` for the machine-level pool).
#   It does NOT silently fall back to keychain.
#   (The recovery advice named the Python `loom-tokens` console script until epic
#   #4081 Phase 4, #4557, deleted the package that provided it; `loom-daemon
#   tokens` is the only implementation now.)
#
# Worktree handling:
#   When invoked from a git worktree, the script resolves the canonical repo
#   root via `git rev-parse --git-common-dir` and looks up `.loom/tokens/`
#   there — never in the worktree's path.
#
# Env vars (pool location):
#   LOOM_SHARED_TOKENS_DIR  Shared machine-level pool location. Non-empty path
#                           overrides the `~/.loom/tokens` default; an empty
#                           value disables the shared fallback (per-repo only).
#
# Usage:
#   .loom/scripts/spawn-claude.sh -p "your prompt"
#   .loom/scripts/spawn-claude.sh --use-wrapper --prompt "..." --log /tmp/log
#
# Env vars:
#   LOOM_WORKSPACE         Override repo root detection.
#   LOOM_SPAWN_NO_EXPORT   If set, skip selection (caller already exported a
#                          token). Useful for testing the dispatch path.
#   LOOM_DAEMON_BIN        Override the `loom-daemon` binary used for native
#                          token selection (see lib/locate-daemon-bin.sh for
#                          the full resolution order: this env var ->
#                          `loom-daemon` on PATH -> build-output-relative
#                          candidates under the repo).
#   LOOM_MODEL             Model to pass as `claude --model <value>` (issue
#                          #3477). Lowest-priority tier: an explicit `--model`
#                          in the passthrough args always wins. When neither
#                          is set, NO --model flag is emitted and the session/
#                          CLI default is preserved.
#   LOOM_EFFORT            Reasoning-effort level to pass as `claude --effort
#                          <value>` (issue #3705). Mirrors LOOM_MODEL: an
#                          explicit `--effort` in the passthrough args wins;
#                          when neither is set, NO --effort flag is emitted and
#                          the session/CLI default is preserved. This sets a
#                          session-default effort for the whole spawned child —
#                          the sweep escalation ladder's per-rung `@effort`
#                          cannot be threaded here because the in-session Task
#                          tool exposes no effort parameter (see sweep.md).
#   LOOM_SWEEP_NICENESS    Scheduling niceness applied to this process (and
#                          everything it execs/forks — claude, claude-wrapper,
#                          cargo, rustc, test binaries) via a `nice -n N exec`
#                          re-exec, mirroring build-gate.sh's niceness pattern
#                          (issue #4233). Precedence: this env var ->
#                          `autonomous.spawnNiceness` config -> default `10`.
#   LOOM_SWEEP_NICE=0      Disables the ENTIRE priority mechanism (nice AND
#                          taskpolicy below), restoring pre-#4233 behavior.
#   LOOM_SWEEP_TASKPOLICY_CLASS  Optional macOS `taskpolicy -c <class>`
#                          scheduling class applied in-place (e.g. "utility").
#                          Precedence: this env var ->
#                          `autonomous.spawnTaskpolicyClass` config -> unset
#                          (off by default). No-op on non-macOS or when
#                          `taskpolicy` is unavailable.
#   LOOM_SWEEP_CPU_QUOTA   Master toggle for the per-sweep CPU-quota
#                          enforcement (issue #5111). `0` disables the whole
#                          mechanism (no budget export, no quota wrap),
#                          restoring pre-#5111 behavior. Default: enabled.
#   LOOM_SWEEP_RESERVED_CORES  Cores subtracted off the host's logical core
#                          count before handing the remainder to the sweep as
#                          its CPU budget — mirrors the daemon's own
#                          `min(16, cpu_cores - 2)` agent-concurrency rule.
#                          Precedence: this env var ->
#                          `autonomous.spawnReservedCores` config -> default
#                          `2`. The resulting budget is always >= 1 core.
#   LOOM_SWEEP_SHARED_CPU_BUDGET  Toggle for the HOST-WIDE division of that
#                          budget across concurrently-running sweeps (issue
#                          #5979). `0` keeps the pre-#5979 behavior where
#                          every sweep independently claims `total -
#                          reserved`. Precedence: this env var ->
#                          `autonomous.spawnSharedCpuBudget` config ->
#                          default enabled.
#   LOOM_SWEEP_INFLIGHT_SWEEPS  Override the concurrent-sweep divisor instead
#                          of asking the local daemon for it. A test hook,
#                          and the escape hatch for a host whose concurrent
#                          agents this daemon does not track.
#   LOOM_SWEEP_INFLIGHT_PROBE_TIMEOUT_SECS  Hard bound on that daemon probe
#                          (default `10`). On timeout the divisor falls back
#                          to `1`, i.e. exactly pre-#5979 behavior.
#   LOOM_SWEEP_CPU_BUDGET_CORES  OUTPUT, not an input: exported into the
#                          spawned session with the computed per-sweep core
#                          budget so agent-written drivers (e.g. a SPICE
#                          batch harness) can read "how many cores may I run
#                          concurrently" from their own environment. Set on
#                          every platform, whether or not the quota below is
#                          actually kernel-enforced.
#   LOOM_SWEEP_WALLCLOCK_CEILING_SECS  Optional hard wall-clock ceiling on
#                          the ENTIRE spawned session (not each leaf process)
#                          — enforced via the systemd scope's
#                          `RuntimeMaxSec=` when a quota wrap applies.
#                          Precedence: this env var ->
#                          `autonomous.spawnWallClockCeilingSecs` config ->
#                          default `0` (disabled). Off by default for the
#                          same reason `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`
#                          below is forced to 0: a healthy sweep can
#                          legitimately run for hours, and this ceiling has
#                          no notion of "still making progress" — it is a
#                          blunt backstop for a genuinely runaway/orphaned
#                          batch, meant to be opted into per-repo (e.g. a
#                          sim-heavy repo bounding its whole driver, not just
#                          each leaf's own `timeout`), not a default that
#                          could kill a legitimate long build.
#
# CPU-quota enforcement mechanism (issue #5111 — nothing bounded a sweep's
# CPU, so an agent-written driver ran 8 concurrent `ngspice` processes and
# saturated an entire 8-core host with no overall wall-clock/CPU limit).
# When a Linux host with a reachable `systemd --user` manager is detected
# (see lib/systemd-user.sh), the final `claude`/`claude-wrapper.sh` exec is
# wrapped in `systemd-run --user --scope -p CPUQuota=<budget*100>%`, an
# ACTUAL kernel cgroup quota: the whole process tree the sweep spawns (this
# script's own descendants, however many leaf processes it forks) can never
# collectively exceed the budget, regardless of process count — an agent
# that ignores the published `LOOM_SWEEP_CPU_BUDGET_CORES` budget is still
# contained. Killing the scope (e.g. #5110's orphan reaping) reaps every
# process inside it in one shot. On a host with no systemd --user manager
# (this fleet's macOS hosts), there is no cgroup-equivalent primitive
# available without extra tooling, so this degrades to advisory-only: the
# budget is still exported, but nothing kernel-side enforces it there yet
# (tracked as a follow-up rather than attempted in this same change).
#
# Host-wide budget sharing (issue #5979 — three concurrent sweeps, each
# correctly computing "6 of this 8-core host's cores are mine", summed to 18
# and drove the host to load 133.87). The budget above is now DIVIDED by the
# number of sweeps in flight on the host, read from `loom-daemon status
# --json` at spawn time (see lib/cpu-budget.sh's loom_cpu_inflight_sweeps).
#
# SPAWN-TIME SNAPSHOT, deliberately — not live tracking. The divisor is
# sampled ONCE, here, and never revised for the life of the sweep. A sweep
# therefore keeps a share sized for the sibling count at its own start: if
# siblings finish early it leaves headroom unclaimed, and if siblings start
# later they divide only what the newer snapshot shows. The alternative —
# continuously re-deriving each sweep's share as the host's population
# changes — would require re-applying an already-installed systemd
# `CPUQuota` to a running scope and re-publishing
# LOOM_SWEEP_CPU_BUDGET_CORES into an already-started agent process, neither
# of which any mechanism in Loom does today. The snapshot is strictly
# conservative in the direction that matters: it can only ever leave a host
# UNDER-subscribed, never over. Live re-balancing is a separate, larger
# change and is explicitly out of scope here.

set -euo pipefail

# --- Logging helpers (match loom convention) ---
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WARN${NC} $*" >&2; }
log_error() { echo -e "${RED}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ERROR${NC} $*" >&2; }

# --- Repo root resolution (handles worktrees) ---
# If LOOM_WORKSPACE is set, trust it. Otherwise:
#   1. Try `git rev-parse --git-common-dir` to find the canonical .git dir
#      (works inside main checkouts and worktrees alike).
#   2. The parent of `.git` (or of the common dir if it's not literally .git)
#      is the canonical repo root.
#   3. Fallback: walk up from the script's directory.
_resolve_workspace() {
    if [[ -n "${LOOM_WORKSPACE:-}" ]]; then
        printf '%s\n' "$LOOM_WORKSPACE"
        return
    fi

    local git_common_dir
    if git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)"; then
        # `git rev-parse --git-common-dir` may return a relative path inside
        # a worktree — convert to absolute, then take parent.
        if [[ ! "$git_common_dir" = /* ]]; then
            git_common_dir="$(cd "$git_common_dir" && pwd)"
        fi
        # If common-dir basename is `.git`, parent is repo root.
        # Otherwise (linked worktree case), it's the literal main `.git/`
        # directory — its parent is still the canonical main checkout.
        printf '%s\n' "$(dirname "$git_common_dir")"
        return
    fi

    # Fallback: relative to this script
    cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

WORKSPACE="$(_resolve_workspace)"

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Sweep/role-runner spawn scheduling priority (issue #4233) ---
#
# Issue #4231 (host meltdown 2026-07-27→28: 6-way sweep fan-out, load 118/28
# cores, WindowServer watchdog-killed 5×, loom-daemon crashed) diagnosed
# STARVATION, not raw load, as the actual failure mode: sweep worker processes
# — and every build they spawn (cargo, rustc, test binaries) — run at default
# niceness (0), so under fan-out they compete head-to-head with WindowServer,
# VNC, and other system daemons for CPU.
#
# #3985/#4020/#4073/#4084 (see build-gate.sh:32-82) already ran this
# experiment from the OTHER side — nicing the build GATE up so it could never
# starve sweeps. #4020 found the extreme gate=19 starved the gate itself into
# UNEVALUATED and settled on gate=5 (a mild, measured, unprivileged-achievable
# gap above sweeps' nice-0 default); its comment (build-gate.sh:63-70)
# explicitly flagged the untried alternative — nicing sweep children UP in the
# spawn path — as deliberately not done at the time. This block does that, so
# the gap between gate and sweep niceness is now real in both directions.
#
# Applied here — the outermost point in the daemon's actual dispatch path.
# `loom-daemon` invokes THIS script directly for both sweep dispatch
# (SweepRegistryConfig::resolve_spawn_bin in
# loom-daemon/src/sweep_registry.rs) and scheduled role-runner dispatch
# (role_runner.rs) — both unattended/background paths; MOM's interactive
# terminals run `claude` directly and never go through this script, so no
# separate "interactive opt-out" is needed. spawn-worker.sh (a not-yet-wired
# future multi-runtime seam, see its own header) execs into this script for
# the "claude" runtime and is therefore covered transparently; it applies no
# priority adjustment of its own so there is no double-apply risk.
#
# Mechanism: a `nice -n N exec "$0" "$@"` re-exec — mirroring build-gate.sh's
# `LOOM_BUILD_GATE_NICED` sentinel pattern — applied before ANY child process
# (token selection, `claude`, `claude-wrapper.sh`). Because `exec` preserves
# the PID and nice/taskpolicy attributes survive exec, the whole subsequent
# process tree (this script → claude/claude-wrapper → any cargo/rustc it
# forks) inherits the adjustment with no separate handling required
# downstream. The build gate has its own independent niceness policy
# (build-gate.sh) and is never invoked through this script, so there is no
# overlap with it either.
#
# Precedence (env > config > default), the tier order used throughout loom:
#   LOOM_SWEEP_NICENESS         > autonomous.spawnNiceness         > 10
#   LOOM_SWEEP_TASKPOLICY_CLASS > autonomous.spawnTaskpolicyClass  > (unset)
#
# LOOM_SWEEP_NICE=0 disables the ENTIRE mechanism (nice AND taskpolicy),
# restoring byte-identical pre-#4233 behavior (nice 0, no taskpolicy class).
#
# Default is nice ONLY, at a moderate value (10) — deliberately above the
# gate's 5, so sweeps now yield to the gate under contention, addressing the
# #4084 CPU-contention premise as a structural side effect. `taskpolicy` is
# opt-in and unset by default: on macOS, `-c utility` is the recommended class
# if enabled — deliberately NOT `-b`/background (DARWIN_BG), which also
# throttles disk I/O and network aggressively enough to risk *causing* the
# very timeouts this issue is trying to prevent.
if [[ -z "${LOOM_SWEEP_NICED:-}" && "${LOOM_SWEEP_NICE:-1}" != "0" ]]; then
    _sweep_niceness="${LOOM_SWEEP_NICENESS:-}"
    _sweep_taskpolicy_class="${LOOM_SWEEP_TASKPOLICY_CLASS:-}"
    _sweep_config_lib="${_script_dir}/lib/config-resolver.sh"
    if [[ ( -z "$_sweep_niceness" || -z "$_sweep_taskpolicy_class" ) \
          && -f "$_sweep_config_lib" ]]; then
        # shellcheck source=./lib/config-resolver.sh
        source "$_sweep_config_lib"
        [[ -z "$_sweep_niceness" ]] \
            && _sweep_niceness="$(loom_config_get "$WORKSPACE" "autonomous.spawnNiceness" "")"
        [[ -z "$_sweep_taskpolicy_class" ]] \
            && _sweep_taskpolicy_class="$(loom_config_get "$WORKSPACE" "autonomous.spawnTaskpolicyClass" "")"
    fi
    : "${_sweep_niceness:=10}"

    # taskpolicy sets the scheduling class on THIS running process (no
    # re-exec needed) and, like nice, survives the exec below since exec
    # preserves the PID. Best-effort: a failure here never blocks the spawn.
    if [[ -n "$_sweep_taskpolicy_class" ]] && command -v taskpolicy >/dev/null 2>&1; then
        if taskpolicy -c "$_sweep_taskpolicy_class" -p $$ >/dev/null 2>&1; then
            log_info "spawn-claude: applied taskpolicy -c $_sweep_taskpolicy_class (issue #4233)"
        else
            log_warn "spawn-claude: taskpolicy -c $_sweep_taskpolicy_class failed (non-fatal, continuing at default policy class)"
        fi
    fi

    if [[ "$_sweep_niceness" != "0" ]] && command -v nice >/dev/null 2>&1; then
        export LOOM_SWEEP_NICED=1
        log_info "spawn-claude: re-exec at nice -n $_sweep_niceness (issue #4233; LOOM_SWEEP_NICE=0 to disable)"
        exec nice -n "$_sweep_niceness" "$0" "$@"
    fi
fi

# --- Per-sweep CPU quota (issue #5111) ---
#
# See the header comment block above for the full rationale and env var
# reference. Unlike the niceness block above, this does NOT re-exec itself —
# it computes a budget and (on a host that can enforce it) builds a
# `systemd-run --user --scope ...` prefix array (CPU_QUOTA_WRAP) that the
# Dispatch section below prepends to the FINAL `claude`/`claude-wrapper.sh`
# exec. Wrapping only the terminal exec — rather than re-invoking this whole
# script under a wrapper, the way the niceness block re-execs itself — avoids
# having to explicitly re-plumb every already-exported env var (the OAuth
# token, LOOM_MODEL/LOOM_EFFORT flags, the safehouse MCP injection, etc.)
# through a `systemd-run` invocation that does not automatically inherit an
# arbitrary parent shell environment the way a plain `exec` does.
#
# Detachment side effect (issue #6129): `systemd-run --user --scope` creates
# its scope as a transient unit owned by the `systemd --user` manager, not by
# this process or by `loom-daemon` — the wrapped `claude`/`claude-wrapper.sh`
# ends up with the user manager as its PPID, not this script or the daemon.
# That means it survives `systemctl --user stop loom-daemon.service` (there is
# no cgroup/parent relationship for that stop job to reach) exactly like a
# launchd child survives a daemon exit, just via a different mechanism. This
# is not itself new behavior for THIS script (spawned children were always
# meant to outlive an ordinary daemon stop — see daemon_service.rs's "survive,
# don't drain" policy comment) — #6129's finding is that operators had no way
# to DELIBERATELY reap them as a group when they actually want to. See the
# `--unit=`/`--slice=` naming below (predictable enumeration) and
# `loom-daemon-quiesce.sh` (the explicit, single-command way to stop dispatch
# AND every in-flight role/sweep child on both platforms).
CPU_QUOTA_WRAP=()
if [[ "${LOOM_SWEEP_CPU_QUOTA:-1}" != "0" ]]; then
    # shellcheck source=./lib/cpu-budget.sh
    source "${_script_dir}/lib/cpu-budget.sh"

    _cpu_reserved="${LOOM_SWEEP_RESERVED_CORES:-}"
    _cpu_wallclock="${LOOM_SWEEP_WALLCLOCK_CEILING_SECS:-}"
    _cpu_shared="${LOOM_SWEEP_SHARED_CPU_BUDGET:-}"
    _cpu_config_lib="${_script_dir}/lib/config-resolver.sh"
    if [[ ( -z "$_cpu_reserved" || -z "$_cpu_wallclock" || -z "$_cpu_shared" ) \
          && -f "$_cpu_config_lib" ]]; then
        # shellcheck source=./lib/config-resolver.sh
        source "$_cpu_config_lib"
        [[ -z "$_cpu_reserved" ]] \
            && _cpu_reserved="$(loom_config_get "$WORKSPACE" "autonomous.spawnReservedCores" "")"
        [[ -z "$_cpu_wallclock" ]] \
            && _cpu_wallclock="$(loom_config_get "$WORKSPACE" "autonomous.spawnWallClockCeilingSecs" "")"
        [[ -z "$_cpu_shared" ]] \
            && _cpu_shared="$(loom_config_get "$WORKSPACE" "autonomous.spawnSharedCpuBudget" "")"
    fi
    [[ "$_cpu_reserved" =~ ^[0-9]+$ ]] || _cpu_reserved=2
    [[ "$_cpu_wallclock" =~ ^[0-9]+$ ]] || _cpu_wallclock=0
    # Enabled unless explicitly switched off (`0` / `false`), matching
    # LOOM_SWEEP_CPU_QUOTA's own default-on shape.
    [[ "$_cpu_shared" =~ ^(0|false|no)$ ]] && _cpu_shared=0 || _cpu_shared=1

    _cpu_total_cores="$(loom_cpu_total_cores)"

    # Issue #5979: how many sweeps are running on THIS HOST right now,
    # including the one being spawned. `1` is both the no-daemon fail-safe
    # and the honest answer for a solo sweep, so the divided budget collapses
    # to the pre-#5979 `total - reserved` in exactly those cases.
    _cpu_inflight=1
    if [[ "$_cpu_shared" == "1" ]]; then
        _cpu_inflight="$(loom_cpu_inflight_sweeps "$WORKSPACE")"
        [[ "$_cpu_inflight" =~ ^[0-9]+$ ]] && ((_cpu_inflight >= 1)) || _cpu_inflight=1
    fi

    _cpu_budget_cores="$(loom_cpu_budget_cores "$_cpu_total_cores" "$_cpu_reserved" "$_cpu_inflight")"
    _cpu_quota_pct=$((_cpu_budget_cores * 100))

    # Published parallelism budget (Suggested-direction #2 in #5111): exported
    # unconditionally, on every platform, so an agent writing a driver script
    # (e.g. a SPICE batch harness) can read how many cores it may use even
    # where the quota below is not kernel-enforced. Since #5979 the number is
    # this sweep's SHARE of the host, not the whole host — a harness that
    # already reads it gets host-wide coordination for free, with no change
    # on its side.
    export LOOM_SWEEP_CPU_BUDGET_CORES="$_cpu_budget_cores"
    if [[ "$_cpu_shared" == "1" ]]; then
        log_info "spawn-claude: CPU budget = ${_cpu_budget_cores} core(s) of ${_cpu_total_cores} (reserved ${_cpu_reserved}) divided across ${_cpu_inflight} in-flight sweep(s) on this host — exported as LOOM_SWEEP_CPU_BUDGET_CORES (issues #5111/#5979)"
    else
        log_info "spawn-claude: CPU budget = ${_cpu_budget_cores} core(s) of ${_cpu_total_cores} (reserved ${_cpu_reserved}) — host-wide sharing disabled (LOOM_SWEEP_SHARED_CPU_BUDGET=0, #5979) — exported as LOOM_SWEEP_CPU_BUDGET_CORES (issue #5111)"
    fi

    _systemd_user_lib="${_script_dir}/lib/systemd-user.sh"
    if [[ -f "$_systemd_user_lib" ]]; then
        # shellcheck source=./lib/systemd-user.sh
        source "$_systemd_user_lib"
    fi

    if command -v is_linux_systemd >/dev/null 2>&1 && is_linux_systemd; then
        _cpu_quota_props=(-p "CPUQuota=${_cpu_quota_pct}%")
        if [[ "$_cpu_wallclock" != "0" ]]; then
            _cpu_quota_props+=(-p "RuntimeMaxSec=${_cpu_wallclock}")
        fi
        # Predictable unit naming + a dedicated slice (issue #6129): a
        # `systemd-run --user --scope` invocation with no --unit= gets an
        # opaque, systemd-generated name (`run-r<32-hex>.scope`, exactly the
        # shape #6129's incident had to `grep` command lines to find), and
        # with no --slice= it lands wherever the default policy places it. An
        # operator draining a host — or this repo's own quiesce tooling
        # (loom-daemon-quiesce.sh, #6129) — needs to enumerate every one of
        # these scopes AS A GROUP without matching on `claude-wrapper.sh -p
        # /loom:` argv text (fragile: breaks the moment a role/prompt name
        # changes). `loom-agent-<pid>-<rand>.scope` is unique per spawn (pid
        # is THIS spawn-claude.sh invocation's own pid, stable across the
        # niceness re-exec above since exec preserves it; the random suffix
        # guards a pid reused across two spawns in the same tick) and always
        # starts with the greppable `loom-agent-` prefix; `loom-agents.slice`
        # groups every one of them under one cgroup subtree for accounting
        # and `systemctl --user list-units 'loom-agent-*.scope'` enumeration.
        # A separate random suffix on the PROBE unit's own name (below) keeps
        # it from ever colliding with the real unit this spawn will create,
        # regardless of how quickly systemd garbage-collects the probe scope.
        _scope_slice="loom-agents.slice"
        _scope_unit="loom-agent-$$-${RANDOM}${RANDOM}.scope"
        _scope_probe_unit="loom-agent-probe-$$-${RANDOM}${RANDOM}.scope"
        _scope_props=(--slice="$_scope_slice")
        # Probe with a trivial `true` invocation first: a real scope create +
        # teardown, so a host where the properties above are rejected (e.g.
        # the "cpu" controller isn't delegated to the user manager) is
        # detected here rather than replacing this process with a failing
        # `systemd-run` at the real exec below. Best-effort: probe failure
        # never blocks the spawn, it just leaves CPU_QUOTA_WRAP empty.
        #
        # THREE-WAY outcome, not two (issue #6129): the naming/slice props are
        # a #6129 ergonomics addition layered on top of #5111's *functional*
        # quota enforcement, so a host that rejects `--unit=`/`--slice=` (an
        # old systemd-run, a slice the user manager refuses to create, a
        # policy that forbids naming transient units) must NOT silently cost
        # this host its CPU quota — that would be a #5111 regression traded
        # for an enumeration convenience. So a failed naming probe re-probes
        # the pre-#6129 unnamed form before giving up: quota preserved,
        # enumerability degraded to `loom-daemon-quiesce.sh`'s cross-platform
        # process-pattern step (which was always the launchd mechanism and
        # reaches an unnamed scope's processes just as well).
        if systemd-run --user --scope --quiet --unit="$_scope_probe_unit" "${_scope_props[@]}" "${_cpu_quota_props[@]}" -- true >/dev/null 2>&1; then
            CPU_QUOTA_WRAP=(systemd-run --user --scope --quiet --unit="$_scope_unit" "${_scope_props[@]}" "${_cpu_quota_props[@]}" --)
            _cpu_quota_msg="spawn-claude: enforcing CPUQuota=${_cpu_quota_pct}% via systemd --user scope unit=${_scope_unit} slice=${_scope_slice} (issue #5111, naming/slice #6129)"
            [[ "$_cpu_wallclock" != "0" ]] && _cpu_quota_msg+=", RuntimeMaxSec=${_cpu_wallclock}s"
            log_info "$_cpu_quota_msg"
        elif systemd-run --user --scope --quiet "${_cpu_quota_props[@]}" -- true >/dev/null 2>&1; then
            CPU_QUOTA_WRAP=(systemd-run --user --scope --quiet "${_cpu_quota_props[@]}" --)
            _cpu_quota_msg="spawn-claude: enforcing CPUQuota=${_cpu_quota_pct}% via an UNNAMED systemd --user scope — this host rejected --unit=/--slice= (issue #5111 quota preserved; #6129 scope enumeration unavailable, use loom-daemon-quiesce.sh's process scan to drain)"
            [[ "$_cpu_wallclock" != "0" ]] && _cpu_quota_msg+=", RuntimeMaxSec=${_cpu_wallclock}s"
            log_warn "$_cpu_quota_msg"
        else
            log_warn "spawn-claude: systemd-run --user --scope probe failed; CPU quota is NOT kernel-enforced this spawn (LOOM_SWEEP_CPU_BUDGET_CORES=${_cpu_budget_cores} remains advisory-only, issue #5111)"
        fi
    else
        log_warn "spawn-claude: no reachable systemd --user manager on this host; CPU quota is advisory-only here (LOOM_SWEEP_CPU_BUDGET_CORES=${_cpu_budget_cores}, issue #5111)"
    fi
else
    log_info "spawn-claude: CPU quota mechanism disabled (LOOM_SWEEP_CPU_QUOTA=0, issue #5111)"
fi

# --- Host-sleep prevention (issue #6311) ---
#
# #3350's check-host-sleep.sh is advisory-only by design — it warns but never
# mutates anything, so an operator re-applies the mitigation by hand on every
# host, every run. Repo-level opt-in (`host.preventSleep` in
# `.loom/config.json`, env override `LOOM_HOST_PREVENT_SLEEP`, standard
# env > config > default-OFF precedence — see lib/host-sleep-config.sh)
# self-wraps THIS spawn in `systemd-inhibit --what=idle:sleep`, exactly the
# manual remediation check-host-sleep.sh has always recommended by hand.
#
# This is the single dispatch chokepoint for BOTH named Linux entry points in
# #6311 ("sweep" and "role runner") — `loom-daemon` invokes this script
# directly for sweep dispatch (SweepRegistryConfig::resolve_spawn_bin) AND
# scheduled role-runner dispatch (role_runner.rs); MOM's interactive
# terminals run `claude` directly and never go through this script, so an
# interactive session still needs its own manual wrap (see
# check-host-sleep.sh's "Note:" addition for that case).
#
# Linux-only — systemd-inhibit talks to systemd-logind, which needs no
# privilege. macOS's reliable defense (`sudo pmset -c sleep 0`) is privileged
# and global; a repo-level config flag must NEVER invoke `sudo` silently
# (#6311's own explicit macOS guardrail), so this mechanism is a deliberate
# no-op there — check-host-sleep.sh still surfaces the manual macOS
# remediation (or, once `host.sleepMitigationAcknowledged` is set, a
# downgraded one-liner naming it).
#
# `idle:sleep` locks are HOST-WIDE, not scoped to this process's own
# children — visible via `systemd-inhibit --list` for the lifetime of this
# spawn (the #6311 acceptance criterion), and as a side effect keep every
# sibling process on the host (including a currently-idle `loom-daemon`)
# awake too, for as long as at least one sweep/role-runner spawn is in
# flight.
#
# Prefix-array composition mirrors CPU_QUOTA_WRAP immediately above: a
# `--why=probe -- true` dry run first, so a host that rejects it (no
# reachable logind, e.g. a container with no dbus) degrades to "spawn
# without the wrap" rather than replacing this process with a failing
# `systemd-inhibit` at the real exec below. Never blocks the spawn.
SLEEP_INHIBIT_WRAP=()
_sleep_inhibit_config_lib="${_script_dir}/lib/host-sleep-config.sh"
if [[ -f "$_sleep_inhibit_config_lib" ]]; then
    # shellcheck source=./lib/host-sleep-config.sh
    source "$_sleep_inhibit_config_lib"
    if declare -F loom_host_prevent_sleep_enabled >/dev/null 2>&1 \
        && [[ "$(loom_host_prevent_sleep_enabled "$WORKSPACE")" == "1" ]]; then
        if command -v systemd-inhibit >/dev/null 2>&1; then
            _sleep_inhibit_why="${LOOM_ROLE:-loom}"
            if systemd-inhibit --what=idle:sleep --who=loom --why=probe -- true >/dev/null 2>&1; then
                SLEEP_INHIBIT_WRAP=(systemd-inhibit --what=idle:sleep --who=loom --why="$_sleep_inhibit_why" --)
                log_info "spawn-claude: host.preventSleep is enabled — wrapping this spawn in systemd-inhibit --what=idle:sleep --why=${_sleep_inhibit_why} (issue #6311)"
            else
                log_warn "spawn-claude: host.preventSleep is enabled but a systemd-inhibit probe failed (no reachable systemd-logind?); spawning WITHOUT the sleep-inhibit wrap (issue #6311)"
            fi
        else
            log_warn "spawn-claude: host.preventSleep is enabled but systemd-inhibit is not on PATH (macOS, or non-systemd Linux); spawning WITHOUT the sleep-inhibit wrap — see check-host-sleep.sh for the manual remediation (issue #6311)"
        fi
    fi
fi

# --- Containerized dispatch mode (issue #7429, epic #6896 Phase 3) ---
#
# Config-selectable, initially OFF: `.loom/config.json` ->
# `runtimes.containment.enabled` (env override `LOOM_SWEEP_CONTAINERIZED`,
# standard env > config > default-off precedence). When enabled, this script
# re-execs ITSELF inside a `docker run` of the configured image (default
# ghcr.io/rjwalters/loom-worker:latest — override with
# `LOOM_SWEEP_CONTAINER_IMAGE` or `runtimes.containment.image`), under the
# path-parity mount contract (`docker/worker/MOUNT-CONTRACT.md`): the
# resolved `$WORKSPACE` (repo root — covers the main checkout AND every
# `.loom/worktrees/*` beneath it) is bind-mounted read-write at the
# IDENTICAL absolute host path, never remapped, so a sweep running in a git
# worktree builds and commits with no repo copy — git's absolute-path
# worktree pointers (`.git` gitdir files, `commondir`, object-store refs)
# resolve identically inside and outside the container (MOUNT-CONTRACT.md
# §1's "load-bearing" rule).
#
# `LOOM_SPAWN_CONTAINERIZED=1` is the recursion guard: once re-exec'd inside
# the container, this same block sees it already set and falls straight
# through to the rest of this script unchanged. Every later section — CPU
# quota (degrades to advisory-only, same as any host with no reachable
# `systemd --user` manager), sleep-inhibit, and token selection — runs
# AGAIN, this time *inside* the container, against the mount contract's
# parity-mounted paths (the token pool read-only, the build cache
# read-write) instead of being duplicated here. This is deliberate: it is
# the existing token-rotation logic in THIS script that the mount contract's
# secrets section (§2) says a worker container must use, not a
# reimplementation of it. Niceness is the one exception: `LOOM_SWEEP_NICED`
# (already exported on the host side, before this block runs) is forwarded
# through the generic `LOOM_*` env passthrough below, so the recursed
# invocation's own niceness re-exec sees its sentinel already set and
# correctly skips re-niceing a process that is, from the HOST kernel's
# perspective, still the same niced process tree.
#
# Restart-safety (issue #5119, ADR-0017 Decision 4, "The #5119 drain
# interaction, specified"): a per-sweep container is its own cgroup, owned
# by the container runtime (dockerd), not by loom-daemon's systemd unit or
# by this script's own process tree. A hard-killed daemon (a plain
# stop/restart on systemd, or any other non-drained teardown of the chain
# that dispatched this script) SIGKILLs the local `docker run` CLIENT below
# exactly like every other exec in this script — but killing that client
# does NOT stop the container it started: `docker run` needs no host-side
# supervising process to keep a container alive after dockerd has it. The
# in-flight sweep survives as an orphan relative to the daemon's in-memory
# registry — the SAME "sweeps survive by design" property launchd already
# gives bare-metal sweeps today (see daemon-reference.md), now extended to
# systemd too, via the container boundary instead of process reparenting.
# `loom-daemon restart --drain` remains the recommended path on both
# supervisors — it waits for in-flight sweeps (containerized or not) to
# finish before the daemon exits, so the orphan case above is a hard-stop
# fallback, not the common path. Reconciling a still-running orphaned
# container after a daemon restart (`SweepRegistry::reconstruct`'s
# container-recognition extension) and teaching `cancel_sweep` to `docker
# stop`/`docker rm` a containerized sweep it explicitly cancels are real,
# named Phase 3 obligations ADR-0017 defers past this issue's own scope note
# ("only add the dispatch mode itself") — tracked as a follow-up rather than
# silently assumed done.
#
# Build-cache placement (MOUNT-CONTRACT.md §4, issue #6013/#6014): a
# container that mounts no build-cache path gets a fresh, empty `target/`
# trapped in its own ephemeral writable layer on every `docker run` — worse
# than a redirected-but-persistent host cache, because then EVERY sweep pays
# a full rebuild. `CARGO_TARGET_DIR` is therefore resolved the SAME way the
# rest of the repo already does (`lib/cargo-target-dir.sh`: env ->
# `cargo metadata` -> `<workspace>/target`), then — only when it resolves
# OUTSIDE the already-mounted workspace — mounted at its own identical
# absolute path (parity applies to caches too) and exported into the
# container, so a containerized sweep shares the exact per-repo-per-host
# cache `post-worktree.sh`'s binary-reuse fast path already assumes, never a
# path that lives only inside the container's own filesystem.
#
# Per-sweep resource LIMITS (CPU/mem ceilings on the container itself) are
# explicitly OUT of scope here — a separate, later Phase 3 issue per #7429's
# own scope note ("Per-sweep resource limits ... are separate, LATER issues
# in this phase"). This mode ships with no `--cpus`/`--memory` docker flags.
# The host CPU-quota mechanism above (issue #5111, `CPU_QUOTA_WRAP`) is
# deliberately NOT applied to the `docker run` client below even when
# computed: wrapping the trivial client process in a systemd scope would not
# constrain the container's own (separate) cgroup, so it would be
# decorative, not real containment.
CONTAINMENT_ENABLED="0"
_containment_config_lib="${_script_dir}/lib/config-resolver.sh"
if [[ -z "${LOOM_SPAWN_CONTAINERIZED:-}" ]]; then
    _containment_enabled="${LOOM_SWEEP_CONTAINERIZED:-}"
    if [[ -z "$_containment_enabled" && -f "$_containment_config_lib" ]]; then
        # shellcheck source=./lib/config-resolver.sh
        source "$_containment_config_lib"
        _containment_enabled="$(loom_config_get "$WORKSPACE" "runtimes.containment.enabled" "")"
    fi
    case "$_containment_enabled" in
        1 | true | yes) CONTAINMENT_ENABLED="1" ;;
        *) CONTAINMENT_ENABLED="0" ;;
    esac
fi

if [[ "$CONTAINMENT_ENABLED" == "1" ]]; then
    if ! command -v docker >/dev/null 2>&1; then
        log_error "spawn-claude: containerized dispatch is enabled (runtimes.containment.enabled / LOOM_SWEEP_CONTAINERIZED) but 'docker' is not on PATH."
        log_error "Install docker, or disable containment (LOOM_SWEEP_CONTAINERIZED=0, or runtimes.containment.enabled=false in .loom/config.json)."
        exit 78 # EX_CONFIG
    fi

    _containment_image="${LOOM_SWEEP_CONTAINER_IMAGE:-}"
    if [[ -z "$_containment_image" && -f "$_containment_config_lib" ]]; then
        # shellcheck source=./lib/config-resolver.sh
        source "$_containment_config_lib"
        _containment_image="$(loom_config_get "$WORKSPACE" "runtimes.containment.image" "")"
    fi
    : "${_containment_image:=ghcr.io/rjwalters/loom-worker:latest}"

    _containment_cwd="$(pwd -P)"
    _containment_mounts=(-v "${WORKSPACE}:${WORKSPACE}")

    # --- Secrets mounts (MOUNT-CONTRACT.md §2) ---
    # The per-repo token pool ($WORKSPACE/.loom/tokens) is already covered by
    # the workspace mount above. The shared machine-level pool (the
    # spawn-claude.sh fallback documented at the top of this file) lives
    # outside $WORKSPACE and needs its own read-only parity mount.
    _containment_shared_tokens="${LOOM_SHARED_TOKENS_DIR:-${HOME:-}/.loom/tokens}"
    if [[ -n "$_containment_shared_tokens" && -d "$_containment_shared_tokens" \
        && "$_containment_shared_tokens" != "${WORKSPACE}"/* ]]; then
        _containment_mounts+=(-v "${_containment_shared_tokens}:${_containment_shared_tokens}:ro")
    fi
    # gh/git forge auth (docker/worker/README.md "Bootstrap seams"): prefer
    # the env-var form already in this process's environment (passed through
    # by the LOOM_*/CLAUDE_*/GH_*/GITHUB_* sweep below); best-effort read-only
    # bind of ~/.config/gh only when neither token env var is present.
    if [[ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" && -n "${HOME:-}" && -d "${HOME}/.config/gh" ]]; then
        _containment_mounts+=(-v "${HOME}/.config/gh:${HOME}/.config/gh:ro")
    fi
    # Git commit identity (check-git-identity.sh's global user.name/user.email).
    if [[ -n "${HOME:-}" && -f "${HOME}/.gitconfig" ]]; then
        _containment_mounts+=(-v "${HOME}/.gitconfig:${HOME}/.gitconfig:ro")
    fi

    # --- Build-cache placement (MOUNT-CONTRACT.md §4, issue #6013/#6014) ---
    _containment_env=()
    _containment_cargo_lib="${_script_dir}/lib/cargo-target-dir.sh"
    if [[ -f "${WORKSPACE}/Cargo.toml" && -f "$_containment_cargo_lib" ]]; then
        # shellcheck source=./lib/cargo-target-dir.sh
        source "$_containment_cargo_lib"
        _containment_target_dir="$(loom_resolve_cargo_target_dir "$WORKSPACE")"
        if [[ -n "$_containment_target_dir" ]]; then
            if [[ "$_containment_target_dir" != "${WORKSPACE}"/* ]]; then
                mkdir -p "$_containment_target_dir" 2>/dev/null || true
                _containment_mounts+=(-v "${_containment_target_dir}:${_containment_target_dir}")
            fi
            _containment_env+=(-e "CARGO_TARGET_DIR=${_containment_target_dir}")
        fi
    fi

    # --- Env passthrough ---
    # Every LOOM_*/CLAUDE_*/SAFEHOUSE*/CODEX_*/GH_TOKEN/GITHUB_TOKEN var
    # already present in THIS process's environment (exported by the daemon
    # before it spawned this script — LOOM_SWEEP_CLAIM_OWNED, LOOM_ROLE,
    # LOOM_RUNTIME, LOOM_MODEL/LOOM_EFFORT when set, etc. — or already
    # resolved above, e.g. CLAUDE_CODE_OAUTH_TOKEN when
    # LOOM_SPAWN_NO_EXPORT bypassed selection) is forwarded by NAME (`-e
    # VAR`, no `=value`) so docker reads the CURRENT value straight from this
    # shell — generic and exhaustive rather than a hardcoded list that drifts
    # from what the daemon actually sets.
    while IFS='=' read -r _containment_var _; do
        case "$_containment_var" in
            LOOM_* | CLAUDE_* | SAFEHOUSE* | CODEX_* | GH_TOKEN | GITHUB_TOKEN)
                _containment_env+=(-e "$_containment_var")
                ;;
        esac
    done < <(env)
    _containment_env+=(-e "LOOM_SPAWN_CONTAINERIZED=1" -e "LOOM_WORKSPACE=${WORKSPACE}" -e "HOME=${HOME:-/home/loom}")

    _containment_labels=(--label "loom.sweep=1" --label "loom.dispatch=container")
    [[ -n "${LOOM_SWEEP_CLAIM_OWNED:-}" ]] && _containment_labels+=(--label "loom.sweep.issue=${LOOM_SWEEP_CLAIM_OWNED}")

    log_info "spawn-claude: containerized dispatch ENABLED (issue #7429) — image=${_containment_image}, workspace=${WORKSPACE} (parity-mounted), cwd=${_containment_cwd}"
    echo "# LOOM_CONTAINMENT_ENABLED image=${_containment_image}" >&2

    exec ${SLEEP_INHIBIT_WRAP[@]+"${SLEEP_INHIBIT_WRAP[@]}"} docker run --rm \
        "${_containment_mounts[@]}" \
        -w "$_containment_cwd" \
        "${_containment_env[@]}" \
        "${_containment_labels[@]}" \
        "$_containment_image" \
        "${WORKSPACE}/.loom/scripts/spawn-claude.sh" "$@"
fi

# --- Locate the loom-daemon binary (token selection, issue #4228) ---
# Resolution precedence (see lib/locate-daemon-bin.sh): $LOOM_DAEMON_BIN ->
# `loom-daemon` on PATH -> build-output-relative candidates under $WORKSPACE.
# shellcheck source=lib/locate-daemon-bin.sh
source "${_script_dir}/lib/locate-daemon-bin.sh"

# --- Daemon self-claim marker visibility (Issue #3823, observability #3967) ---
# `loom-daemon`'s `SweepRegistry::spawn_child` exports
# `LOOM_SWEEP_CLAIM_OWNED=<issue>` into a daemon-dispatched child so its
# `/loom:sweep` pre-flight recognises the daemon's own pre-dispatch
# `loom:issue -> loom:building` label flip as ITS OWN claim rather than
# self-skipping the issue as "another worker is in flight" (the #3967
# incident). Log the marker's presence/value here — unconditionally, on
# every spawn, whether daemon-dispatched or not — so a future occurrence is
# diagnosable straight from `.loom/logs/sweep-issue-<N>.log` without needing
# to reproduce the dispatch: this line, plus the `spawn-claude:` prefix, is
# already captured by `spawn_child`'s per-sweep log redirection, so any gap
# between the Rust `Command::env()` call and what this script's own
# environment actually sees is now directly observable instead of inferred.
log_info "spawn-claude: LOOM_SWEEP_CLAIM_OWNED=${LOOM_SWEEP_CLAIM_OWNED:-unset}"

# --- Argument parsing ---
USE_WRAPPER=false
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --use-wrapper)
            USE_WRAPPER=true
            shift
            ;;
        --help|-h)
            sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' \
                | head -n -1
            exit 0
            ;;
        --)
            shift
            PASSTHROUGH_ARGS+=("$@")
            break
            ;;
        *)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
    esac
done

# --- Model selection (issue #3477, Phase 1; observability #3482, Phase 3a) ---
# Precedence: explicit `--model` in the passthrough args > LOOM_MODEL env >
# nothing (session/CLI default — no --model flag emitted at all).
#
# Observability (#3482): exactly ONE structured `spawn-claude: model=<value>`
# line is emitted on every spawn, covering all three precedence cases —
# including `model=default` when nothing is configured. The line is
# stderr-only and changes NO spawn behavior; downstream log scrapers key on
# the `model=` token.
_explicit_model=""
_has_model_arg=false
_prev_was_model_flag=false
for _arg in ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}; do
    if [[ "$_prev_was_model_flag" == "true" ]]; then
        _explicit_model="$_arg"
        _prev_was_model_flag=false
        continue
    fi
    case "$_arg" in
        --model)
            _has_model_arg=true
            _prev_was_model_flag=true
            ;;
        --model=*)
            _has_model_arg=true
            _explicit_model="${_arg#--model=}"
            ;;
    esac
done

if [[ "$_has_model_arg" == "true" ]]; then
    if [[ -n "${LOOM_MODEL:-}" ]]; then
        log_info "spawn-claude: explicit --model in args wins over LOOM_MODEL='$LOOM_MODEL'"
    fi
    log_info "spawn-claude: model=${_explicit_model:-default} (from --model arg)"
elif [[ -n "${LOOM_MODEL:-}" ]]; then
    PASSTHROUGH_ARGS+=(--model "$LOOM_MODEL")
    log_info "spawn-claude: model=$LOOM_MODEL (from LOOM_MODEL)"
else
    log_info "spawn-claude: model=default"
fi

# --- Effort selection (issue #3705) ---
# Mirrors the model block above for the `claude --effort <level>` session knob.
# Precedence: explicit `--effort` in the passthrough args > LOOM_EFFORT env >
# nothing (session/CLI default — no --effort flag emitted at all).
#
# This is the ONLY per-call reasoning-effort surface available in this
# environment: the `claude` CLI exposes `--effort`, but the in-session Task
# tool (the sweep's per-role subagent dispatch, one level deep) exposes NO
# effort parameter. So spawn-claude/daemon can set a *session-default* effort
# for a whole `/loom:sweep` child; the escalation ladder's per-rung `@effort`
# still degrades to the bare model at Task-tool dispatch time (see
# `sweep.md` → "Effort graceful degradation").
#
# Observability: exactly ONE structured `spawn-claude: effort=<value>` line
# is emitted only when an effort is actually resolved (explicit arg or env).
# When nothing is configured, NO effort line is emitted and NO --effort flag
# is appended — byte-for-byte identical to pre-#3705 behavior.
_explicit_effort=""
_has_effort_arg=false
_prev_was_effort_flag=false
for _arg in ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}; do
    if [[ "$_prev_was_effort_flag" == "true" ]]; then
        _explicit_effort="$_arg"
        _prev_was_effort_flag=false
        continue
    fi
    case "$_arg" in
        --effort)
            _has_effort_arg=true
            _prev_was_effort_flag=true
            ;;
        --effort=*)
            _has_effort_arg=true
            _explicit_effort="${_arg#--effort=}"
            ;;
    esac
done

if [[ "$_has_effort_arg" == "true" ]]; then
    if [[ -n "${LOOM_EFFORT:-}" ]]; then
        log_info "spawn-claude: explicit --effort in args wins over LOOM_EFFORT='$LOOM_EFFORT'"
    fi
    log_info "spawn-claude: effort=${_explicit_effort:-default} (from --effort arg)"
elif [[ -n "${LOOM_EFFORT:-}" ]]; then
    PASSTHROUGH_ARGS+=(--effort "$LOOM_EFFORT")
    log_info "spawn-claude: effort=$LOOM_EFFORT (from LOOM_EFFORT)"
fi

# --- Account provider resolution (issue #5609, design D8) ---
# Reads THIS runtime's own manifest (defaults/runtimes/claude.json)'s
# "accountProvider" field so the pool `tokens select --provider` dispatches
# into is a property of the runtime manifest, not a value hardcoded in this
# script -- an operator can locally repoint it by editing
# .loom/runtimes/claude.json without touching this script. Resolution mirrors
# check-runtime-capabilities.sh's precedence: an on-disk
# <repo>/.loom/runtimes/<name>.json wins over the defaults/runtimes/ fallback
# next to this script. A missing file, missing field, or missing `jq` all
# fall open to "claude" (never fail closed) -- design D8's "a missing
# accountProvider on an un-resynced install must default to claude".
_loom_account_provider_for_runtime() {
    local runtime_name="$1" manifest=""
    if [[ -f "${WORKSPACE}/.loom/runtimes/${runtime_name}.json" ]]; then
        manifest="${WORKSPACE}/.loom/runtimes/${runtime_name}.json"
    elif [[ -f "${_script_dir}/../runtimes/${runtime_name}.json" ]]; then
        manifest="${_script_dir}/../runtimes/${runtime_name}.json"
    fi
    if [[ -z "$manifest" ]] || ! command -v jq >/dev/null 2>&1; then
        printf '%s\n' "claude"
        return
    fi
    local provider
    provider="$(jq -r '.accountProvider // "claude"' "$manifest" 2>/dev/null)"
    case "$provider" in
        "" | null) provider="claude" ;;
    esac
    printf '%s\n' "$provider"
}

# --- Token selection ---
if [[ -z "${LOOM_SPAWN_NO_EXPORT:-}" && -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    _daemon_bin="$(loom_locate_daemon_bin "$WORKSPACE")"
    if [[ -z "$_daemon_bin" ]] || ! "$_daemon_bin" tokens select --help >/dev/null 2>&1; then
        log_error "No loom-daemon binary supporting 'tokens select' was found."
        log_error "(\$LOOM_DAEMON_BIN -> 'loom-daemon' on PATH -> build-output-relative"
        log_error "candidates under the repo all came up empty or stale.)"
        log_error "Build or start one, then retry:"
        log_error "  ./.loom/scripts/cli/loom-daemon-start.sh"
        log_error "  cargo build --release -p loom-daemon"
        exit 78  # EX_CONFIG
    fi

    # --- Binary provenance at the selection site (issue #4643) ---
    # The binary that decides token selection is resolved HERE, independently
    # of any running daemon ($LOOM_DAEMON_BIN -> PATH -> build-output
    # candidates), so a long-running daemon can hand a sweep child a binary
    # older than the last merged selection fix. That staleness was the leading
    # hypothesis for the 2026-07-30 incident (13h-old exhaustion entries
    # appearing to block every spawn); refuting it took out-of-band forensics
    # on the host's installed binaries, because NOTHING in the sweep log
    # recorded which binary ran. Log path + version on every spawn so the next
    # occurrence is answerable straight from `.loom/logs/sweep-issue-<N>.log`.
    # Best-effort: a binary too old to support `--version` still logs its path.
    _daemon_version="$("$_daemon_bin" --version 2>/dev/null | head -1)"
    log_info "spawn-claude: token-selection binary: $_daemon_bin (${_daemon_version:-version unknown})"

    _account_provider="$(_loom_account_provider_for_runtime claude)"
    _select_args=(tokens select --workspace "$WORKSPACE" --provider "$_account_provider" --export)
    # Pre-flight: auto-unpin if every allowlisted account has hit the
    # consecutive-failure threshold (default 5). Without this, an
    # operator-set pin can trap the spawner once all pinned accounts
    # exhaust their weekly quota. Empty-pool guard: we never silently
    # clear .bad_tokens — if that file blocks every account, the user
    # must intervene (e.g. `loom-daemon tokens unblock <name>`). Capability-probed
    # (issue #4228) so a daemon binary mid-roll that predates `--auto-unpin`
    # degrades to plain selection instead of a hard argument-parse failure.
    if "$_daemon_bin" tokens select --help 2>&1 | grep -q -- '--auto-unpin'; then
        _select_args+=(--auto-unpin)
    fi

    # Capture stdout (shell-evalable export lines) and stderr (errors /
    # advisories, e.g. a firing "[auto-unpin] ..." line) separately so log
    # output never contaminates what we're about to `eval`.
    _selection_stderr_file="$(mktemp)"
    _selection_output=""
    if ! _selection_output="$("$_daemon_bin" "${_select_args[@]}" 2>"$_selection_stderr_file")"; then
        log_error "Token selection failed:"
        cat "$_selection_stderr_file" >&2 || true
        rm -f "$_selection_stderr_file"
        log_error "Run '$_daemon_bin tokens bootstrap' to populate <repo>/.loom/tokens/,"
        log_error "or add '--shared' for the machine-level pool"
        log_error "(~/.loom/tokens, override LOOM_SHARED_TOKENS_DIR) that consumer"
        log_error "repos fall back to. Use '$_daemon_bin tokens unblock <name>' if"
        log_error ".bad_tokens is the cause."
        log_error "Spawn-claude refuses to auto-clear .bad_tokens — that's"
        log_error "intentional: an empty pool indicates a real auth problem."
        log_error "Set CLAUDE_CODE_OAUTH_TOKEN explicitly to bypass selection."
        exit 78  # EX_CONFIG
    fi
    # Surface any advisory stderr (e.g. the auto-unpin line) without treating
    # it as a failure — mirrors the historical Python pre-flight's `|| true`.
    cat "$_selection_stderr_file" >&2 || true
    rm -f "$_selection_stderr_file"

    # `--export` emits `export CLAUDE_CODE_OAUTH_TOKEN=...` and
    # `export LOOM_TOKEN_NAME=...` (both consumed by downstream processes,
    # including a claude-wrapper.sh invoked via --use-wrapper) plus a local
    # (non-exported) `LOOM_TOKEN_MODE=...` used only for the log line below.
    # Tokens are base64/hex-like and never contain a single quote in
    # practice — see `loom-daemon tokens select`'s own doc comment.
    eval "$_selection_output"

    if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
        log_error "Token selection returned an empty key for account '${LOOM_TOKEN_NAME:-unknown}'."
        exit 78
    fi

    log_info "spawn-claude: using OAuth account '${LOOM_TOKEN_NAME:-unknown}' (mode=${LOOM_TOKEN_MODE:-unknown})"
    echo "# LOOM_ACCOUNT name=${LOOM_TOKEN_NAME:-unknown}" >&2
    unset LOOM_TOKEN_MODE
fi

# --- Print-mode background-task wait ceiling (issue #3943) ---
# When the daemon spawns a sweep child as a headless `claude -p "/loom:sweep N"`
# session, the Claude Code harness (print mode) terminates still-running
# background tasks — the sweep's dispatched Builder/Judge subagents — after a
# 600s ceiling and exits the session. Any role phase taking >10 minutes is
# killed mid-build, causing loom:building<->loom:issue label ping-pong. Disable
# the ceiling (0 = no cap) so long-running phases run to completion. This is
# harmless for interactive use (no background-task reaping there). Use the
# `:=` default-assignment idiom so an operator override — e.g. a non-zero
# ceiling exported in the environment — is preserved.
: "${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:=0}"
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS

# --- Headless-session marker for the Stop guard (issue #6645) ---
# `guard-background-subagents.sh` blocks a stop that would orphan a background
# child. That block is correct in headless `-p` mode (ending the turn kills the
# process) and a pure false positive in an interactive session (children
# survive the turn boundary), so the guard must be able to tell the two apart.
# Its primary signal is the owning `claude` process's own argv, but this export
# is the defense-in-depth belt: this script is the SOLE sanctioned headless
# dispatch path in Loom, so a marker set here classifies every Loom-dispatched
# sweep correctly even if the harness's argv shape changes.
#
# Set ONLY when print mode is actually requested — an interactive `claude`
# launched through this script (no `-p`/`--print` in the passthrough args) must
# NOT be marked headless, or it inherits exactly the friction #6645 removes.
# Env vars set before `exec` are inherited by the replacing process image, and
# the harness passes its own environment down to hook subprocesses.
_loom_print_mode=false
for _arg in ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}; do
    case "$_arg" in
        -p | --print | --print=*) _loom_print_mode=true; break ;;
    esac
done
if [[ "$_loom_print_mode" == "true" ]]; then
    export LOOM_HEADLESS_SESSION=1
fi
unset _loom_print_mode

# --- Optional safehouse MCP server injection (issue #3999) ---
# When the `safehouse` config block is enabled and a socket + launch command
# resolve, inject a session-scoped MCP config that adds the `safehouse` stdio
# server alongside `loom`, with a PER-WORKER persona so two concurrent workers
# in the same workspace get distinct `SAFEHOUSE_PERSONA` values. The persona is
# drawn from a bounded, operator-pre-registered pool (`safehouse.workerPersonas`)
# round-robined by this worker's issue slot (`LOOM_SWEEP_CLAIM_OWNED`), because
# safehoused's allowlist is a static boot-time TOML array with no runtime/prefix
# registration — literal per-issue names cannot be minted at dispatch time.
#
# Delivery shape: a session-scoped `--mcp-config <file>` (not a rewrite of the
# workspace `.mcp.json`). Concurrent sweeps SHARE the workspace root, so a
# per-worker persona cannot live in the shared file; a session-scoped config is
# the only correct per-worker surface. The file lists `loom` FIRST so it is
# self-contained even if the session's cwd has no project `.mcp.json`.
#
# Degradation contract (mirrors #3997): disabled/absent ⇒ byte-for-byte no-op
# (no --mcp-config appended). Enabled but the launch command is missing, or no
# socket resolves ⇒ log a warning and SKIP injection — the `loom` MCP server is
# never affected and the worker always starts. Only the socket path (no token or
# key) is ever written into the injected config.
_mcp_config_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/mcp-config.sh"
if [[ -f "$_mcp_config_lib" ]]; then
    # shellcheck source=./lib/mcp-config.sh
    source "$_mcp_config_lib"

    # Respect an explicit operator-supplied --mcp-config (do not clobber).
    _has_mcp_config_arg=false
    for _arg in ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}; do
        case "$_arg" in
            --mcp-config | --mcp-config=*) _has_mcp_config_arg=true; break ;;
        esac
    done

    if [[ "$_has_mcp_config_arg" == "false" \
        && "$(loom_mcp_safehouse_enabled "$WORKSPACE")" == "true" ]]; then
        _sh_socket="$(loom_mcp_safehouse_socket "$WORKSPACE")"
        _sh_command="$(loom_mcp_safehouse_command "$WORKSPACE")"
        _sh_pool="$(loom_mcp_worker_personas "$WORKSPACE")"
        _sh_fallback="$(loom_mcp_safehouse_persona_fallback "$WORKSPACE")"
        _sh_persona="$(loom_mcp_pick_persona \
            "${LOOM_SWEEP_CLAIM_OWNED:-}" "$_sh_pool" "$_sh_fallback")"

        # Export persona + socket into the SESSION process environment — not
        # only into the injected MCP config file's server env — so Bash-tool
        # helpers can reach safehoused. Lifecycle role subagents (loom-builder /
        # loom-judge / loom-doctor) pin their tool allowlists to Read/Glob/Grep/
        # Bash(/Write/Edit) with no MCP tools, so the injected `safehouse_send`
        # tool is invisible to them; `fleet-send.sh` (Bash) is their path to the
        # room and it resolves the socket/persona from these env vars (#4199).
        # Only the socket path is exported (no token or key). Guard on a
        # resolvable socket — with none there is nothing to connect to, and this
        # is independent of whether the safehouse-mcp launch command is present.
        if [[ -n "$_sh_socket" ]]; then
            export SAFEHOUSE_PERSONA="$_sh_persona"
            export SAFEHOUSED_SOCKET="$_sh_socket"
        fi

        if [[ -z "$_sh_socket" ]]; then
            # Loud on purpose (issue #5523): before this, the message named
            # only the mechanism ("skipping safehouse MCP injection"), buried
            # in a per-role sweep log nobody was tailing — every sweep on an
            # affected host silently stopped narrating to safehouse for 11
            # hours, unnoticed, because the ONLY consequence anyone could see
            # from outside was the public fleet pulse going
            # stale. Name that consequence here, and point at the standalone,
            # on-demand check (independent of a daemon restart) that answers
            # "is this drifted?" without reading any sweep log.
            log_warn "spawn-claude: SAFEHOUSE DRIFT — safehouse.enabled is true but no socket resolves (safehouse.socket / \$LOOM_SAFEHOUSE_SOCKET / \$SAFEHOUSED_SOCKET); no safehouse narration will be recorded for this sweep — the public fleet pulse is fed exclusively from safehouse narration, so this host's activity will not appear there until the socket resolves. Run '.loom/scripts/check-safehouse-socket.sh' to check every managed repo on this host without reading sweep logs. Skipping safehouse MCP injection (loom MCP unaffected)."
        elif ! command -v "$_sh_command" >/dev/null 2>&1; then
            log_warn "spawn-claude: safehouse launch command '$_sh_command' not found in PATH; skipping safehouse MCP injection (loom MCP unaffected)."
        else
            if [[ ! -S "$_sh_socket" && ! -e "$_sh_socket" ]]; then
                log_warn "spawn-claude: safehouse socket '$_sh_socket' not present at spawn; injecting anyway (best-effort — safehouse-mcp connects lazily and never blocks the worker)."
            fi
            _mcp_session_config="$(mktemp -t loom-mcp-config.XXXXXX 2>/dev/null || mktemp)"
            if loom_mcp_emit_config "$WORKSPACE" "$_sh_socket" "$_sh_persona" "$_sh_command" >"$_mcp_session_config" 2>/dev/null; then
                PASSTHROUGH_ARGS+=(--mcp-config "$_mcp_session_config")
                log_info "spawn-claude: injected safehouse MCP server (persona=$_sh_persona, socket=$_sh_socket, config=$_mcp_session_config)"
            else
                rm -f "$_mcp_session_config"
                log_warn "spawn-claude: failed to generate safehouse MCP config; skipping injection (loom MCP unaffected)."
            fi
        fi
    fi
fi

# --- Dispatch ---
# SLEEP_INHIBIT_WRAP (issue #6311) then CPU_QUOTA_WRAP (issue #5111) are
# prepended, in that order, to whichever final command is exec'd below —
# each empty (no-op) unless its own block above found a usable mechanism.
# Order composes correctly because both wraps end their own array with a
# trailing `--`: `systemd-inhibit ... -- systemd-run ... -- claude ...`. The
# `+"${arr[@]}"` guard on each is required under `set -u`: bash 3.2 (macOS's
# shipped default) treats `"${arr[@]}"` on a truly empty array as an
# unbound-variable error without it.
#
# Process-group self-reap scope note (Issue #6192): both branches below end
# in a plain `exec`, which REPLACES this process's image — nothing is left
# alive afterward to trap the leaf command's exit and reap any children it
# leaves behind, so `lib/reap-process-group.sh`'s self-reap cannot be wired in
# here without restructuring `exec` into a fork+wait, a materially riskier
# change to this script's signal/tty semantics than this issue's scope
# justifies. This is not a gap in practice for the primary daemon-dispatch
# path: `sweep_registry::dispatch` appends `--use-wrapper` by default (see
# `dispatch_appends_use_wrapper_flag`), so daemon-dispatched sweeps take the
# `USE_WRAPPER` branch into `claude-wrapper.sh`, which already runs `claude`
# as a managed child (never exec-replaces itself) and carries the self-reap
# trap directly (see its own header comment). Only a manual/interactive
# invocation that explicitly omits `--use-wrapper` reaches the raw `exec`
# below without that backstop.
if [[ "$USE_WRAPPER" == "true" ]]; then
    _wrapper="${WORKSPACE}/.loom/scripts/claude-wrapper.sh"
    if [[ ! -x "$_wrapper" ]]; then
        log_error "Cannot find executable claude-wrapper.sh at $_wrapper"
        exit 1
    fi
    exec ${SLEEP_INHIBIT_WRAP[@]+"${SLEEP_INHIBIT_WRAP[@]}"} ${CPU_QUOTA_WRAP[@]+"${CPU_QUOTA_WRAP[@]}"} "$_wrapper" "${PASSTHROUGH_ARGS[@]}"
fi

# Default: exec the `claude` CLI directly.
if ! command -v claude >/dev/null 2>&1; then
    log_error "'claude' command not found in PATH."
    log_error "Install Claude Code or pass --use-wrapper to invoke claude-wrapper.sh."
    exit 127
fi
echo "# LOOM_CLI_START runtime=claude" >&2
exec ${SLEEP_INHIBIT_WRAP[@]+"${SLEEP_INHIBIT_WRAP[@]}"} ${CPU_QUOTA_WRAP[@]+"${CPU_QUOTA_WRAP[@]}"} claude "${PASSTHROUGH_ARGS[@]}"
