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
# its own, change the hash. T16-T18 are the #7362 regression tests — a linked
# PR's FULL label set used to be folded into BLOCKERS, so ordinary
# review-cycle label churn (`loom:pr` <-> `loom:review-requested` <->
# `loom:reviewing` <-> `loom:operator` <-> `loom:treating`) produced 28+
# near-duplicate re-check comments on #6805 in 36 hours despite an unchanged
# verdict; the fingerprint now tracks only whether a superseding-block label
# (`loom:changes-requested`/`loom:blocked`) is present, plus the merge-state
# bucket.
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

# Pin the loom-daemon this suite tests against — the subject is a thin stub over
# `loom-daemon dep-recheck-fingerprint` now (epic #7810 PR 4). FATAL, not SKIP:
# see the helper for why.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "dep-recheck-fingerprint"

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
rc=0
"$TARGET_SCRIPT" extract-refs >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T0e: extract-refs also requires --number or --stdin"

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

# T5c (#7362): a SECOND, redundant superseding-block label alongside an
# already-present one (loom:blocked added on top of loom:changes-requested)
# does NOT change CONCLUSION_HASH -- the label component only tracks whether
# *any* superseding-block label is present, not the full label set, so both
# fixtures bucket to the same "block-label" state.
FIXTURE_LABEL_ADDED='{"prs":[{"number":4743,"state":"OPEN","labels":["loom:changes-requested","loom:blocked"],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}]}'
out_label_added="$(echo "$FIXTURE_LABEL_ADDED" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "$(field "$out1" CONCLUSION_HASH)" "$(field "$out_label_added" CONCLUSION_HASH)" \
    "T5c: a second, redundant superseding-block label does NOT change CONCLUSION_HASH (#7362 narrowed fingerprint)"

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
# NOTE: this is the REAL, unflattened shape `gh pr view --json labels`
# actually returns — an array of label OBJECTS, not plain strings. Do NOT
# pre-flatten this in the test stub (that masked the #7304 regression: the
# real _fetch_dep_recheck_json() never flattened labels, but this stub used
# to do the flattening for it, so the live-mode test validated a shape the
# script doesn't actually produce).
jq -n '{number: 4743, state: "OPEN", labels: [{id:"x", name:"loom:changes-requested", color:"ABCDEF"}], mergeable: "CONFLICTING", mergeStateStatus: "CONFLICTING"}' \
    >"$STUB_DIR/pr-4743.json"

out="$("$TARGET_SCRIPT" dep-recheck --number 6335 --repo owner/repo)"
assert_eq "blocked" "$(field "$out" VERDICT)" "T13a: live --number mode fetches the issue's linked PRs and computes VERDICT"
assert_contains_hash="$(field "$out" CONCLUSION_HASH)"
assert_ne "" "$assert_contains_hash" "T13b: live --number mode emits a non-empty CONCLUSION_HASH"

# --- T13d/T13e: THE #7304 REGRESSION - a labeled, CONFLICTING PR fetched via
# the real gh label-object shape must not crash jq, and the label must
# actually be recognized by _dep_recheck_verdict (not silently ignored).
jq -n '{closedByPullRequestsReferences: [{number: 9999}]}' >"$STUB_DIR/issue-6336.json"
jq -n '{number: 9999, state: "OPEN", labels: [{id:"a", name:"loom:blocked", color:"111111"}, {id:"b", name:"loom:pr", color:"222222"}], mergeable: "MERGEABLE", mergeStateStatus: "CLEAN"}' \
    >"$STUB_DIR/pr-9999.json"

out_labeled="$("$TARGET_SCRIPT" dep-recheck --number 6336 --repo owner/repo)"
assert_eq "blocked" "$(field "$out_labeled" VERDICT)" \
    "T13d: a labeled (loom:blocked), non-conflicting, real-shape PR is still VERDICT=blocked (label match works on real gh objects, not just the --stdin fixture shape)"
assert_eq "9999:OPEN:block-label:mergeable" "$(field "$out_labeled" BLOCKERS)" \
    "T13e: BLOCKERS renders the narrowed block-label/merge-bucket fingerprint (#7362) from the real gh label-object shape without a jq type error"

jq -n '{number: 20,state: "OPEN"}' >"$STUB_DIR/issue-20.json"
jq -n '{number: 22, state: "CLOSED"}' >"$STUB_DIR/issue-22.json"
p="$("$TARGET_SCRIPT" operator-premise --refs "20 22" --repo owner/repo)"
assert_eq "stale-premise" "$(field "$p" VERDICT)" "T13c: live operator-premise mode checks each --refs number's state"

# T13f (#8011): a non-numeric --refs token is a hard error (exit 1), not a
# silent drop. The shell original iterated `for ref in $REFS_ARG`, tried
# `gh issue view`/`gh pr view` on each raw token, and `_die`d 1 the moment a
# lookup failed both ways -- which a non-numeric token always would. A prior
# Rust port's `.filter_map(|t| t.parse().ok())` instead silently dropped it,
# succeeding with exit 0 and a fingerprint computed over fewer references
# than asked for.
rc=0
out="$("$TARGET_SCRIPT" operator-premise --refs "abc" --repo owner/repo 2>/dev/null)" || rc=$?
assert_eq "1" "$rc" "T13f: a non-numeric --refs token exits 1 rather than silently dropping (#8011)"
assert_eq "" "$out" "T13f: no VERDICT/REFS/CONCLUSION_HASH is emitted on the error path"

# --- T14: named-dependency - a `## Dependencies` checklist item naming a
# different, non-closing issue/PR as a prerequisite (#7314, the #6335/#6333
# shape `dep-recheck` cannot see: #6333 never carries `Closes #6335`) --------

# T14a: a single unchecked dependency still OPEN -> VERDICT=blocked
out="$(echo '{"deps":[{"number":6333,"checked":false,"state":"OPEN"}]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
assert_eq "blocked" "$(field "$out" VERDICT)" "T14a: a single unchecked, still-OPEN named dependency is VERDICT=blocked"
assert_eq "6333:OPEN" "$(field "$out" DEPS)" "T14a: DEPS renders the ref number and its live state"

# T14b: a single unchecked dependency that has MERGED -> VERDICT=clear
out="$(echo '{"deps":[{"number":6333,"checked":false,"state":"MERGED"}]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
assert_eq "clear" "$(field "$out" VERDICT)" "T14b: a MERGED named dependency is VERDICT=clear"

# T14c: a single unchecked dependency CLOSED without merging -> VERDICT=clear
# (matches curator.md's "When Dependencies Complete" treatment of a closed
# reference: closed-without-merging still counts as resolved).
out="$(echo '{"deps":[{"number":6333,"checked":false,"state":"CLOSED"}]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
assert_eq "clear" "$(field "$out" VERDICT)" "T14c: a CLOSED (not merged) named dependency still counts as clear"

# T14d: multiple named dependencies, only some resolved -> VERDICT=blocked
# until every one of them is resolved.
out="$(echo '{"deps":[{"number":1,"checked":false,"state":"MERGED"},{"number":2,"checked":false,"state":"OPEN"}]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
assert_eq "blocked" "$(field "$out" VERDICT)" "T14d: mixed dependencies (one resolved, one still open) is VERDICT=blocked"
out="$(echo '{"deps":[{"number":1,"checked":false,"state":"MERGED"},{"number":2,"checked":false,"state":"CLOSED"}]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
assert_eq "clear" "$(field "$out" VERDICT)" "T14d: mixed dependencies all resolved (one MERGED, one CLOSED) is VERDICT=clear"

# T14e: a checked checklist item is treated as already resolved regardless of
# its (never-consulted) state, and renders as "<ref>:checked".
out="$(echo '{"deps":[{"number":1,"checked":true,"state":"OPEN"}]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
assert_eq "clear" "$(field "$out" VERDICT)" "T14e: a checked dependency never blocks, even if its (unused) state says OPEN"
assert_eq "1:checked" "$(field "$out" DEPS)" "T14e: DEPS renders a checked dependency as '<ref>:checked'"

# T14f: no named dependencies at all -> VERDICT=clear, empty DEPS (mirrors
# dep-recheck's "empty prs -> clear" default).
out="$(echo '{"deps":[]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
assert_eq "clear" "$(field "$out" VERDICT)" "T14f: an empty deps list defaults to VERDICT=clear"
assert_eq "" "$(field "$out" DEPS)" "T14f: DEPS is empty when there are no named dependencies"

# T14g: label churn on a referenced OPEN PR (loom:pr/loom:review-requested/
# loom:changes-requested/loom:merge-conflict/loom:operator, etc.) is
# irrelevant to this subcommand — it only ever looks at `state`, never
# `labels`, and the --stdin shape doesn't even carry a labels field.
out1="$(echo '{"deps":[{"number":6333,"checked":false,"state":"OPEN"}]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
out2="$(echo '{"deps":[{"number":6333,"checked":false,"state":"OPEN"}]}' | "$TARGET_SCRIPT" named-dependency --stdin)"
assert_eq "$(field "$out1" CONCLUSION_HASH)" "$(field "$out2" CONCLUSION_HASH)" \
    "T14g: identical named-dependency input twice produces an identical hash"

# --- T15: named-dependency live --number mode (stubbed gh) - parses the
# issue body's own `## Dependencies` checklist and looks up each unchecked
# reference's live state -----------------------------------------------------
jq -n '{body: "## Dependencies\n\n- [ ] #6333: prerequisite feature\n- [x] #100: already done\n\n## Other Section\n\n- [ ] #999: not a dependency, different section entirely\n"}' \
    >"$STUB_DIR/issue-6335.json"
jq -n '{state: "OPEN"}' >"$STUB_DIR/issue-6333.json"

out="$("$TARGET_SCRIPT" named-dependency --number 6335 --repo owner/repo)"
assert_eq "blocked" "$(field "$out" VERDICT)" \
    "T15a: live --number mode parses the body's Dependencies checklist and reports VERDICT=blocked while the named ref is still OPEN"
# DEPS can span multiple lines (one per named dependency); `field()` above
# only returns the first, so extract the full multi-line block directly.
deps="$(printf '%s\n' "$out" | sed -n '/^DEPS=/,/^CONCLUSION_HASH=/p' | sed '$d' | sed 's/^DEPS=//')"
assert_eq "$(printf '100:checked\n6333:OPEN')" "$deps" \
    "T15b: DEPS includes both the checked (#100) and unchecked-but-open (#6333) entries, and excludes #999 from an unrelated section"

# T15c: once the named dependency itself is merged, live mode reports clear.
jq -n '{state: "MERGED"}' >"$STUB_DIR/pr-6333.json"
rm -f "$STUB_DIR/issue-6333.json"
out_merged="$("$TARGET_SCRIPT" named-dependency --number 6335 --repo owner/repo)"
assert_eq "clear" "$(field "$out_merged" VERDICT)" \
    "T15c: live mode falls back to gh pr view when the reference is a PR (not an issue), and reports VERDICT=clear once merged"

# T15d: a `PR #N (closes #M): ...` checklist item shape (#7501) — the exact
# phrasing that on #7498 caused DEPS to come back empty and VERDICT=clear
# even though the named PR was still OPEN. A `PR `/`Issue ` token before the
# `#N` must not cause the item to be silently dropped.
jq -n '{body: "## Dependencies\n\n- [ ] PR #7496 (closes #7495): must merge before this issue is buildable.\n"}' \
    >"$STUB_DIR/issue-7498.json"
jq -n '{state: "OPEN"}' >"$STUB_DIR/pr-7496.json"

out_pr_prefix="$("$TARGET_SCRIPT" named-dependency --number 7498 --repo owner/repo)"
assert_eq "blocked" "$(field "$out_pr_prefix" VERDICT)" \
    "T15d: a '- [ ] PR #N (closes #M): ...' checklist item is parsed into DEPS, not silently dropped, so a still-OPEN named PR reports VERDICT=blocked (#7501)"
assert_eq "7496:OPEN" "$(field "$out_pr_prefix" DEPS)" \
    "T15d: DEPS reports the PR-prefixed reference (#7496), not the parenthetical closes-target (#7495)"

# T15e: an `### Dependencies` (H3) heading — as filed by Curator on #7498 —
# must be recognized the same as `## Dependencies` (H2), instead of being
# silently skipped and reporting a false VERDICT=clear (#7503).
jq -n '{body: "### Dependencies\n\n- [ ] #7496: must merge before this issue is buildable.\n\n### Other Section\n\n- [ ] #999: not a dependency, different section entirely\n"}' \
    >"$STUB_DIR/issue-7498.json"
jq -n '{state: "OPEN"}' >"$STUB_DIR/issue-7496.json"
out_h3="$("$TARGET_SCRIPT" named-dependency --number 7498 --repo owner/repo)"
assert_eq "blocked" "$(field "$out_h3" VERDICT)" \
    "T15e: an H3 '### Dependencies' heading is recognized just like H2, reporting VERDICT=blocked instead of a false clear (#7503)"
assert_eq "7496:OPEN" "$(field "$out_h3" DEPS)" \
    "T15e: DEPS includes the H3-section dependency and excludes #999 from an unrelated H3 section"

# T15f (#8011): a `*` bullet, not just `-`, is accepted for a checklist item.
# `* [ ] #N: ...` is valid GitHub task-list syntax used in hand-written
# Curator checklists; the pre-port shell matched only a literal `-` bullet,
# so this fixture pins the Rust port's intentionally broader match (kept, not
# narrowed — see loom-daemon/src/dep_recheck/named.rs's `item_re` doc).
jq -n '{body: "## Dependencies\n\n* [ ] #123: asterisk bullet\n"}' \
    >"$STUB_DIR/issue-8011.json"
jq -n '{state: "OPEN"}' >"$STUB_DIR/issue-123.json"
out_star_bullet="$("$TARGET_SCRIPT" named-dependency --number 8011 --repo owner/repo)"
assert_eq "blocked" "$(field "$out_star_bullet" VERDICT)" \
    "T15f: a '* [ ] #N: ...' checklist item (asterisk bullet) is parsed into DEPS, reporting VERDICT=blocked for a still-OPEN reference (#8011)"
assert_eq "123:OPEN" "$(field "$out_star_bullet" DEPS)" \
    "T15f: DEPS reports the asterisk-bulleted reference"
rm -f "$STUB_DIR/issue-123.json"

# --- T16: dep-recheck - narrowed label fingerprint (#7362): a pure label flip
# among loom:pr/loom:review-requested/loom:reviewing/loom:operator/loom:treating
# — none of them a superseding-block label — with no merge-state change must
# NOT change CONCLUSION_HASH -------------------------------------------------
BASE_MERGEABLE='"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"'
F_PR='{"prs":[{"number":6817,"state":"OPEN","labels":["loom:pr"],'"$BASE_MERGEABLE"'}]}'
F_REVIEW='{"prs":[{"number":6817,"state":"OPEN","labels":["loom:review-requested","loom:reviewing"],'"$BASE_MERGEABLE"'}]}'
F_OPERATOR='{"prs":[{"number":6817,"state":"OPEN","labels":["loom:operator"],'"$BASE_MERGEABLE"'}]}'
F_TREATING='{"prs":[{"number":6817,"state":"OPEN","labels":["loom:review-requested","loom:treating"],'"$BASE_MERGEABLE"'}]}'
out_pr="$(echo "$F_PR" | "$TARGET_SCRIPT" dep-recheck --stdin)"
out_review="$(echo "$F_REVIEW" | "$TARGET_SCRIPT" dep-recheck --stdin)"
out_operator="$(echo "$F_OPERATOR" | "$TARGET_SCRIPT" dep-recheck --stdin)"
out_treating="$(echo "$F_TREATING" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "$(field "$out_pr" CONCLUSION_HASH)" "$(field "$out_review" CONCLUSION_HASH)" \
    "T16a: loom:pr -> loom:review-requested+loom:reviewing (no superseding label, no merge-state change) leaves CONCLUSION_HASH unchanged (#7362)"
assert_eq "$(field "$out_pr" CONCLUSION_HASH)" "$(field "$out_operator" CONCLUSION_HASH)" \
    "T16b: loom:pr -> loom:operator (Champion merge-risk hold, no superseding label) leaves CONCLUSION_HASH unchanged"
assert_eq "$(field "$out_pr" CONCLUSION_HASH)" "$(field "$out_treating" CONCLUSION_HASH)" \
    "T16c: loom:pr -> loom:review-requested+loom:treating (Doctor cycle, no superseding label) leaves CONCLUSION_HASH unchanged"
assert_eq "clear" "$(field "$out_pr" VERDICT)" \
    "T16d: none of loom:pr/loom:review-requested/loom:reviewing/loom:operator/loom:treating is a superseding-block label on its own"

# --- T17: a superseding-block label newly appearing/disappearing DOES change
# CONCLUSION_HASH, even measured against the same base fixture as T16 --------
F_CHANGES_REQUESTED='{"prs":[{"number":6817,"state":"OPEN","labels":["loom:changes-requested"],'"$BASE_MERGEABLE"'}]}'
F_BLOCKED_LABEL='{"prs":[{"number":6817,"state":"OPEN","labels":["loom:blocked"],'"$BASE_MERGEABLE"'}]}'
out_cr="$(echo "$F_CHANGES_REQUESTED" | "$TARGET_SCRIPT" dep-recheck --stdin)"
out_blocked_label="$(echo "$F_BLOCKED_LABEL" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_ne "$(field "$out_pr" CONCLUSION_HASH)" "$(field "$out_cr" CONCLUSION_HASH)" \
    "T17a: loom:changes-requested newly appearing (a superseding-block label) changes CONCLUSION_HASH"
assert_eq "blocked" "$(field "$out_cr" VERDICT)" "T17b: loom:changes-requested alone is VERDICT=blocked"
assert_eq "$(field "$out_cr" CONCLUSION_HASH)" "$(field "$out_blocked_label" CONCLUSION_HASH)" \
    "T17c: loom:changes-requested and loom:blocked are both superseding-block labels -> same bucket, same hash despite different label text"
out_pr_again="$(echo "$F_PR" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_eq "$(field "$out_pr" CONCLUSION_HASH)" "$(field "$out_pr_again" CONCLUSION_HASH)" \
    "T17d: the superseding-block label disappearing again (back to loom:pr only) returns to the original hash"

# --- T18: mergeable/mergeStateStatus crossing the conflicting/clean boundary
# changes CONCLUSION_HASH even with labels held constant ---------------------
F_PR_CONFLICTING='{"prs":[{"number":6817,"state":"OPEN","labels":["loom:pr"],"mergeable":"CONFLICTING","mergeStateStatus":"CONFLICTING"}]}'
out_pr_conflicting="$(echo "$F_PR_CONFLICTING" | "$TARGET_SCRIPT" dep-recheck --stdin)"
assert_ne "$(field "$out_pr" CONCLUSION_HASH)" "$(field "$out_pr_conflicting" CONCLUSION_HASH)" \
    "T18a: mergeable MERGEABLE/CLEAN -> CONFLICTING (labels unchanged) changes CONCLUSION_HASH"
assert_eq "blocked" "$(field "$out_pr_conflicting" VERDICT)" \
    "T18b: CONFLICTING merge state alone (no superseding label) is still VERDICT=blocked"

shell_refs() {
    printf '%s\n' "$1" | bash -euc 'eval "$(cat)"; printf "%s" "$REFS"'
}

# --- T19-T26: extract-refs (#4963) - the "Extracting the stated reference"
# extraction behind curator.md's "Checking Operator-Only Premises" section.
# The regression under test is the #4507 self-perpetuation loop: the old
# inline `[.body] + [.comments[].body]` shell scanned comment history
# unconditionally, so the bot's own "premise possibly stale" comment (which
# quotes the matched phrase back into the thread) became new input the next
# pass's extraction re-matched — indefinitely, even after the body itself was
# fixed. extract-refs closes this by scanning the body always, but a comment
# only when it is neither authored by the automation identity nor carrying
# this file's own curator marker.
# ------------------------------------------------------------------------

# T19: THE #4963 REGRESSION - reproduction of the exact #4507 shape: body has
# zero matches (already fixed to "Sequenced after #4510 (both now closed)"),
# comment history has N automation-authored heartbeat comments (carrying the
# `curator:operator-premise-recheck:` marker) quoting the already-diagnosed
# false-positive phrase "Depends on #4510" back into the thread. Must extract
# ZERO references -- not re-match the bot's own historical report comments.
FIXTURE_4507="$(jq -n '{
    body: "Sequenced after #4510 (both now closed)",
    comments: [
        {author: {login: "loom-fleet-dispatch"}, body: "**Operator-parked, premise possibly stale**: the reference this issue is parked on, #4510 (\"Depends on #4510\"), is now **closed**. <!-- curator:operator-premise-recheck:aaaa1111 -->"},
        {author: {login: "loom-fleet-dispatch"}, body: "**Operator-parked, premise possibly stale**: the reference this issue is parked on, #4510 (\"Depends on #4510\"), is now **closed**. <!-- curator:operator-premise-recheck:aaaa1111 -->"},
        {author: {login: "loom-fleet-dispatch"}, body: "**Operator-parked, premise possibly stale**: the reference this issue is parked on, #4510 (\"Depends on #4510\"), is now **closed**. <!-- curator:operator-premise-recheck:aaaa1111 -->"}
    ]
}')"
out="$(echo "$FIXTURE_4507" | "$TARGET_SCRIPT" extract-refs --stdin)"
assert_eq "" "$(shell_refs "$out")" \
    "T19: #4507 shape (body already fixed, N automation heartbeat comments quoting the stale phrase) extracts zero references (#4963)"

# T20: a genuine NEW human-authored "Blocked by #N" comment (different login,
# no marker) IS still detected -- the fix must not blind the check to
# comment-sourced references entirely.
FIXTURE_HUMAN="$(jq -n '{
    body: "no blockers in the body",
    comments: [
        {author: {login: "some-human-operator"}, body: "Actually, Blocked by #321 now."}
    ]
}')"
out="$(echo "$FIXTURE_HUMAN" | "$TARGET_SCRIPT" extract-refs --stdin)"
assert_eq "321" "$(shell_refs "$out")" "T20: a genuine new human-authored 'Blocked by #N' comment is still detected"

# T21: login-based exclusion alone (no marker present) still excludes an
# automation-authored comment.
FIXTURE_NO_MARKER="$(jq -n '{
    body: "no blockers in the body",
    comments: [
        {author: {login: "loom-fleet-dispatch"}, body: "Depends on #55, restating without the marker this time."}
    ]
}')"
out="$(echo "$FIXTURE_NO_MARKER" | "$TARGET_SCRIPT" extract-refs --stdin)"
assert_eq "" "$(shell_refs "$out")" "T21: an automation-authored comment is excluded even without the marker (login match alone suffices)"

# T22: marker-based exclusion alone (different login) still excludes a
# comment carrying the curator marker (belt-and-suspenders, per #4963 AC).
FIXTURE_MARKER_DIFF_LOGIN="$(jq -n '{
    body: "no blockers in the body",
    comments: [
        {author: {login: "some-other-login"}, body: "Depends on #66 <!-- curator:dep-recheck:deadbeef -->"}
    ]
}')"
out="$(echo "$FIXTURE_MARKER_DIFF_LOGIN" | "$TARGET_SCRIPT" extract-refs --stdin)"
assert_eq "" "$(shell_refs "$out")" "T22: a comment carrying the curator marker is excluded even under a different login (belt-and-suspenders)"

# T23: a body-only reference is found with no comments at all.
FIXTURE_BODY_ONLY='{"body": "This issue is Blocked by #42.", "comments": []}'
out="$(echo "$FIXTURE_BODY_ONLY" | "$TARGET_SCRIPT" extract-refs --stdin)"
assert_eq "42" "$(shell_refs "$out")" "T23: a body-only reference is found with no comments at all"

# T24: --bot-login overrides the default automation identity.
FIXTURE_CUSTOM_BOT='{"body": "no blockers", "comments": [{"author": {"login": "my-custom-bot"}, "body": "Depends on #88"}]}'
out_default="$(echo "$FIXTURE_CUSTOM_BOT" | "$TARGET_SCRIPT" extract-refs --stdin)"
assert_eq "88" "$(shell_refs "$out_default")" "T24a: a non-default automation login is NOT excluded without --bot-login"
out_custom="$(echo "$FIXTURE_CUSTOM_BOT" | "$TARGET_SCRIPT" extract-refs --stdin --bot-login my-custom-bot)"
assert_eq "" "$(shell_refs "$out_custom")" "T24b: --bot-login excludes the named identity's comments"

# T25: login match is case-insensitive and tolerant of an 'app/' prefix or a
# '[bot]' suffix, since different `gh` views/API paths normalize a GitHub
# App's login differently.
FIXTURE_LOGIN_VARIANTS="$(jq -n '{
    body: "no blockers",
    comments: [
        {author: {login: "app/loom-fleet-dispatch"}, body: "Depends on #91"},
        {author: {login: "Loom-Fleet-Dispatch[bot]"}, body: "Depends on #92"}
    ]
}')"
out="$(echo "$FIXTURE_LOGIN_VARIANTS" | "$TARGET_SCRIPT" extract-refs --stdin)"
assert_eq "" "$(shell_refs "$out")" "T25: login match tolerates an 'app/' prefix and a '[bot]' suffix, case-insensitively"

# T25b (#8011): a declared phrase and its `#N` may be split across a line
# break. The pre-port shell matched line-oriented (`grep -oE`), so it could
# never see this; the Rust port's pattern runs over the whole text and `\s`
# matches `\n`, so it does. Kept intentionally (same worse-failure-direction
# reasoning as named-dependency's bullet-marker divergence) rather than
# narrowed back to line-oriented matching.
FIXTURE_NEWLINE_SPANNING='{"body": "Blocked by\n#42", "comments": []}'
out="$(echo "$FIXTURE_NEWLINE_SPANNING" | "$TARGET_SCRIPT" extract-refs --stdin)"
assert_eq "42" "$(shell_refs "$out")" \
    "T25b: a phrase and its #N split across a newline is still matched (#8011, kept intentionally)"

# T25c (#8011): --bot-login normalises the SAME way (lower-case, strip a
# leading 'app/' or trailing '[bot]') on both the flag's own value and the
# comment author, so a caller may pass either spelling. The pre-port shell
# only normalised the comment author's side, so passing the 'app/'-prefixed
# spelling here would never have matched a bare-login comment author (a bug
# the port fixes, not a regression to preserve).
FIXTURE_BOT_LOGIN_APP_PREFIX='{"body": "no blockers", "comments": [{"author": {"login": "loom-fleet-dispatch"}, "body": "Depends on #93"}]}'
out="$(echo "$FIXTURE_BOT_LOGIN_APP_PREFIX" | "$TARGET_SCRIPT" extract-refs --stdin --bot-login "app/loom-fleet-dispatch")"
assert_eq "" "$(shell_refs "$out")" \
    "T25c: --bot-login \"app/<login>\" excludes a bare-login comment author, symmetric normalisation (#8011, arguably a fix over the shell)"

# T26: live --number mode (stubbed gh) reproduces the #4507 shape end to end
# via `gh issue view --json body,comments`.
jq -n '{
    body: "Sequenced after #4510 (both now closed)",
    comments: [
        {author: {login: "loom-fleet-dispatch"}, body: "premise possibly stale: Depends on #4510 <!-- curator:operator-premise-recheck:aaaa1111 -->"}
    ]
}' >"$STUB_DIR/issue-4507.json"
out="$("$TARGET_SCRIPT" extract-refs --number 4507 --repo owner/repo)"
assert_eq "" "$(shell_refs "$out")" "T26a: live --number mode reproduces the #4507 shape end to end (zero refs via gh issue view body,comments)"

jq -n '{
    body: "Blocked by #200",
    comments: []
}' >"$STUB_DIR/issue-4508.json"
out="$("$TARGET_SCRIPT" extract-refs --number 4508 --repo owner/repo)"
assert_eq "200" "$(shell_refs "$out")" "T26b: live --number mode still finds a genuine body reference"

# Execute the documented shell consumer, rather than merely inspecting fields.
consume_extract_refs() {
    "$TARGET_SCRIPT" extract-refs --stdin | bash -euc '
        REFS=previous-value
        eval "$(cat)"
        printf "%s" "$REFS"
    '
}
for fixture_expected in 'none|' 'Blocked by #42|42' 'Blocked by #42. Requires #43.|42 43'; do
    fixture="${fixture_expected%%|*}"
    expected="${fixture_expected#*|}"
    rc=0
    actual="$(jq -n --arg body "$fixture" '{body:$body,comments:[]}' | consume_extract_refs)" || rc=$?
    assert_eq "0" "$rc" "T27: eval consumer succeeds for '$fixture'"
    assert_eq "$expected" "$actual" "T27: eval consumer retains all references for '$fixture'"
done
rc=0
# The shell-looking issue text must stay literal input, never a command.
# shellcheck disable=SC2016
actual="$(printf '%s' '{"body":"Requires #43; $(exit 91)","comments":[{"author":{"login":"human"},"body":"Blocked by #42; exit 92"}]}' | consume_extract_refs)" || rc=$?
assert_eq "0" "$rc" "T28: consumer accepts mixed body/comment refs without executing source text"
assert_eq "42 43" "$actual" "T28: only sorted numeric refs reach the consumer"

# The next documented consumer overwrites REFS with one number:state per line.
rc=0
actual="$(printf '%s' '{"refs":[{"number":42,"state":"OPEN"},{"number":43,"state":"CLOSED"}]}' |
    "$TARGET_SCRIPT" operator-premise --stdin | bash -euc 'eval "$(cat)"; printf "%s\n%s" "$VERDICT" "$REFS"')" || rc=$?
assert_eq "0" "$rc" "T29: operator-premise eval consumer retains multiline refs"
assert_eq $'stale-premise\n42:OPEN\n43:CLOSED' "$actual" "T29: both reference states survive eval"

# --- T30-T36: decide (#7617) - the "Four-way decision" that gates the
# `loom:curating` claim on whether this pass will actually mutate anything.
# THE #7617 REGRESSION: curator.md used to claim `loom:curating` before
# comparing CONCLUSION_HASH against the prior marker, then release it again
# once the comparison came back "unchanged" — claim/unclaim churn with zero
# work performed on a long-blocked issue re-checked over and over. `decide`
# is the shared, tested implementation of that comparison so the claim
# decision is no longer hand-rolled in prose per Curator pass (mirrors why
# `dep-recheck`/`operator-premise` themselves were extracted, #7281).
# ------------------------------------------------------------------------

# T30: THE #7617 REGRESSION ITSELF - same hash, well inside the heartbeat
# window -> ACTION=skip, CLAIM=false. This is the exact "no claim/unclaim
# label event" case the acceptance criteria calls for.
out="$("$TARGET_SCRIPT" decide --hash abc123 --prior-hash abc123 --prior-age-hours 1)"
assert_eq "skip" "$(field "$out" ACTION)" "T30a: same hash, inside the window -> ACTION=skip"
assert_eq "false" "$(field "$out" CLAIM)" "T30b: same hash, inside the window -> CLAIM=false (no loom:curating claim/unclaim at all)"

# T31: no prior marker at all (first-ever check) -> always report, and a
# report needs the claim.
out="$("$TARGET_SCRIPT" decide --hash abc123)"
assert_eq "comment" "$(field "$out" ACTION)" "T31a: no prior marker -> ACTION=comment (first-ever check always reports)"
assert_eq "true" "$(field "$out" CLAIM)" "T31b: no prior marker -> CLAIM=true"

# T32: a changed conclusion (a real state change, e.g. the tracked PR merged,
# or an escalation that folded a new orthogonal condition into the hash)
# always comments, no matter how fresh the prior marker is.
out="$("$TARGET_SCRIPT" decide --hash def456 --prior-hash abc123 --prior-age-hours 0)"
assert_eq "comment" "$(field "$out" ACTION)" "T32a: a changed hash -> ACTION=comment even immediately after the prior marker"
assert_eq "true" "$(field "$out" CLAIM)" "T32b: a changed hash -> CLAIM=true (this pass still claims normally before commenting, unchanged from today)"

# T33: same hash, past the heartbeat window -> exactly one refresh comment,
# which needs the claim.
out="$("$TARGET_SCRIPT" decide --hash abc123 --prior-hash abc123 --prior-age-hours 25)"
assert_eq "heartbeat" "$(field "$out" ACTION)" "T33a: same hash, past the 24h window -> ACTION=heartbeat"
assert_eq "true" "$(field "$out" CLAIM)" "T33b: same hash, past the window -> CLAIM=true"

# T33c: the window boundary itself (exactly the heartbeat threshold) counts
# as past the window, not inside it -- mirrors "at or past" in the header doc.
out="$("$TARGET_SCRIPT" decide --hash abc123 --prior-hash abc123 --prior-age-hours 24)"
assert_eq "heartbeat" "$(field "$out" ACTION)" "T33c: age exactly equal to the heartbeat window -> ACTION=heartbeat, not skip"

# T34: an empty --hash (nothing to report this pass, e.g. operator-premise's
# VERDICT=open) is a pure no-op regardless of any prior marker -- there is
# nothing to compare and therefore nothing to claim.
out="$("$TARGET_SCRIPT" decide --hash "" --prior-hash abc123 --prior-age-hours 1)"
assert_eq "none" "$(field "$out" ACTION)" "T34a: empty --hash -> ACTION=none"
assert_eq "false" "$(field "$out" CLAIM)" "T34b: empty --hash -> CLAIM=false"

# T35: --heartbeat-hours overrides the default window, and
# LOOM_DEP_RECHECK_HEARTBEAT_HOURS is honored when --heartbeat-hours is not
# passed explicitly.
out="$("$TARGET_SCRIPT" decide --hash abc123 --prior-hash abc123 --prior-age-hours 5 --heartbeat-hours 4)"
assert_eq "heartbeat" "$(field "$out" ACTION)" "T35a: --heartbeat-hours narrows the window (age 5h >= 4h -> heartbeat, not skip)"
out="$(LOOM_DEP_RECHECK_HEARTBEAT_HOURS=4 "$TARGET_SCRIPT" decide --hash abc123 --prior-hash abc123 --prior-age-hours 5)"
assert_eq "heartbeat" "$(field "$out" ACTION)" "T35b: LOOM_DEP_RECHECK_HEARTBEAT_HOURS is honored when --heartbeat-hours is omitted"

# T36: usage errors -- --hash is mandatory (even if empty, it must be passed
# explicitly), and --prior-age-hours is mandatory whenever --prior-hash is
# non-empty (there is nothing to age otherwise).
rc=0
"$TARGET_SCRIPT" decide >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T36a: decide without --hash is a usage error"
rc=0
"$TARGET_SCRIPT" decide --hash abc123 --prior-hash abc123 >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T36b: --prior-hash without --prior-age-hours is a usage error"

# T37: --json output for decide.
out="$("$TARGET_SCRIPT" decide --hash abc123 --prior-hash abc123 --prior-age-hours 1 --json)"
assert_eq "skip" "$(jq -r '.action' <<<"$out")" "T37a: --json reports action"
assert_eq "false" "$(jq -r '.claim' <<<"$out")" "T37b: --json reports claim as a JSON boolean"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
