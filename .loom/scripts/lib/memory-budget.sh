#!/usr/bin/env bash
# memory-budget.sh — cross-platform total-memory detection + per-sweep memory
# budget math, mirroring lib/cpu-budget.sh's shape (issue #5111/#5979) for the
# memory axis (issue #7430, epic #6896 Phase 3: per-sweep resource limits and
# containment observability).
#
# Motivation: spawn-claude.sh's containerized dispatch mode (issue #7429)
# shipped with no `--cpus`/`--memory` docker flags by its own explicit scope
# note — a containerized sweep could still exhaust host memory even though
# CPU is already budgeted (issue #5111) and divided across concurrent sweeps
# (issue #5979). This gives the memory axis the same treatment: a per-sweep
# share of the host's usable RAM, divided across sweeps currently in flight,
# so N concurrent containerized sweeps' declared `--memory` caps sum to no
# more than the host's usable total — never a repeat of the #5979 "each
# sweep independently claims the whole budget" bug, this time on the memory
# axis instead of CPU.
#
# Source this file (do not exec). Defines:
#
#   loom_mem_total_mb
#       Echoes the host's total physical memory in MiB. Resolution order:
#       `/proc/meminfo`'s `MemTotal:` line (Linux, reported in KiB) ->
#       `sysctl -n hw.memsize` (macOS/BSD, reported in bytes) -> `4096` (a
#       conservative 4 GiB last-resort fail-safe). Never echoes 0 or a
#       non-numeric value — every caller does budget arithmetic on the
#       result.
#
#   loom_mem_budget_mb <total_mb> <reserved_mb> [in_flight_sweeps]
#       Echoes max(512, floor(max(512, total_mb - reserved_mb) /
#       in_flight_sweeps)) — this sweep's SHARE of the host's memory, mirroring
#       `loom_cpu_budget_cores`'s division (issue #5979). `in_flight_sweeps`
#       defaults to 1. Always at least 512 MiB, so a small host or a large
#       concurrent-sweep count never computes a budget too small for a
#       container to even start.

loom_mem_total_mb() {
    local kb="" bytes="" mb=""
    if [[ -r /proc/meminfo ]]; then
        kb="$(awk '/^MemTotal:/ { print $2; exit }' /proc/meminfo 2>/dev/null || true)"
        if [[ "$kb" =~ ^[0-9]+$ ]] && ((kb > 0)); then
            mb=$((kb / 1024))
        fi
    fi
    if ! [[ "$mb" =~ ^[0-9]+$ ]] || [[ "$mb" -eq 0 ]]; then
        bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
        if [[ "$bytes" =~ ^[0-9]+$ ]] && ((bytes > 0)); then
            mb=$((bytes / 1024 / 1024))
        fi
    fi
    if ! [[ "$mb" =~ ^[0-9]+$ ]] || [[ "$mb" -eq 0 ]]; then
        mb=4096
    fi
    echo "$mb"
}

loom_mem_budget_mb() {
    local total="$1" reserved="$2" in_flight="${3:-1}"
    if ! [[ "$total" =~ ^[0-9]+$ ]]; then
        total=4096
    fi
    if ! [[ "$reserved" =~ ^[0-9]+$ ]]; then
        reserved=0
    fi
    if ! [[ "$in_flight" =~ ^[0-9]+$ ]] || ((in_flight < 1)); then
        in_flight=1
    fi
    local budget=$((total - reserved))
    if ((budget < 512)); then
        budget=512
    fi
    budget=$((budget / in_flight))
    if ((budget < 512)); then
        budget=512
    fi
    echo "$budget"
}
