#!/usr/bin/env bash
# cpu-budget.sh — cross-platform logical-core detection + per-sweep CPU
# budget math, shared by spawn-claude.sh's CPU-quota enforcement (issue
# #5111: nothing bounded a sweep's CPU, so a single agent-written driver
# could run N concurrent CPU-bound processes and saturate an entire host).
#
# Issue #5979 made that budget HOST-WIDE rather than per-sweep: the original
# math was a pure function of the host's core count, so three concurrent
# sweeps on an 8-core box each independently computed "6 cores are mine" and
# summed to 18 (observed: load 133.87, 0% idle, the host halting its own
# dispatch). The budget is now divided by the number of sweeps in flight on
# the host at spawn time.
#
# Source this file (do not exec). Defines:
#
#   loom_cpu_total_cores
#       Echoes the host's logical CPU count. Resolution order: `nproc`
#       (Linux, most package managers) -> `getconf _NPROCESSORS_ONLN`
#       (POSIX, covers Linux hosts without coreutils too) -> `sysctl -n
#       hw.ncpu` (macOS/BSD) -> `1` (last-resort fail-safe). Never echoes 0
#       or a non-numeric value — every caller does budget arithmetic on the
#       result, and a zero/garbage core count would either divide-by-zero
#       or silently clamp every sweep to a phantom cap.
#
#   loom_cpu_budget_cores <total_cores> <reserved_cores> [in_flight_sweeps]
#       Echoes max(1, floor(max(1, total_cores - reserved_cores) /
#       in_flight_sweeps)) — this sweep's SHARE of the host, not the whole
#       host (issue #5979). `in_flight_sweeps` defaults to 1, so a two-arg
#       call keeps the exact pre-#5979 `max(1, total - reserved)` result.
#       Always at least 1, so a small host (a reserved value >= total, or
#       more concurrent sweeps than cores) never computes a zero-core budget
#       that would deadlock every future sweep.
#
#   loom_cpu_inflight_sweeps [repo_root]
#       Echoes how many sweeps are running on THIS HOST right now, counting
#       the caller's own about-to-start sweep (issue #5979). Always echoes a
#       positive integer; `1` is the fail-safe answer whenever the count
#       cannot be established, which reproduces pre-#5979 behavior exactly.
#       Resolution order: $LOOM_SWEEP_INFLIGHT_SWEEPS (explicit override) ->
#       `loom-daemon status --json` (host-wide, union across every managed
#       root) -> 1.

loom_cpu_total_cores() {
    local n=""
    if command -v nproc >/dev/null 2>&1; then
        n="$(nproc 2>/dev/null || true)"
    fi
    if ! [[ "$n" =~ ^[0-9]+$ ]] || [[ "$n" -eq 0 ]]; then
        n="$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)"
    fi
    if ! [[ "$n" =~ ^[0-9]+$ ]] || [[ "$n" -eq 0 ]]; then
        n="$(sysctl -n hw.ncpu 2>/dev/null || true)"
    fi
    if ! [[ "$n" =~ ^[0-9]+$ ]] || [[ "$n" -eq 0 ]]; then
        n=1
    fi
    echo "$n"
}

loom_cpu_budget_cores() {
    local total="$1" reserved="$2" in_flight="${3:-1}"
    if ! [[ "$total" =~ ^[0-9]+$ ]]; then
        total=1
    fi
    if ! [[ "$reserved" =~ ^[0-9]+$ ]]; then
        reserved=0
    fi
    # A garbage or zero divisor must never divide-by-zero or inflate the
    # budget — fall back to "this sweep is the only one" (pre-#5979 math).
    if ! [[ "$in_flight" =~ ^[0-9]+$ ]] || ((in_flight < 1)); then
        in_flight=1
    fi
    local budget=$((total - reserved))
    if ((budget < 1)); then
        budget=1
    fi
    # Issue #5979: split the host's usable cores among the sweeps that are
    # actually running on it, instead of handing each sweep the whole host.
    # Integer division floors deliberately (3 sweeps on 6 usable cores get 2
    # each, never 2.67 rounded up to 3) so the shares sum to <= the host —
    # except at the 1-core floor below, where they cannot.
    budget=$((budget / in_flight))
    if ((budget < 1)); then
        budget=1
    fi
    echo "$budget"
}

# loom_cpu_inflight_sweeps [repo_root] -> host-wide concurrent-sweep count,
# INCLUDING the caller's own sweep. See the header block for the contract.
#
# Self-inclusion is explicit rather than assumed. The daemon inserts a sweep's
# registry entry AFTER `Command::spawn()` returns (see
# `sweep_registry/dispatch.rs`), so a child that probes fast enough can see a
# snapshot that does not yet list itself. Counting `others + 1` is correct
# under either ordering: the caller's own issue number
# ($LOOM_SWEEP_CLAIM_OWNED, exported by the daemon on every dispatch) is
# subtracted out of the snapshot if present, then added back unconditionally.
# A sweep with no claimed issue (a `--prs` Mode C sweep, or a hand-run
# spawn-claude.sh) simply never matches, so it is counted as the "+1".
loom_cpu_inflight_sweeps() {
    local root="${1:-${WORKSPACE:-$PWD}}"

    # 1. Explicit override — a test hook, and the escape hatch for a host
    #    running concurrent sweeps that the local daemon does not know about.
    if [[ "${LOOM_SWEEP_INFLIGHT_SWEEPS:-}" =~ ^[0-9]+$ ]] \
        && ((LOOM_SWEEP_INFLIGHT_SWEEPS >= 1)); then
        echo "$LOOM_SWEEP_INFLIGHT_SWEEPS"
        return 0
    fi

    # 2. Ask the local daemon. Every dependency below is optional: a host with
    #    no jq, no daemon binary, no running daemon, or a daemon that does not
    #    answer within the probe budget falls through to the fail-safe `1`.
    command -v jq >/dev/null 2>&1 || { echo 1; return 0; }

    local _lib_dir
    _lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if ! declare -F loom_locate_daemon_bin >/dev/null 2>&1; then
        [[ -f "$_lib_dir/locate-daemon-bin.sh" ]] || { echo 1; return 0; }
        # shellcheck source=./locate-daemon-bin.sh
        source "$_lib_dir/locate-daemon-bin.sh"
    fi
    if ! declare -F bounded_run >/dev/null 2>&1; then
        [[ -f "$_lib_dir/bounded-run.sh" ]] || { echo 1; return 0; }
        # shellcheck source=./bounded-run.sh
        source "$_lib_dir/bounded-run.sh"
    fi

    local bin
    bin="$(loom_locate_daemon_bin "$root" 2>/dev/null || true)"
    [[ -n "$bin" && -x "$bin" ]] || { echo 1; return 0; }

    # Hard bound on the probe: this runs on the critical path of every spawn,
    # and an unresponsive daemon must cost a few seconds, never the sweep.
    local probe_secs="${LOOM_SWEEP_INFLIGHT_PROBE_TIMEOUT_SECS:-10}"
    [[ "$probe_secs" =~ ^[0-9]+$ ]] && ((probe_secs >= 1)) || probe_secs=10

    local status_json
    status_json="$(bounded_run "$probe_secs" "$bin" status --json 2>/dev/null || true)"
    [[ -n "$status_json" ]] || { echo 1; return 0; }

    # `in_flight` is the daemon's union across every managed root, so this is
    # a host-wide count, not a per-repo one. `unregistered_locked` (#4214) is
    # added because those entries are documented as demonstrably-alive sweeps
    # the in-memory registry lost track of — they burn CPU exactly like a
    # registered sweep does. The two lists are disjoint by construction.
    local n
    n="$(printf '%s' "$status_json" | jq -r --arg self "${LOOM_SWEEP_CLAIM_OWNED:-}" '
        (.in_flight // []) as $live
        | (.unregistered_locked // []) as $locked
        | ([$live[] | select((.kind.type? == "Issue")
              and (($self != "") and ((.kind.value? | tostring) == $self)))] | length) as $self_live
        | ([$locked[] | select(($self != "") and ((.issue? | tostring) == $self))] | length) as $self_locked
        | (($live | length) + ($locked | length) - $self_live - $self_locked) + 1
    ' 2>/dev/null || true)"

    if [[ "$n" =~ ^[0-9]+$ ]] && ((n >= 1)); then
        echo "$n"
    else
        echo 1
    fi
}
