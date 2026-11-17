#!/usr/bin/env bash
# test-memory-budget.sh — tests for defaults/scripts/lib/memory-budget.sh
# (issue #7430, epic #6896 Phase 3 — per-sweep resource limits, the memory
# axis).
#
# Mirrors test-cpu-budget.sh's shape:
#
#   loom_mem_total_mb
#       Host-dependent — only asserted to be a positive integer, exactly
#       like loom_cpu_total_cores's own test below (this suite must pass
#       identically on any host, CI runner or laptop).
#
#   loom_mem_budget_mb <total_mb> <reserved_mb> [in_flight]
#       Pure function of its arguments — asserted against exact expected
#       values, the same way loom_cpu_budget_cores is.
#
# Usage:
#   bash defaults/scripts/tests/test-memory-budget.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SCRIPT_DIR/../lib/memory-budget.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_PASSED=$((TESTS_PASSED+1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_FAILED=$((TESTS_FAILED+1)); echo -e "  ${RED}FAIL${NC}: $1"; }
assert_eq() { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1' want '$2')"; fi; }

if [[ ! -r "$LIB" ]]; then
    echo -e "${RED}FATAL${NC}: cannot read $LIB"
    exit 1
fi
# shellcheck source=../lib/memory-budget.sh
source "$LIB"

echo "════════════════════════════════════════════"
echo "  memory-budget.sh tests (#7430)"
echo "════════════════════════════════════════════"

# ------------------------------------------------------------------
echo ""
echo "-- loom_mem_total_mb --"
# ------------------------------------------------------------------

mem_total="$(loom_mem_total_mb)"
if [[ "$mem_total" =~ ^[0-9]+$ ]] && ((mem_total >= 1)); then
    pass "loom_mem_total_mb echoes a positive integer (got ${mem_total} MiB)"
else
    fail "loom_mem_total_mb echoes a positive integer (got '$mem_total')"
fi

# ------------------------------------------------------------------
echo ""
echo "-- loom_mem_budget_mb: single-sweep (pre-#5979-style) contract --"
# ------------------------------------------------------------------

assert_eq "$(loom_mem_budget_mb 16384 2048)" "14336" \
    "16384 MiB total - 2048 MiB reserved = 14336 MiB (single sweep)"
assert_eq "$(loom_mem_budget_mb 4096 2048)" "2048" \
    "a smaller host still yields a sane budget: 4096 - 2048 = 2048"
assert_eq "$(loom_mem_budget_mb 1024 2048)" "512" \
    "an over-large reservation clamps to the 512 MiB floor, never 0 or negative"
assert_eq "$(loom_mem_budget_mb garbage 2048)" "2048" \
    "a non-numeric total degrades to the 4096 MiB fallback: 4096 - 2048 = 2048"
assert_eq "$(loom_mem_budget_mb 16384 garbage)" "16384" \
    "a non-numeric reservation is treated as 0"

# ------------------------------------------------------------------
echo ""
echo "-- loom_mem_budget_mb: host-wide division across in-flight sweeps (#5979 parity) --"
# ------------------------------------------------------------------

assert_eq "$(loom_mem_budget_mb 16384 2048 2)" "7168" \
    "2 concurrent sweeps split the 14336 MiB usable budget: 14336 / 2 = 7168"
assert_eq "$(loom_mem_budget_mb 16384 2048 4)" "3584" \
    "4 concurrent sweeps split it further: 14336 / 4 = 3584"
assert_eq "$(loom_mem_budget_mb 16384 2048 100)" "512" \
    "a huge concurrent-sweep count clamps to the 512 MiB floor, never 0"
assert_eq "$(loom_mem_budget_mb 16384 2048 0)" "14336" \
    "an invalid (zero) divisor is treated as 1, exactly like loom_cpu_budget_cores"

# ------------------------------------------------------------------
echo ""
echo "-- Saturated-host regression: N sweeps' shares never exceed the host total --"
# ------------------------------------------------------------------
# This is the memory-axis form of the #5979 CPU regression check: prove the
# SAME division rule that keeps concurrent CPU budgets from summing past the
# host (the #5979 load-133.87 incident's fix) also holds for memory, so N
# concurrent containerized sweeps' declared `--memory` caps can never sum to
# more than the host's usable RAM.

total=16384
reserved=2048
usable=$((total - reserved))
for n in 1 2 3 5 8; do
    share="$(loom_mem_budget_mb "$total" "$reserved" "$n")"
    summed=$((share * n))
    if ((summed <= usable || share == 512)); then
        pass "N=$n: share=${share}MiB * $n = ${summed}MiB <= usable ${usable}MiB (or floor-clamped)"
    else
        fail "N=$n: share=${share}MiB * $n = ${summed}MiB EXCEEDS usable ${usable}MiB — the #5979 regression, on the memory axis"
    fi
done

echo ""
echo "═══════════════════════════════════════════"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
