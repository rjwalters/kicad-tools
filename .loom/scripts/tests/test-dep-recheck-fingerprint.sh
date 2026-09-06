#!/usr/bin/env bash
# test-dep-recheck-fingerprint.sh - Unit tests for dep-recheck-fingerprint.sh
# (#7281), the shared fingerprint computation behind curator.md's "Re-check
# Idempotency" (#4986) and "Checking Operator-Only Premises" (#6849) sections.
#
# The regression under test is production hash churn: #6335/#6805 accumulated
# dozens of distinct `CONCLUSION_HASH` values over weeks despite an unchanged
# blocking condition, because every Curator pass hand-rolled the computation
# from prose instead of sharing one tested implementation. T3/T4 are the
# direct regression tests — a PR's `mergeable`/`mergeStateStatus` flickering
# through `UNKNOWN` (GitHub has not finished computing it yet) must not, on
# its own, change the hash.
#
# Strategy: most tests drive `--stdin` directly (pure function, no `gh` at
# all — the simplest and fastest way to pin down the hashing/decision logic).
# A smaller set of tests stub `gh` on PATH to cover the `--number` live-fetch
# path end to end.
#
# Usage:
#   ./.loom/scripts/tests/test-dep-recheck-fingerprint.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_SCRIPT="$HELPERS_DIR/dep-recheck-fingerprint.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_ne() {
    local a="$1" b="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$a" != "$b" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Both sides were: '$a'"
    fi
}

[[ -x "$TARGET_SCRIPT" ]] || {
    echo -e "${RED}FATAL${NC}: $TARGET_SCRIPT missing or not executable"
    exit 2
}
command -v jq >/dev/null 2>&1 || {
    echo -e "${RED}FATAL${NC}: jq required"
    exit 2
}

field() { # <output> <KEY>
    grep -E "^$2=" <<<"$1" | head -n 1 | cut -d= -f2-
}

echo "Testing dep-recheck-fingerprint.sh..."
echo ""

# --- T0: usage errors --------------------------------------------------------
rc=0
"$TARGET_SCRIPT" bogus --stdin >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T0a: unknown subcommand is a usage error"
rc=0
"$TARGET_SCRIPT" dep-recheck >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T0b: neither --number nor --stdin is a usage error"
rc=0
echo '{"prs":[]}' | "$TARGET_SCRIPT" dep-recheck --stdin --number 1 >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T0c: --stdin and --number together is a usage error"
rc=0
echo '{"prs":[]}' | "$TARGET_SCRIPT" dep-recheck --stdin --verdict bogus >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T0d: an invalid --verdict value is a usage error"

# --- T1: identical input twice -> identical hash (the core determinism bug) --
FIXTURE_BLOCKED='{"prs":[{"number":4743,"state":"OPEN","labels":["loom:changes-requested"],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}]}'
out1="$(echo "$FIXTURE_BLOCKED" | "$TARGET_SCRIPT" dep-recheck --stdin)"
out2="$(echo "$FIXTURE_BLOCKED" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "$(field "$out1" CONCLUSION_HASH)" "$(field "$out2" CONCLUSION_HASH)" \
    "T1a: identical input JSON produces an identical hash across repeated invocations"
assert_eq "blocked" "$(field "$out1" VERDICT)" "T1b: an OPEN PR with a blocking label is VERDICT=blocked"
assert_ne "" "$(field "$out1" CONCLUSION_HASH)" "T1c: CONCLUSION_HASH is non-empty"

# --- T2: no linked PR at all -> VERDICT=clear, empty BLOCKERS ---------------
out="$(echo '{"prs":[]}' | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "clear" "$(field "$out" VERDICT)" "T2: an empty prs list defaults to VERDICT=clear"
assert_eq "" "$(field "$out" BLOCKERS)" "T2: BLOCKERS is empty when there are no linked PRs"

# --- T3: THE #7281 REGRESSION - transient UNKNOWN must not flip the verdict -
# A PR blocking purely on merge-state (no blocking label): CONFLICTING today.
FIXTURE_CONFLICTING='{"prs":[{"number":100,"state":"OPEN","labels":[],"mergeable":"CONFLICTING","mergeStateStatus":"CONFLICTING"}]}'
out_conflicting="$(echo "$FIXTURE_CONFLICTING" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "blocked" "$(field "$out_conflicting" VERDICT)" "T3a: merge-state CONFLICTING alone (no label) is VERDICT=blocked"

# Same PR, same state/labels, but GitHub has not finished computing mergeable
# yet (a transient read, not a real change).
FIXTURE_UNKNOWN='{"prs":[{"number":100,"state":"OPEN","labels":[],"mergeable":"UNKNOWN","mergeStateStatus":"UNKNOWN"}]}'
out_unknown="$(echo "$FIXTURE_UNKNOWN" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "blocked" "$(field "$out_unknown" VERDICT)" \
    "T3b: a transient UNKNOWN merge state fails safe to still-blocked, not clear (#7281)"
assert_eq "$(field "$out_conflicting" CONCLUSION_HASH)" "$(field "$out_unknown" CONCLUSION_HASH)" \
    "T3c: CONFLICTING -> UNKNOWN (state/labels unchanged) does not change CONCLUSION_HASH"

# --- T4: only mergeStateStatus (not mergeable) reporting UNKNOWN, same rule -
FIXTURE_UNKNOWN_STATUS_ONLY='{"prs":[{"number":100,"state":"OPEN","labels":[],"mergeable":"CONFLICTING","mergeStateStatus":"UNKNOWN"}]}'
out="$(echo "$FIXTURE_UNKNOWN_STATUS_ONLY" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "blocked" "$(field "$out" VERDICT)" "T4: mergeStateStatus=UNKNOWN alone still fails safe to blocked"

# --- T5: a genuinely different PR state DOES change the hash ---------------
FIXTURE_CLEARED='{"prs":[{"number":100,"state":"OPEN","labels":[],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}]}'
out_cleared="$(echo "$FIXTURE_CLEARED" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "clear" "$(field "$out_cleared" VERDICT)" "T5a: a confirmed-clean merge state with no blocking label is VERDICT=clear"
assert_ne "$(field "$out_conflicting" CONCLUSION_HASH)" "$(field "$out_cleared" CONCLUSION_HASH)" \
    "T5b: CONFLICTING -> confirmed MERGEABLE (a real change) changes CONCLUSION_HASH"

FIXTURE_LABEL_ADDED='{"prs":[{"number":4743,"state":"OPEN","labels":["loom:changes-requested","loom:blocked"],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}]}'
out_label_added="$(echo "$FIXTURE_LABEL_ADDED" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_ne "$(field "$out1" CONCLUSION_HASH)" "$(field "$out_label_added" CONCLUSION_HASH)" \
    "T5c: an added block-bearing label (a real change) changes CONCLUSION_HASH"

FIXTURE_MERGED='{"prs":[{"number":4743,"state":"MERGED","labels":["loom:changes-requested"],"mergeable":"UNKNOWN","mergeStateStatus":"UNKNOWN"}]}'
out_merged="$(echo "$FIXTURE_MERGED" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "clear" "$(field "$out_merged" VERDICT)" "T5d: a MERGED PR no longer blocks regardless of its labels/merge state"
assert_ne "$(field "$out1" CONCLUSION_HASH)" "$(field "$out_merged" CONCLUSION_HASH)" \
    "T5e: OPEN -> MERGED (a real change) changes CONCLUSION_HASH"

# --- T6: label ordering churn from the API never looks like a changed
#         conclusion (labels are sorted before hashing) -------------------
FIXTURE_LABELS_A='{"prs":[{"number":1,"state":"OPEN","labels":["loom:blocked","loom:changes-requested"],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}]}'
FIXTURE_LABELS_B='{"prs":[{"number":1,"state":"OPEN","labels":["loom:changes-requested","loom:blocked"],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}]}'
out_a="$(echo "$FIXTURE_LABELS_A" | "$TARGET_SCRIPT" dep-recheck --stdin)"
out_b="$(echo "$FIXTURE_LABELS_B" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "$(field "$out_a" CONCLUSION_HASH)" "$(field "$out_b" CONCLUSION_HASH)" \
    "T6a: label ordering does not affect CONCLUSION_HASH"
FIXTURE_PRS_ORDER_A='{"prs":[{"number":1,"state":"OPEN","labels":[],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"},{"number":2,"state":"OPEN","labels":[],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}]}'
FIXTURE_PRS_ORDER_B='{"prs":[{"number":2,"state":"OPEN","labels":[],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"},{"number":1,"state":"OPEN","labels":[],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}]}'
out_pa="$(echo "$FIXTURE_PRS_ORDER_A" | "$TARGET_SCRIPT" dep-recheck --stdin)"
out_pb="$(echo "$FIXTURE_PRS_ORDER_B" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "$(field "$out_pa" CONCLUSION_HASH)" "$(field "$out_pb" CONCLUSION_HASH)" \
    "T6b: the order PRs are returned in does not affect CONCLUSION_HASH"

# --- T7: --verdict overrides the mechanical computation (secondary heuristic,
#         no linked PR at all) ----------------------------------------------
out="$(echo '{"prs":[]}' | "$TARGET_SCRIPT" dep-recheck --stdin --verdict blocked --block-reason "doctor cycle exhausted")"
assert_eq "blocked" "$(field "$out" VERDICT)" "T7a: --verdict overrides the mechanical (empty-prs -> clear) default"
assert_eq "doctor cycle exhausted" "$(field "$out" BLOCK_REASON)" "T7b: --block-reason is echoed back and folded into the hash"
out2="$(echo '{"prs":[]}' | "$TARGET_SCRIPT" dep-recheck --stdin --verdict blocked --block-reason "Sweep coordination: blocking")"
assert_ne "$(field "$out" CONCLUSION_HASH)" "$(field "$out2" CONCLUSION_HASH)" \
    "T7c: a changed --block-reason (same verdict) still changes CONCLUSION_HASH"

# --- T8: --orthogonal folds into the hash without disturbing the ordinary
#         (empty) case -------------------------------------------------------
out_ordinary="$(echo "$FIXTURE_CLEARED" | "$TARGET_SCRIPT" dep-recheck --stdin)"
out_orthogonal="$(echo "$FIXTURE_CLEARED" | "$TARGET_SCRIPT" dep-recheck --stdin --orthogonal "epic-open-but-complete:owner/repo#14")"
assert_ne "$(field "$out_ordinary" CONCLUSION_HASH)" "$(field "$out_orthogonal" CONCLUSION_HASH)" \
    "T8a: a non-empty --orthogonal changes CONCLUSION_HASH (the 'changed conclusion always comments' row fires)"
out_orthogonal2="$(echo "$FIXTURE_CLEARED" | "$TARGET_SCRIPT" dep-recheck --stdin --orthogonal "")"
assert_eq "$(field "$out_ordinary" CONCLUSION_HASH)" "$(field "$out_orthogonal2" CONCLUSION_HASH)" \
    "T8b: an empty --orthogonal (the default) leaves the hash exactly as before"

# --- T9: --json output -------------------------------------------------------
out="$(echo "$FIXTURE_BLOCKED" | "$TARGET_SCRIPT" dep-recheck --stdin --json)"
assert_eq "blocked" "$(jq -r '.verdict' <<<"$out")" "T9a: --json reports verdict"
assert_ne "" "$(jq -r '.conclusion_hash' <<<"$out")" "T9b: --json reports a non-empty conclusion_hash"

# --- T10: operator-premise - identical input twice -> identical hash -------
FIXTURE_STALE='{"refs":[{"number":14,"state":"CLOSED"},{"number":22,"state":"OPEN"}]}'
p1="$(echo "$FIXTURE_STALE" | "$TARGET_SCRIPT" operator-premise --stdin)"
p2="$(echo "$FIXTURE_STALE" | "$TARGET_SCRIPT" operator-premise --stdin)"
assert_eq "stale-premise" "$(field "$p1" VERDICT)" "T10a: any closed reference is VERDICT=stale-premise"
assert_eq "$(field "$p1" CONCLUSION_HASH)" "$(field "$p2" CONCLUSION_HASH)" \
    "T10b: operator-premise identical input twice produces an identical hash"

# --- T11: operator-premise - every reference open -> no hash at all --------
FIXTURE_ALL_OPEN='{"refs":[{"number":14,"state":"OPEN"},{"number":22,"state":"OPEN"}]}'
p="$(echo "$FIXTURE_ALL_OPEN" | "$TARGET_SCRIPT" operator-premise --stdin)"
assert_eq "open" "$(field "$p" VERDICT)" "T11a: every reference open is VERDICT=open"
assert_eq "" "$(field "$p" CONCLUSION_HASH)" "T11b: no hash is computed when every reference is still open (nothing to report)"

# --- T12: operator-premise - a genuinely different reference set changes
#          the hash, ref ordering does not -----------------------------------
FIXTURE_STALE_OTHER='{"refs":[{"number":14,"state":"OPEN"},{"number":22,"state":"CLOSED"}]}'
p_other="$(echo "$FIXTURE_STALE_OTHER" | "$TARGET_SCRIPT" operator-premise --stdin)"
assert_ne "$(field "$p1" CONCLUSION_HASH)" "$(field "$p_other" CONCLUSION_HASH)" \
    "T12a: a different closed reference changes CONCLUSION_HASH"
FIXTURE_STALE_REORDERED='{"refs":[{"number":22,"state":"OPEN"},{"number":14,"state":"CLOSED"}]}'
p_reordered="$(echo "$FIXTURE_STALE_REORDERED" | "$TARGET_SCRIPT" operator-premise --stdin)"
assert_eq "$(field "$p1" CONCLUSION_HASH)" "$(field "$p_reordered" CONCLUSION_HASH)" \
    "T12b: reference ordering does not affect operator-premise CONCLUSION_HASH"

# --- T13: live --number mode (stubbed gh) -----------------------------------
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

cat >"$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"

case "${1:-}" in
  issue)
    shift
    sub="$1"; shift
    num=""
    jqexpr=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --json) shift 2 ;;
        --jq) jqexpr="${2:-}"; shift 2 ;;
        --repo) shift 2 ;;
        *) [[ -z "$num" ]] && num="$1"; shift ;;
      esac
    done
    if [[ "$sub" == "view" ]]; then
      f="$D/issue-$num.json"
      [[ -f "$f" ]] || { echo "stub gh: missing $f" >&2; exit 1; }
      if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" "$f"; else cat "$f"; fi
    else
      echo "stub gh: unhandled issue sub '$sub'" >&2; exit 3
    fi
    ;;
  pr)
    shift
    sub="$1"; shift
    num=""
    jqexpr=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --json) shift 2 ;;
        --jq) jqexpr="${2:-}"; shift 2 ;;
        --repo) shift 2 ;;
        *) [[ -z "$num" ]] && num="$1"; shift ;;
      esac
    done
    if [[ "$sub" == "view" ]]; then
      f="$D/pr-$num.json"
      [[ -f "$f" ]] || { echo "stub gh: missing $f" >&2; exit 1; }
      if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" "$f"; else cat "$f"; fi
    else
      echo "stub gh: unhandled pr sub '$sub'" >&2; exit 3
    fi
    ;;
  *) echo "stub gh: unhandled args: $*" >&2; exit 3 ;;
esac
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

jq -n '{closedByPullRequestsReferences: [{number: 4743}]}' >"$STUB_DIR/issue-6335.json"
jq -n '{number: 4743, state: "OPEN", labels: [{name:"loom:changes-requested"}], mergeable: "CONFLICTING", mergeStateStatus: "CONFLICTING"}' \
    | jq '{number, state, labels: [.labels[].name], mergeable, mergeStateStatus}' >"$STUB_DIR/pr-4743.json"

out="$("$TARGET_SCRIPT" dep-recheck --number 6335 --repo owner/repo)"
assert_eq "blocked" "$(field "$out" VERDICT)" "T13a: live --number mode fetches the issue's linked PRs and computes VERDICT"
assert_contains_hash="$(field "$out" CONCLUSION_HASH)"
assert_ne "" "$assert_contains_hash" "T13b: live --number mode emits a non-empty CONCLUSION_HASH"

jq -n '{number: 20,state: "OPEN"}' >"$STUB_DIR/issue-20.json"
jq -n '{number: 22, state: "CLOSED"}' >"$STUB_DIR/issue-22.json"
p="$("$TARGET_SCRIPT" operator-premise --refs "20 22" --repo owner/repo)"
assert_eq "stale-premise" "$(field "$p" VERDICT)" "T13c: live operator-premise mode checks each --refs number's state"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
