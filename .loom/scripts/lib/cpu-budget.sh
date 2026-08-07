#!/usr/bin/env bash
# cpu-budget.sh — cross-platform logical-core detection + per-sweep CPU
# budget math, shared by spawn-claude.sh's CPU-quota enforcement (issue
# #5111: nothing bounded a sweep's CPU, so a single agent-written driver
# could run N concurrent CPU-bound processes and saturate an entire host).
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
#   loom_cpu_budget_cores <total_cores> <reserved_cores>
#       Echoes max(1, total_cores - reserved_cores) — the number of cores a
#       single sweep may use. Always at least 1, so a small host (or a
#       reserved value >= total) never computes a zero-core budget that
#       would deadlock every future sweep.

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
    local total="$1" reserved="$2"
    if ! [[ "$total" =~ ^[0-9]+$ ]]; then
        total=1
    fi
    if ! [[ "$reserved" =~ ^[0-9]+$ ]]; then
        reserved=0
    fi
    local budget=$((total - reserved))
    if ((budget < 1)); then
        budget=1
    fi
    echo "$budget"
}
