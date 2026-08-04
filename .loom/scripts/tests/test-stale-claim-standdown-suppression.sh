#!/usr/bin/env bash
# test-stale-claim-standdown-suppression.sh - Regression guard for #5123.
#
# #5123 closed two gaps in the loom:reviewing / loom:treating / loom:curating
# stale-claim livelock protection:
#
#   1. judge.md's and doctor.md's "Fresh" stand-down path posted a new marked
#      `<!-- loom:standdown claim=$CLAIMED_AT -->` comment on EVERY pass, even
#      when the immediately preceding comment already carried the identical
#      marker for the same claim — pure noise (observed live on PR #5115: 3
#      near-identical stand-downs in 85 seconds).
#   2. loom:curating (Curator) had no TTL/marker/bounded-fallback mechanism at
#      all — a dead Curator claim was permanently stranded, the exact failure
#      shape already fixed for loom:reviewing/loom:treating.
#
# This suite has two parts:
#
#   A. Extracts the ACTUAL "Duplicate stand-down suppression" bash block from
#      judge.md, doctor.md, and curator.md (not a reimplementation) and runs
#      it against mocked `gh`/`jq` input, asserting: (a) an unchanged latest
#      marker suppresses the comment, (b) a stale/absent marker still posts
#      one. This means an edit that breaks the extracted block's syntax or
#      behavior fails this suite directly — no docs/test drift possible for
#      this specific code path.
#
#   B. A decision-table conformance check mirroring the Fresh/Stale/bounded-
#      fallback table shared by all three role prompts (the table itself is
#      prose, not executable, so this reimplements it once and asserts
#      boundary behavior: never stomp a fresh claim, TTL-reclaim once stale,
#      and the streak+age-floor join for the bounded fallback) — covering the
#      loom:curating port specifically, since that lane is new in this PR.
#
# Usage:
#   ./.loom/scripts/tests/test-stale-claim-standdown-suppression.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

JUDGE_MD="$REPO_ROOT/defaults/.claude/commands/loom/judge.md"
DOCTOR_MD="$REPO_ROOT/defaults/.claude/commands/loom/doctor.md"
CURATOR_MD="$REPO_ROOT/defaults/.claude/commands/loom/curator.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

for f in "$JUDGE_MD" "$DOCTOR_MD" "$CURATOR_MD"; do
    [[ -f "$f" ]] || { echo "FATAL: missing $f"; exit 1; }
done

# Extract a fenced ```bash ... ``` block: starts at the line containing
# $2 (must be inside the fence, right after the opening ```bash) and reads
# through (but excludes) the next line that is exactly ```.
extract_block() { # <file> <start-substring>
    local file="$1" start="$2"
    awk -v start="$start" '
        index($0, start) { capture=1 }
        capture {
            if ($0 == "```") exit
            print
        }
    ' "$file"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ============================================================================
# Part A: extracted duplicate-suppression blocks (real code under test)
# ============================================================================

echo "Part A: duplicate stand-down comment suppression (extracted from role prompts)"

# <lane-name> <file> <gh-subcommand-that-must-fire e.g. "gh pr comment"/"gh issue comment">
run_suppression_case() {
    local lane="$1" file="$2" gh_kind="$3" comments_json="$4" expect_comment="$5"
    local case_label="$6"

    local block
    block="$(extract_block "$file" 'LATEST_COMMENT_BODY=')"
    if [[ -z "$block" ]]; then
        fail "$lane ($case_label): could not extract the duplicate-suppression block from $file"
        return
    fi

    local gh_calls="$WORK/${lane}-gh-calls-$RANDOM"
    : > "$gh_calls"

    # Mock `gh` — records every invocation so we can assert whether a
    # comment was (or wasn't) posted, without touching the network.
    gh() {
        printf '%s\n' "gh $*" >> "$gh_calls"
    }
    export -f gh >/dev/null 2>&1 || true

    # These four are consumed by the extracted block below via `eval`, not
    # referenced directly in this function — shellcheck can't see that use.
    # shellcheck disable=SC2034
    N=12345
    CLAIMED_AT="2026-08-03T21:11:41Z"
    # shellcheck disable=SC2034
    MARKER="<!-- loom:standdown claim=$CLAIMED_AT -->"
    # shellcheck disable=SC2034
    COMMENTS_JSON="$comments_json"

    local out
    out="$(eval "$block" 2>&1)"
    local rc=$?

    if [[ "$rc" -ne 0 ]]; then
        fail "$lane ($case_label): extracted block exited non-zero (rc=$rc): $out"
        return
    fi

    local comment_posted=0
    grep -q "$gh_kind" "$gh_calls" 2>/dev/null && comment_posted=1

    if [[ "$expect_comment" == "yes" ]]; then
        if [[ "$comment_posted" -eq 1 ]]; then
            pass "$lane ($case_label): posted a stand-down comment as expected"
        else
            fail "$lane ($case_label): expected a stand-down comment, none was posted. gh calls: $(cat "$gh_calls" 2>/dev/null)"
        fi
    else
        if [[ "$comment_posted" -eq 0 ]]; then
            pass "$lane ($case_label): suppressed the duplicate comment as expected"
        else
            fail "$lane ($case_label): expected NO comment (duplicate suppression), but one was posted: $(cat "$gh_calls" 2>/dev/null)"
        fi
        if [[ "$out" == *"skipping duplicate comment"* ]]; then
            pass "$lane ($case_label): logged the skip reason"
        else
            fail "$lane ($case_label): did not log a skip reason. Got: $out"
        fi
    fi

    unset -f gh 2>/dev/null || true
}

MARKER_TXT='<!-- loom:standdown claim=2026-08-03T21:11:41Z -->'

# Case 1: latest (and only) comment already carries the identical marker ->
# suppress.
DUP_JSON=$(cat <<EOF
[{"created_at":"2026-08-03T21:12:31Z","body":"Judge pass: PR still carries a fresh claim. $MARKER_TXT"}]
EOF
)

# Case 2: no comments since the claim at all -> must still post (first
# stand-down for this claim, nothing to suppress).
EMPTY_JSON='[]'

# Case 3: a real (non-marker) comment is the latest -> must still post (this
# is genuine activity, not a repeat stand-down).
FRESH_JSON=$(cat <<EOF
[{"created_at":"2026-08-03T21:12:00Z","body":"Actually reviewing now, one moment."}]
EOF
)

run_suppression_case "judge"   "$JUDGE_MD"   "gh pr comment"    "$DUP_JSON"   "no"  "identical marker already latest"
run_suppression_case "judge"   "$JUDGE_MD"   "gh pr comment"    "$EMPTY_JSON" "yes" "no comments yet"
run_suppression_case "judge"   "$JUDGE_MD"   "gh pr comment"    "$FRESH_JSON" "yes" "latest comment is real activity"

run_suppression_case "doctor"  "$DOCTOR_MD"  "gh pr comment"    "$DUP_JSON"   "no"  "identical marker already latest"
run_suppression_case "doctor"  "$DOCTOR_MD"  "gh pr comment"    "$EMPTY_JSON" "yes" "no comments yet"
run_suppression_case "doctor"  "$DOCTOR_MD"  "gh pr comment"    "$FRESH_JSON" "yes" "latest comment is real activity"

run_suppression_case "curator" "$CURATOR_MD" "gh issue comment" "$DUP_JSON"   "no"  "identical marker already latest"
run_suppression_case "curator" "$CURATOR_MD" "gh issue comment" "$EMPTY_JSON" "yes" "no comments yet"
run_suppression_case "curator" "$CURATOR_MD" "gh issue comment" "$FRESH_JSON" "yes" "latest comment is real activity"

# ============================================================================
# Part B: loom:curating decision-table conformance (mirrors the doc's table)
# ============================================================================

echo ""
echo "Part B: loom:curating Fresh/Stale/bounded-fallback decision table"

# Mirrors the exact table added to curator.md's "Stale loom:curating Claim
# Check" (structurally identical to judge.md/doctor.md's). Kept in sync by
# hand with that table — if the table's conditions change, update this
# function to match.
decide_curating() { # <claim_age_min> <comments_after> <standdown_count> [ttl] [streak_cap]
    local age="$1" comments_after="$2" standdown_count="$3"
    local ttl="${4:-30}" streak_cap="${5:-3}"

    if [[ "$standdown_count" -ge "$streak_cap" && "$age" -ge "$ttl" ]]; then
        echo "stale-bounded-fallback"
    elif [[ "$age" -lt "$ttl" || "$comments_after" -gt 0 ]]; then
        echo "fresh"
    else
        echo "stale"
    fi
}

assert_decision() { # <label> <expected> <actual...>
    local label="$1" expected="$2"; shift 2
    local actual
    actual="$(decide_curating "$@")"
    if [[ "$actual" == "$expected" ]]; then
        pass "$label -> $expected"
    else
        fail "$label -> expected $expected, got $actual (inputs: $*)"
    fi
}

# Never stomp a genuinely fresh claim (age well under TTL, no other activity).
assert_decision "fresh: age=5m, no activity"                 "fresh"                 5 0 0
# Real (non-standdown) activity keeps it fresh even once past the TTL.
assert_decision "fresh: age=45m but real comment after claim" "fresh"                45 1 0
# Ordinary staleness: past TTL, zero non-standdown activity.
assert_decision "stale: age=31m, no activity"                 "stale"                 31 0 0
# Exact TTL boundary (age == ttl) must already be reclaimable, not fresh.
assert_decision "boundary: age==ttl exactly"                  "stale"                 30 0 0
assert_decision "boundary: age==ttl-1 is still fresh"         "fresh"                 29 0 0
# Bounded fallback: streak alone (claim still young) must NOT force-reclaim —
# this is the #4790 regression the age-floor join exists to prevent.
assert_decision "peer-arrival-rate alone (age=10m, streak=3) stays fresh" "fresh" 10 0 3
# Bounded fallback fires only once BOTH the streak cap and the age floor hold.
assert_decision "bounded fallback: age=30m AND streak=3"      "stale-bounded-fallback" 30 0 3
# One short of the streak cap, past TTL -> ordinary stale (not "fallback"),
# same outcome either way but exercises the boundary distinctly.
assert_decision "streak=2 (below cap), past TTL"              "stale"                 40 0 2

# Custom TTL / streak cap (repo override) still honored.
assert_decision "custom TTL=60: age=45m stays fresh"          "fresh"                 45 0 0 60 3
assert_decision "custom TTL=60: age=61m goes stale"           "stale"                 61 0 0 60 3

# ============================================================================
# Summary
# ============================================================================

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
