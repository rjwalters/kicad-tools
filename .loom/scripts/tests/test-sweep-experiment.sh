#!/usr/bin/env bash
# test-sweep-experiment.sh - Unit tests for the sweep model-cost experiment
# instrumentation (issue #3725).
#
# The sweep skill shells out to `sweep-experiment.sh`, a thin stub that execs
# `python3 -m loom_tools.sweep_experiment`. In the Loom SOURCE tree the stub
# cannot resolve loom-tools from `defaults/scripts/` (repo-root detection stops
# at `defaults/.loom`; in a real install the script lives at `.loom/scripts/`),
# so these tests drive the exact module the stub execs, with PYTHONPATH set the
# same way `run_loom_tool` sets it. The stub file itself is covered by
# `bash -n` + `shellcheck` in CI.
#
# Usage:
#   bash defaults/scripts/tests/test-sweep-experiment.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
export PYTHONPATH="$REPO_ROOT/loom-tools/src:${PYTHONPATH:-}"

SE() { python3 -m loom_tools.sweep_experiment "$@"; }

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_PASSED=$((TESTS_PASSED+1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_FAILED=$((TESTS_FAILED+1)); echo -e "  ${RED}FAIL${NC}: $1"; }
assert_eq()      { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1' want '$2')"; fi; }
assert_contains(){ if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 (missing '$2' in: $1)"; fi; }
assert_file()    { if [[ -f "$1" ]]; then pass "$2"; else fail "$2 (missing: $1)"; fi; }

if ! command -v python3 >/dev/null 2>&1; then echo "ERROR: python3 required" >&2; exit 1; fi

echo "Case 1: tri-state resolution (env-over-config, default off, malformed -> off)"
assert_eq "$(SE resolve-mode --config /nonexistent 2>/dev/null)" "off" "default is off"
assert_eq "$(LOOM_MODEL_EXPERIMENT=observe SE resolve-mode --config /nonexistent 2>/dev/null)" "observe" "env observe"
assert_eq "$(LOOM_MODEL_EXPERIMENT=bogus SE resolve-mode --config /nonexistent 2>/dev/null)" "off" "malformed env -> off"
echo ""

echo "Case 2: canary guardrail — experiment downgrades to observe on non-canary"
OUT="$(LOOM_MODEL_EXPERIMENT=experiment SE resolve-mode --config /nonexistent 2>&1)"
assert_contains "$OUT" "observe" "non-canary experiment downgraded to observe"
assert_contains "$OUT" "NON-CANARY" "loud warning names the guardrail"
assert_eq "$(LOOM_MODEL_EXPERIMENT=experiment LOOM_MODEL_EXPERIMENT_CANARY=1 SE resolve-mode --config /nonexistent 2>/dev/null)" "experiment" "canary confirmed -> experiment honored"
echo ""

echo "Case 3: deterministic + stratified arm assignment"
A1="$(SE assign-arm --issue 100 --complexity routine)"
A2="$(SE assign-arm --issue 100 --complexity routine)"
assert_eq "$A1" "$A2" "same issue+complexity is resume-stable"
assert_eq "$A1" "A opus" "issue 100 routine -> A opus"
assert_eq "$(SE assign-arm --issue 100 --complexity complex)" "B sonnet" "issue 100 complex -> opposite arm (stratified)"
assert_eq "$(SE assign-arm --issue 101 --complexity routine)" "B sonnet" "issue 101 routine -> B sonnet (parity)"
echo ""

echo "Case 4: startup banner names mode + arm"
BAN="$(LOOM_MODEL_EXPERIMENT=experiment LOOM_MODEL_EXPERIMENT_CANARY=1 SE banner --issue 100 --complexity complex --config /nonexistent 2>/dev/null)"
assert_contains "$BAN" "mode=EXPERIMENT" "banner names experiment mode"
assert_contains "$BAN" "ARM B" "banner names the assigned arm"
assert_contains "$BAN" "SUPPRESSED" "banner notes tier-2.5 suppression"
echo ""

echo "Case 5: record append + harvest with transcript join (exact cost)"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/se-test.XXXXXX")"
STATS="$ROOT/stats.jsonl"
SESS="$ROOT/archive/myrepo/2026-07-22/UUID1"
mkdir -p "$SESS/UUID1/subagents"
printf '%s\n' '{"message":{"model":"claude-opus-4-8","usage":{"input_tokens":10,"output_tokens":5,"cache_creation_input_tokens":1000,"cache_read_input_tokens":2000}}}' \
  > "$SESS/UUID1/subagents/agent-bld1.jsonl"
cat > "$SESS/index.json" <<EOF
{"schema":"loom.transcript-index/v1","session_uuid":"UUID1","repo":"myrepo","agents":[{"agent_id":"agent-bld1","role":"loom-builder","transcript":"subagents/agent-bld1.jsonl"}]}
EOF
SE record --quiet --stats-file "$STATS" --issue 100 --mode experiment --arm A --model opus --complexity routine --phase builder --role builder --agent-id agent-bld1 --attempt 1
SE record --quiet --stats-file "$STATS" --issue 100 --mode experiment --arm A --phase judge --role judge --verdict pass --attempt 1
SE record --quiet --stats-file "$STATS" --issue 100 --mode experiment --arm A --phase merge --role merge
assert_file "$STATS" "stats JSONL created"
NLINES="$(wc -l < "$STATS" | tr -d ' ')"
assert_eq "$NLINES" "3" "three JSONL records appended"
FMODE="$(stat -f '%Lp' "$STATS" 2>/dev/null || stat -c '%a' "$STATS")"
assert_eq "$FMODE" "600" "stats file is 0600"
HARV="$(SE harvest --stats-file "$STATS" --archive-dir "$ROOT/archive" --format json 2>/dev/null)"
assert_contains "$HARV" '"transcript": 1' "harvest joined 1 transcript (exact cost)"
assert_contains "$HARV" '"first_attempt_pass_rate": 1.0' "arm A first-attempt pass rate 100%"
assert_contains "$HARV" '"merge_rate": 1.0' "arm A merge rate 100%"
# exact cost 0.022275 from the single usage block
assert_contains "$HARV" '0.022275' "exact cache-aware cost from transcript usage"
rm -rf "$ROOT"
echo ""

echo "Case 6: harvest on empty/missing store does not crash"
OUT="$(SE harvest --stats-file "/nonexistent-$$.jsonl" 2>&1)"; RC=$?
assert_eq "$RC" "0" "harvest exits 0 on missing store"
assert_contains "$OUT" "records: 0" "harvest reports zero records"
echo ""

echo "════════════════════════════════════════════"
echo "  Total: $TESTS_RUN  Passed: $TESTS_PASSED  Failed: $TESTS_FAILED"
echo "════════════════════════════════════════════"
[[ "$TESTS_FAILED" -eq 0 ]]
