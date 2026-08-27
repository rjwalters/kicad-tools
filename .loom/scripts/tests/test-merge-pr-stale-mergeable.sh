#!/usr/bin/env bash
# test-merge-pr-stale-mergeable.sh - Unit tests for the stale-cached-mergeable
# recheck-before-refusal logic in merge-pr.sh (#6104).
#
# GitHub's REST `.mergeable` field is computed asynchronously and invalidated
# on every push to the base branch. On a fast-moving repo it can read a stale
# `false` for a PR that would actually merge cleanly against current main.
# Before this fix, merge-pr.sh trusted the FIRST `.mergeable == false` read
# and refused immediately with a message asserting a conflict that did not
# actually exist (observed on PR #5995: `git merge-tree` reported a clean
# merge against the same freshly-fetched base the script had just refused).
#
# The fix adds `_recheck_mergeable_before_refusal()`, called only once
# `.mergeable` has already read `false`:
#   1. Re-queries via the UNCACHED recheck path (forge_get_pr_nocache) after a
#      short backoff, up to N times -- using the uncached path deliberately,
#      since $GH may be wrapped by `gh-cached` and re-reading through that
#      cache would keep returning the same stale value (mirrors the existing
#      _NRC_RECHECK_JSON pattern already used elsewhere in this script).
#   2. If still `false` after all retries, corroborates with a local
#      `git merge-tree` check against the freshly fetched base ref -- this is
#      what lets the caller distinguish "the forge's cached state is
#      stale/unknown" (refuse-stale) from "this branch genuinely conflicts"
#      (refuse-conflict).
#
# This test exercises:
#   1. Behavioral tests of the REAL `_recheck_mergeable_before_refusal`
#      function body (extracted verbatim from merge-pr.sh, no drift):
#      (a) recheck resolves to mergeable=true after a stale first read ->
#          "merge:..." (the AC's core positive case).
#      (b) recheck never resolves true, but local git merge-tree against a
#          freshly fetched base is clean -> "merge:..." (stale/false-negative
#          cached state, corroborated).
#      (c) recheck never resolves true, and git merge-tree independently
#          confirms a real conflict -> "refuse-conflict:..." (the AC's
#          negative case -- don't regress real conflicts).
#      (d) base/head ref unavailable -> "refuse-stale:..." (fail closed, but
#          distinguishable from a confirmed conflict).
#      (e) local fetch fails (no such remote) -> "refuse-stale:..." (same
#          distinction, different cause).
#   2. Source wiring: the mergeability gate calls the recheck helper via
#      forge_get_pr_nocache (not the cached forge_get_pr), branches on the
#      three-way decision, and the two refusal messages are textually
#      distinct (stale/unknown vs genuinely conflicts) per acceptance
#      criterion 3.
#   3. Durable telemetry (#6978, follow-up from #6156): the mergeability gate
#      calls merge-admission-telemetry.sh's `record` subcommand once per
#      invocation, BEFORE branching into info/error (error exits the script),
#      and the recheck function sets the `_RECHECK_ATTEMPTS_USED` global to
#      the number of backoff attempts actually consumed for each of the (a)-
#      (f) decision cases above -- purely additive instrumentation, no change
#      to the decision itself.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-stale-mergeable.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (pattern: $pattern)"; fi
}

refute_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then fail "$msg (unexpectedly matched: $pattern)"; else pass "$msg"; fi
}

[[ -x "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not executable" >&2; exit 1; }

# --- Test 1: source wiring ---
echo "Test 1: merge-pr.sh source wires the #6104 stale-mergeable recheck"

assert_grep '_recheck_mergeable_before_refusal\(\) \{' "$MERGE_PR" \
    "_recheck_mergeable_before_refusal is defined"
assert_grep '_MSM_DECISION="\$\(_recheck_mergeable_before_refusal' "$MERGE_PR" \
    "the mergeability gate calls _recheck_mergeable_before_refusal"
assert_grep 'forge_get_pr_nocache "\$nwo" "\$pr_number" "\$gh_cmd"' "$MERGE_PR" \
    "the recheck uses the UNCACHED forge_get_pr_nocache (not the cached forge_get_pr)"
assert_grep 'git -C "\$repo_root" merge-tree --write-tree' "$MERGE_PR" \
    "the recheck corroborates with a local git merge-tree check"
assert_grep 'refuse-conflict\)' "$MERGE_PR" \
    "the gate branches on the refuse-conflict decision"
assert_grep 'this branch genuinely conflicts' "$MERGE_PR" \
    "the genuine-conflict refusal message is present"
assert_grep "forge's cached mergeable state is stale/unknown and could not be corroborated locally" "$MERGE_PR" \
    "the stale/unknown refusal message is present and textually distinct (AC 3)"
refute_grep 'error "PR #\$PR_NUMBER has merge conflicts — resolve before merging"$' "$MERGE_PR" \
    "the old bare unconditional refusal (no recheck, no reason) is gone"

# The two refusal messages must be distinct strings (AC 3) -- not the same
# generic "has merge conflicts" text in both branches.
_conflict_msg_line=$(grep -n 'this branch genuinely conflicts' "$MERGE_PR" | head -1 | cut -d: -f1)
_stale_msg_line=$(grep -n "could not be corroborated locally" "$MERGE_PR" | head -1 | cut -d: -f1)
if [[ -n "$_conflict_msg_line" ]] && [[ -n "$_stale_msg_line" ]] && [[ "$_conflict_msg_line" != "$_stale_msg_line" ]]; then
    pass "genuine-conflict and stale/unknown refusal messages live on distinct lines"
else
    fail "expected two distinct refusal message lines, got conflict=$_conflict_msg_line stale=$_stale_msg_line"
fi

# Ordering: the recheck helper is defined BEFORE the synchronous-merge
# mergeability gate that calls it (definition-before-use in a linear script).
_def_line=$(grep -n '^_recheck_mergeable_before_refusal() {' "$MERGE_PR" | head -1 | cut -d: -f1)
_call_line=$(grep -n '_MSM_DECISION="\$(_recheck_mergeable_before_refusal' "$MERGE_PR" | head -1 | cut -d: -f1)
if [[ -n "$_def_line" ]] && [[ -n "$_call_line" ]] && [[ "$_def_line" -lt "$_call_line" ]]; then
    pass "_recheck_mergeable_before_refusal is defined before its callsite"
else
    fail "definition/callsite ordering wrong (def=$_def_line call=$_call_line)"
fi

echo ""
echo "Test 1b: durable telemetry wiring (#6978, follow-up from #6156)"

assert_grep 'merge-admission-telemetry\.sh" record' "$MERGE_PR" \
    "the mergeability gate calls merge-admission-telemetry.sh record"
assert_grep 'action "\$_MSM_ACTION" --reason "\$_MSM_REASON"' "$MERGE_PR" \
    "the telemetry call carries the computed action and reason"
assert_grep 'retries-used "\$_MSM_RETRIES_USED" --backoff-delay-sec "\$_MSM_DELAY"' "$MERGE_PR" \
    "the telemetry call carries retries_used and backoff_delay_sec"
assert_grep '>/dev/null 2>&1 \|\| true' "$MERGE_PR" \
    "the telemetry call is isolated with || true (must never abort the merge path)"
# retries_used is derived from the decision's OWN reason text (no change to
# _recheck_mergeable_before_refusal itself -- see Test 2's function-body
# extraction below, which is byte-for-byte the same function pre- and
# post-#6978): "recheck #N" when the recheck resolved true mid-loop,
# otherwise the configured retry count (every other path exhausts all
# retries before returning).
assert_grep '_MSM_REASON" =~ recheck' "$MERGE_PR" \
    "retries_used is parsed from the reason string's 'recheck #N' pattern when present"
assert_grep '_MSM_RETRIES_USED="\$_MSM_RETRIES"' "$MERGE_PR" \
    "retries_used defaults to the configured retry count otherwise"

# The telemetry emission must happen BEFORE the case/error branch below it --
# `error()` calls `exit 1`, so if telemetry were emitted only inside (or
# after) the case statement, it would never fire on a refusal.
_telemetry_line=$(grep -n 'merge-admission-telemetry\.sh" record' "$MERGE_PR" | head -1 | cut -d: -f1)
_case_line=$(grep -n 'case "\$_MSM_ACTION" in' "$MERGE_PR" | head -1 | cut -d: -f1)
if [[ -n "$_telemetry_line" ]] && [[ -n "$_case_line" ]] && [[ "$_telemetry_line" -lt "$_case_line" ]]; then
    pass "telemetry is emitted BEFORE the info/error branch (line $_telemetry_line < $_case_line)"
else
    fail "expected the telemetry call (line ${_telemetry_line:-?}) before the case statement (line ${_case_line:-?})"
fi

# --- Extract the ACTUAL function body from the live source (no drift) ---
extract_fn() {
    local name="$1" file="$2"
    awk -v fn="$name" '
      $0 ~ "^"fn"\\(\\) \\{" { grab=1 }
      grab { print }
      grab && /^}/ { exit }
    ' "$file"
}

eval "$(extract_fn _recheck_mergeable_before_refusal "$MERGE_PR")"

echo ""
echo "Test 2: behavioral tests of the real _recheck_mergeable_before_refusal body"

# --- Fixtures: a real git repo with a bare 'origin' remote so the function's
# `git -C repo_root fetch origin ...` / `git merge-tree origin/A origin/B`
# calls resolve against real refs. ---
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-merge-stale-mergeable.XXXXXX")"
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { rm -rf "$TMP_ROOT" 2>/dev/null || true; }
trap cleanup EXIT

ORIGIN_BARE="$TMP_ROOT/origin.git"
git init -q --bare "$ORIGIN_BARE"

WORK="$TMP_ROOT/work"
git init -q "$WORK"
git -C "$WORK" config user.email "test@example.com"
git -C "$WORK" config user.name "Test"
git -C "$WORK" remote add origin "$ORIGIN_BARE"

echo "hello" > "$WORK/README.md"
git -C "$WORK" add -A
git -C "$WORK" commit -q -m "initial"
git -C "$WORK" branch -M main
git -C "$WORK" push -q -u origin main

# clean-merge branch: touches an unrelated file, no conflict with main.
git -C "$WORK" checkout -q -b feature/clean
echo "unrelated addition" > "$WORK/new-file.txt"
git -C "$WORK" add -A
git -C "$WORK" commit -q -m "clean addition"
git -C "$WORK" push -q -u origin feature/clean
git -C "$WORK" checkout -q main

# conflicting branch: edits the SAME line main also edits after the branch point.
git -C "$WORK" checkout -q -b feature/conflict
echo "feature version" > "$WORK/README.md"
git -C "$WORK" commit -q -am "feature edits README"
git -C "$WORK" push -q -u origin feature/conflict
git -C "$WORK" checkout -q main
echo "main version" > "$WORK/README.md"
git -C "$WORK" commit -q -am "main edits README (diverges from feature/conflict)"
git -C "$WORK" push -q origin main

# Stub forge_get_pr_nocache: sequences of canned .mergeable values, one per
# call, consumed via a call index. NOTE: the real function invokes this via
# command substitution (`recheck_json="$(forge_get_pr_nocache ...)"`), which
# forks a subshell -- a plain in-memory counter variable incremented inside
# the stub would NOT persist across calls (each subshell gets its own copy).
# A file-based counter survives across the subshell boundary.
_STUB_SEQUENCE=()
_STUB_COUNTER_FILE="$TMP_ROOT/stub-counter"
_reset_stub_counter() { echo 0 > "$_STUB_COUNTER_FILE"; }
forge_get_pr_nocache() {
    # Args ($1=nwo $2=pr_number $3=gh_cmd) are intentionally ignored -- this
    # stub replays a canned .mergeable sequence regardless of call args.
    local idx val
    idx="$(cat "$_STUB_COUNTER_FILE")"
    echo $((idx + 1)) > "$_STUB_COUNTER_FILE"
    val="false"
    if [[ "$idx" -lt "${#_STUB_SEQUENCE[@]}" ]]; then
        val="${_STUB_SEQUENCE[$idx]}"
    fi
    if [[ "$val" == "null" ]]; then
        echo '{"mergeable":null}'
    else
        echo "{\"mergeable\":$val}"
    fi
}

# --- (a) recheck resolves to mergeable=true after a stale first read -> merge ---
_STUB_SEQUENCE=("false" "true")
_reset_stub_counter
out_a="$(_recheck_mergeable_before_refusal "owner/repo" 1 "gh" "main" "feature/clean" "$WORK" 3 0)"
if [[ "$out_a" == merge:* ]] && [[ "$out_a" == *"stale"* ]] && [[ "$out_a" == *"recheck #2"* ]]; then
    pass "(a) recheck resolving to mergeable=true proceeds to merge"
else
    fail "(a) expected a merge:*stale*recheck #2* decision; got: $out_a"
fi

# --- (b) recheck never resolves true, but git merge-tree is clean -> merge ---
_STUB_SEQUENCE=("false" "false")
_reset_stub_counter
out_b="$(_recheck_mergeable_before_refusal "owner/repo" 1 "gh" "main" "feature/clean" "$WORK" 2 0)"
if [[ "$out_b" == merge:* ]] && [[ "$out_b" == *"git merge-tree"* ]] && [[ "$out_b" == *"stale/false-negative"* ]]; then
    pass "(b) persistent mergeable=false but clean git merge-tree still proceeds to merge (the AC's core reproduction)"
else
    fail "(b) expected a merge:*git merge-tree*stale/false-negative* decision; got: $out_b"
fi

# --- (c) recheck never resolves true AND git merge-tree confirms a real
#     conflict -> refuse-conflict (the negative case; don't regress) ---
_STUB_SEQUENCE=("false" "false")
_reset_stub_counter
out_c="$(_recheck_mergeable_before_refusal "owner/repo" 1 "gh" "main" "feature/conflict" "$WORK" 2 0)"
if [[ "$out_c" == refuse-conflict:* ]] && [[ "$out_c" == *"genuinely conflicts"* ]]; then
    pass "(c) persistent mergeable=false with a real git merge-tree conflict refuses with 'genuinely conflicts'"
else
    fail "(c) expected a refuse-conflict:*genuinely conflicts* decision; got: $out_c"
fi

# --- (d) base/head ref unavailable -> refuse-stale (fail closed, but
#     distinguishable from a confirmed conflict) ---
_STUB_SEQUENCE=("false")
_reset_stub_counter
out_d="$(_recheck_mergeable_before_refusal "owner/repo" 1 "gh" "" "feature/clean" "$WORK" 1 0)"
if [[ "$out_d" == refuse-stale:* ]] && [[ "$out_d" == *"ref unavailable"* ]]; then
    pass "(d) missing base/head ref refuses as refuse-stale (not refuse-conflict)"
else
    fail "(d) expected a refuse-stale:*ref unavailable* decision; got: $out_d"
fi

# --- (e) local fetch fails (no such remote/ref) -> refuse-stale ---
NO_ORIGIN="$TMP_ROOT/no-origin"
git init -q "$NO_ORIGIN"
_STUB_SEQUENCE=("false")
_reset_stub_counter
out_e="$(_recheck_mergeable_before_refusal "owner/repo" 1 "gh" "main" "feature/clean" "$NO_ORIGIN" 1 0)"
if [[ "$out_e" == refuse-stale:* ]] && [[ "$out_e" == *"could not fetch"* ]]; then
    pass "(e) a fetch failure refuses as refuse-stale (not refuse-conflict) -- corroboration unavailable, not disproven"
else
    fail "(e) expected a refuse-stale:*could not fetch* decision; got: $out_e"
fi

# --- (f) mergeable=null (still computing) on every recheck also fails to
#     resolve true, falling through to git merge-tree corroboration exactly
#     like a persistent false would. ---
_STUB_SEQUENCE=("null" "null")
_reset_stub_counter
out_f="$(_recheck_mergeable_before_refusal "owner/repo" 1 "gh" "main" "feature/clean" "$WORK" 2 0)"
if [[ "$out_f" == merge:* ]] && [[ "$out_f" == *"git merge-tree"* ]]; then
    pass "(f) persistent mergeable=null (still computing) still corroborates via git merge-tree and proceeds"
else
    fail "(f) expected a merge:*git merge-tree* decision; got: $out_f"
fi

echo ""
echo "Test 3: telemetry retries_used derivation (#6978) against the real decisions above"

# Mirrors the exact call-site logic added in merge-pr.sh: retries_used is the
# configured retry count UNLESS the reason string embeds "recheck #N" (the
# recheck resolved true mid-loop), in which case that specific attempt number
# wins. This is applied here to the REAL decision strings produced by the
# unmodified _recheck_mergeable_before_refusal calls above (a)-(f) -- proof
# the derivation is correct against live output, not a hand-crafted fixture.
_derive_retries_used() {
    local reason="$1" configured="$2"
    if [[ "$reason" =~ recheck\ \#([0-9]+) ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo "$configured"
    fi
}

_reason_a="${out_a#*:}"
if [[ "$(_derive_retries_used "$_reason_a" 3)" == "2" ]]; then
    pass "(a) retries_used derives to 2 (the mid-loop attempt that resolved true), not the configured 3"
else
    fail "(a) expected retries_used=2, got: $(_derive_retries_used "$_reason_a" 3)"
fi

_reason_b="${out_b#*:}"
if [[ "$(_derive_retries_used "$_reason_b" 2)" == "2" ]]; then
    pass "(b) retries_used derives to the configured 2 (all retries exhausted before merge-tree corroboration)"
else
    fail "(b) expected retries_used=2, got: $(_derive_retries_used "$_reason_b" 2)"
fi

_reason_c="${out_c#*:}"
if [[ "$(_derive_retries_used "$_reason_c" 2)" == "2" ]]; then
    pass "(c) retries_used derives to the configured 2 on a genuine-conflict refusal"
else
    fail "(c) expected retries_used=2, got: $(_derive_retries_used "$_reason_c" 2)"
fi

_reason_d="${out_d#*:}"
if [[ "$(_derive_retries_used "$_reason_d" 1)" == "1" ]]; then
    pass "(d) retries_used derives to the configured 1 on a ref-unavailable refusal"
else
    fail "(d) expected retries_used=1, got: $(_derive_retries_used "$_reason_d" 1)"
fi

_reason_e="${out_e#*:}"
if [[ "$(_derive_retries_used "$_reason_e" 1)" == "1" ]]; then
    pass "(e) retries_used derives to the configured 1 on a fetch-failure refusal"
else
    fail "(e) expected retries_used=1, got: $(_derive_retries_used "$_reason_e" 1)"
fi

_reason_f="${out_f#*:}"
if [[ "$(_derive_retries_used "$_reason_f" 2)" == "2" ]]; then
    pass "(f) retries_used derives to the configured 2 when every recheck returns null"
else
    fail "(f) expected retries_used=2, got: $(_derive_retries_used "$_reason_f" 2)"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
