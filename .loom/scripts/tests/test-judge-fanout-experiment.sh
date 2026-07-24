#!/usr/bin/env bash
# test-judge-fanout-experiment.sh — tests for the #3739/#3748 Judge fan-out
# experiment artifacts.
#
# Covers everything testable WITHOUT the Workflow tool (which a builder/CI does
# not have):
#   1. judge-fanout-workflow.js is syntactically loadable (compile-only via the
#      same vm.Script wrap-and-never-execute pattern the gate uses), including
#      the #3748 hardened prompts + No-Fable-Judge guard.
#   2. judge-fanout-experiment.sh with the flag UNSET is a strict no-op: exit 0,
#      no dispatch, and it writes no files.
#   3. judge-fanout-experiment.sh with the flag SET syntax-checks both the
#      workflow and the corpus-runner scaffold and still runs no live judge.
#   4. The corpus-runner scaffold's non-Workflow-tool logic (arg validation,
#      results-table emission) runs and produces the expected shape — without
#      ever invoking the Workflow tool (a stub `gh` provides the diff).
#
# Usage:
#   bash defaults/scripts/tests/test-judge-fanout-experiment.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
EXP_DIR="$REPO_ROOT/defaults/scripts/experiments"
GATE="$EXP_DIR/judge-fanout-experiment.sh"
WORKFLOW_JS="$EXP_DIR/judge-fanout-workflow.js"
RUNNER="$EXP_DIR/judge-fanout-corpus-runner.sh"
TEMPLATE="$EXP_DIR/judge-fanout-results-template.md"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_PASSED=$((TESTS_PASSED+1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_FAILED=$((TESTS_FAILED+1)); echo -e "  ${RED}FAIL${NC}: $1"; }
assert_eq()       { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1' want '$2')"; fi; }
assert_contains() { if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 (missing '$2' in: $1)"; fi; }
assert_file()     { if [[ -f "$1" ]]; then pass "$2"; else fail "$2 (missing: $1)"; fi; }
assert_no_file()  { if [[ ! -f "$1" ]]; then pass "$2"; else fail "$2 (unexpectedly present: $1)"; fi; }

echo "Preflight: artifacts present"
assert_file "$GATE" "gate script exists"
assert_file "$WORKFLOW_JS" "workflow JS exists"
assert_file "$RUNNER" "corpus-runner scaffold exists"
assert_file "$TEMPLATE" "results template exists"
echo ""

echo "Case 1: workflow JS is syntactically loadable (compile-only, never executed)"
if command -v node >/dev/null 2>&1; then
  OUT="$(node -e '
    const fs = require("fs");
    const vm = require("vm");
    const body = fs.readFileSync(process.argv[1], "utf8");
    new vm.Script("(async () => {\n" + body + "\n})", { filename: process.argv[1] });
    console.log("COMPILE_OK");
  ' "$WORKFLOW_JS" 2>&1)"
  RC=$?
  assert_eq "$RC" "0" "workflow compiles clean"
  assert_contains "$OUT" "COMPILE_OK" "workflow compile emitted OK marker"
  # The hardened artifact must carry its guardrails.
  assert_contains "$(cat "$WORKFLOW_JS")" "assertNotFable" "No-Fable-Judge guard present (#3702)"
  assert_contains "$(cat "$WORKFLOW_JS")" "security-credential-surface" "dimension-scoped prompt present"
else
  echo "  (node not found — skipping compile check)"
fi
echo ""

echo "Case 2: flag UNSET is a strict no-op (exit 0, no dispatch, no file writes)"
SENTINEL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jf-noop.XXXXXX")"
# Run the gate with the flag unset, cwd inside an empty sentinel dir so any
# accidental file write would be detectable.
OUT="$( cd "$SENTINEL_DIR" && env -u LOOM_JUDGE_FANOUT_EXPERIMENT bash "$GATE" 2>&1 )"; RC=$?
assert_eq "$RC" "0" "flag-unset gate exits 0"
assert_contains "$OUT" "disabled" "flag-unset gate reports disabled"
assert_contains "$OUT" "no-op" "flag-unset gate reports no-op"
# The disabled path must not run the syntax-check banner or the live-run guidance.
if [[ "$OUT" == *"EXPERIMENT (#3739)"* ]]; then fail "flag-unset must not print the enabled banner"; else pass "flag-unset prints no enabled banner"; fi
# No files created in the sentinel dir.
CREATED="$(find "$SENTINEL_DIR" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "$CREATED" "0" "flag-unset gate writes no files"
# Explicit off-ish values are also no-ops.
for v in 0 false no off ""; do
  RC2=0
  ( cd "$SENTINEL_DIR" && LOOM_JUDGE_FANOUT_EXPERIMENT="$v" bash "$GATE" >/dev/null 2>&1 ) || RC2=$?
  assert_eq "$RC2" "0" "flag='$v' is a no-op exit 0"
done
rm -rf "$SENTINEL_DIR"
echo ""

echo "Case 3: flag SET syntax-checks both artifacts and runs no live judge"
OUT="$( LOOM_JUDGE_FANOUT_EXPERIMENT=1 bash "$GATE" 2>&1 )"; RC=$?
assert_eq "$RC" "0" "flag-set gate exits 0"
assert_contains "$OUT" "EXPERIMENT (#3739)" "flag-set gate prints enabled banner"
if command -v node >/dev/null 2>&1; then
  assert_contains "$OUT" "syntax OK" "flag-set gate syntax-checks the workflow"
fi
assert_contains "$OUT" "corpus-runner scaffold syntax OK" "flag-set gate syntax-checks the scaffold"
assert_contains "$OUT" "does NOT dispatch a live judge" "flag-set gate confirms no live run"
echo ""

echo "Case 4: corpus-runner scaffold logic runs + produces expected shape (no Workflow tool)"
RUN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/jf-run.XXXXXX")"
# Stub `gh` so the scaffold's `gh pr diff` returns a fake diff — no network, no
# real GitHub calls, and definitely no Workflow tool.
STUB_BIN="$RUN_ROOT/bin"; mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/gh" <<'STUB'
#!/usr/bin/env bash
# minimal gh stub: only `gh pr diff <N>` is exercised.
if [[ "$1" == "pr" && "$2" == "diff" ]]; then
  printf '%s\n' "@@ -1,2 +1,3 @@ stub" "+added line for PR $3"
  exit 0
fi
exit 0
STUB
chmod +x "$STUB_BIN/gh"

OUT_DIR="$RUN_ROOT/out"
OUT="$( PATH="$STUB_BIN:$PATH" bash "$RUNNER" --out "$OUT_DIR" 3721:merged 3699:rejected 2>&1 )"; RC=$?
assert_eq "$RC" "0" "scaffold exits 0"
assert_file "$OUT_DIR/pr-3721.args.json" "per-PR args built for 3721"
assert_file "$OUT_DIR/pr-3699.args.json" "per-PR args built for 3699"
assert_file "$OUT_DIR/results.md" "results table emitted"
# args JSON has the expected keys and PR number.
ARGS="$(cat "$OUT_DIR/pr-3721.args.json")"
assert_contains "$ARGS" '"pr": 3721' "args carries pr number"
assert_contains "$ARGS" '"diff":' "args carries diff field"
assert_contains "$ARGS" "added line for PR 3721" "args diff came from gh stub"
# results table has header + one row per PR, ground-truth outcome filled, measured cells blank.
TBL="$(cat "$OUT_DIR/results.md")"
assert_contains "$TBL" "| PR # | Known outcome |" "results table has the expected header"
assert_contains "$TBL" "| 3721 | merged |" "row for 3721 with known outcome, empty measured cells"
assert_contains "$TBL" "| 3699 | rejected |" "row for 3699 with known outcome"
assert_contains "$TBL" "Do NOT fabricate data" "results table warns against fabricated data"
# The scaffold's Workflow hook point must stay an UNFILLED, documented stub — a
# builder cannot invoke the Workflow tool (#3289), so it exists only as a marked
# TODO/example. (bash cannot invoke Workflow at all — any occurrence is textual —
# so we assert the stub markers are present rather than grep for the call string.)
RUNNER_SRC="$(cat "$RUNNER")"
assert_contains "$RUNNER_SRC" "HOOK POINT (UNFILLED" "scaffold marks the hook point UNFILLED"
assert_contains "$RUNNER_SRC" "emit_workflow_invocation_stub" "scaffold hook point is a stub function"
assert_contains "$RUNNER_SRC" "does NOT invoke the Workflow tool" "scaffold documents it never invokes the tool"

echo ""
echo "Case 5: scaffold input validation rejects bad specs"
RC2=0; ( PATH="$STUB_BIN:$PATH" bash "$RUNNER" --out "$OUT_DIR" notanumber >/dev/null 2>&1 ) || RC2=$?
if [[ "$RC2" -ne 0 ]]; then pass "non-numeric PR spec rejected"; else fail "non-numeric PR spec should be rejected"; fi
RC3=0; ( PATH="$STUB_BIN:$PATH" bash "$RUNNER" --out "$OUT_DIR" 3721:bogus >/dev/null 2>&1 ) || RC3=$?
if [[ "$RC3" -ne 0 ]]; then pass "invalid outcome rejected"; else fail "invalid outcome should be rejected"; fi
RC4=0; ( PATH="$STUB_BIN:$PATH" bash "$RUNNER" --out "$OUT_DIR" >/dev/null 2>&1 ) || RC4=$?
if [[ "$RC4" -ne 0 ]]; then pass "no-PR invocation rejected"; else fail "no-PR invocation should be rejected"; fi
rm -rf "$RUN_ROOT"
echo ""

echo "════════════════════════════════════════════"
echo "  Total: $TESTS_RUN  Passed: $TESTS_PASSED  Failed: $TESTS_FAILED"
echo "════════════════════════════════════════════"
[[ "$TESTS_FAILED" -eq 0 ]]
