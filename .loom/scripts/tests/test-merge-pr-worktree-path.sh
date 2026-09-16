#!/usr/bin/env bash
# test-merge-pr-worktree-path.sh - Tests for the --worktree-path flag (#3364)
#
# Verifies:
#   1. --worktree-path appears in --help output (composition with help test).
#   2. CLI rejects bad input early: missing value, nonexistent path,
#      registered-worktree validation.
#   3. The script's source contains the bypass-sentinel logic and the
#      porcelain discovery fallback (static grep checks — full integration
#      requires a live forge).
#   4. Inline simulation of the cleanup decision tree:
#      - LOOM_PRESERVE_WORKTREE=1 wins
#      - --no-cleanup-worktree wins over --worktree-path
#      - --worktree-path bypasses sentinel on the explicit path
#      - default path keeps sentinel guard
#      - discovery fallback emits hint without removing
#
# This is the companion to test-merge-pr-help.sh. The help test verifies
# the documentation surface; this test verifies the implementation surface.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"
FORGE_HELPERS="$SCRIPTS_DIR/lib/forge-helpers.sh"

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

[[ -x "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not executable" >&2; exit 1; }

# --- Test 1: CLI parsing rejects bad input early ---
echo "Test 1: CLI rejects bad --worktree-path input"

# Missing value
set +e
out=$("$MERGE_PR" 1 --worktree-path 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"--worktree-path requires a value"* ]]; then
    pass "missing value for --worktree-path errors with rc!=0 and clear message"
else
    fail "missing value: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# Nonexistent path
set +e
out=$("$MERGE_PR" --worktree-path /nonexistent-loom-test-path 1 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"does not exist"* ]]; then
    pass "nonexistent --worktree-path errors with rc!=0 and clear message"
else
    fail "nonexistent path: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# Path exists but is not a registered worktree of this repo
set +e
out=$("$MERGE_PR" --worktree-path /tmp 1 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"not a registered worktree"* ]]; then
    pass "unregistered --worktree-path errors with rc!=0 and clear message"
else
    fail "unregistered path: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# --- Test 2: Source contains the expected logic surface ---
echo ""
echo "Test 2: merge-pr.sh source contains the new logic blocks"

assert_grep "WORKTREE_PATH_OVERRIDE=" "$MERGE_PR" \
    "merge-pr.sh declares WORKTREE_PATH_OVERRIDE state variable"
assert_grep "_find_worktree_by_branch" "$MERGE_PR" \
    "merge-pr.sh defines the porcelain branch-search helper"
assert_grep "_worktree_branch_for" "$MERGE_PR" \
    "merge-pr.sh defines the worktree-to-branch lookup helper"
assert_grep "_maybe_delete_local_branch" "$MERGE_PR" \
    "merge-pr.sh defines the safe local-branch delete helper"
assert_grep "git branch -d" "$MERGE_PR" \
    "merge-pr.sh uses git branch -d (safe delete, not -D)"
assert_grep "allow_unmanaged" "$MERGE_PR" \
    "_remove_loom_worktree takes allow_unmanaged second arg"
assert_grep "Bypassing sentinel guard" "$MERGE_PR" \
    "explicit --worktree-path logs the sentinel-bypass action"
assert_grep "Discovered worktree for branch" "$MERGE_PR" \
    "discovery fallback emits a hint about the discovered path"
assert_grep "re-run with: --worktree-path" "$MERGE_PR" \
    "discovery fallback suggests --worktree-path in the hint"

# --- Test 2b: async-close-race guard (#4186) source surface ---
assert_grep "_issue_is_closed_for_cleanup" "$MERGE_PR" \
    "merge-pr.sh defines the close-target-aware cleanup gate"
assert_grep "forge_pr_close_targets" "$MERGE_PR" \
    "merge-pr.sh's cleanup gate consults forge_pr_close_targets (async-close-race adaptation)"
assert_grep "forge_get_issue_state" "$MERGE_PR" \
    "merge-pr.sh's cleanup gate consults forge_get_issue_state for non-close-target issues"
assert_grep "Preserving worktree at" "$MERGE_PR" \
    "default-path preserved-worktree case logs a clear reason"
assert_grep "Preserving discovered worktree at" "$MERGE_PR" \
    "discovered-path preserved-worktree case logs a clear reason"
assert_grep "forge_get_issue_state" "$FORGE_HELPERS" \
    "forge-helpers.sh defines forge_get_issue_state"

# --- Test 2c: co-existing Judge review worktree cleanup (#6264) source surface ---
assert_grep "JUDGE_PR_WT_PATH" "$MERGE_PR" \
    "merge-pr.sh declares JUDGE_PR_WT_PATH for the co-existing pr-<N> check"
assert_grep 'JUDGE_PR_WT_PATH="\$WT_ROOT_DIR/pr-\$PR_NUMBER"' "$MERGE_PR" \
    "JUDGE_PR_WT_PATH is set to pr-\$PR_NUMBER only on the feature/issue-<N> branch"
assert_grep "Found co-existing Judge/Doctor review worktree" "$MERGE_PR" \
    "co-existing pr-<N> removal logs a clear reason (#6264)"
assert_grep "Preserving Judge/Doctor review worktree at" "$MERGE_PR" \
    "co-existing pr-<N> preserved-worktree case logs a clear reason (#6264)"

# --- Test 2d: never-closing-issue worktree/branch cleanup (#6694) source surface ---
assert_grep 'source "\$SCRIPT_DIR/lib/branch-landed.sh"' "$MERGE_PR" \
    "merge-pr.sh sources the shared branch-landed primitive (#7812)"
assert_grep 'branch_landed "\$branch" "\${DEFAULT_BRANCH_NAME:-}" "\$expected_head_sha"' "$MERGE_PR" \
    "_maybe_delete_local_branch delegates its safety check to the shared primitive (#6694/#7812)"
assert_grep 'branch_has_landed "\$PR_BRANCH" "\$DEFAULT_BRANCH_NAME" "\$PR_HEAD_SHA"' "$MERGE_PR" \
    "the worktree-preserve decision reuses the shared primitive at every call site (#6694/#7812)"
assert_grep "holds nothing unmerged; removing it \\(#6694\\)" "$MERGE_PR" \
    "a fully-captured branch is cleaned up even when the issue-close gate says preserve (#6694)"
assert_grep "designed never to close \\(#6694\\), that retry never fires: remove manually" "$MERGE_PR" \
    "the reworded preserve message names a remedy that does not depend on the issue closing (#6694)"

# --- Test 3: Precedence — --no-cleanup-worktree warns when combined ---
echo ""
echo "Test 3: --no-cleanup-worktree wins over --worktree-path"

# This requires a registered worktree path. Use the script's own repo root
# (the worktree this test is running inside).
SELF_WT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
# Resolve the worktree's actual recorded path via porcelain — git's worktree
# list uses the canonical recorded path which may differ from $PWD if there
# are symlinks.
ACTUAL_WT="$(cd "$SELF_WT" && git rev-parse --show-toplevel 2>/dev/null || echo "")"

if [[ -n "$ACTUAL_WT" ]] && git -C "$ACTUAL_WT" worktree list --porcelain 2>/dev/null | \
   awk -v p="$ACTUAL_WT" '/^worktree / { if ($2 == p) { found=1; exit } } END { exit !found }'; then
    # We're in a worktree; we can use ACTUAL_WT as a valid --worktree-path value.
    # Run with --dry-run so the merge itself short-circuits (we only want to see
    # the validation + warning).
    set +e
    out=$("$MERGE_PR" --no-cleanup-worktree --worktree-path "$ACTUAL_WT" 1 --dry-run 2>&1)
    rc=$?
    set -e
    if [[ "$out" == *"--no-cleanup-worktree wins"* ]]; then
        pass "combining --no-cleanup-worktree + --worktree-path warns"
    else
        fail "expected '--no-cleanup-worktree wins' warning; got: $out"
    fi
else
    echo "  SKIP: not running inside a registered worktree, skipping precedence test"
fi

# --- Test 4: Inline simulation of the cleanup decision tree ---
echo ""
echo "Test 4: cleanup decision tree (inline simulation)"

# Replicate the decision shape from merge-pr.sh's cleanup driver so we can
# exercise every branch without a live forge round-trip.
simulate_cleanup() {
    # Args:
    #   $1 preserve            ("0" / "1")        # LOOM_PRESERVE_WORKTREE
    #   $2 cleanup             ("true" / "false") # --no-cleanup-worktree => false
    #   $3 override            (string or "")     # --worktree-path value
    #   $4 default_exists      ("true" / "false") # whether .loom/worktrees/issue-N exists
    #   $5 override_has_sentinel ("true" / "false") # does override path have .loom-managed
    #   $6 discovered          (string or "")     # discovered worktree path
    #   $7 discovered_has_sentinel ("true" / "false")
    #   $8 issue_num           (string or "", default "") # set only on the
    #      feature/issue-<N> path; empty models the unaffected pr-<N> path (#4186)
    #   $9 is_close_target     ("true" / "false", default "false") # is
    #      issue_num among forge_pr_close_targets for the just-merged PR?
    #   $10 issue_state        ("OPEN" / "CLOSED" / "", default "") # live
    #      forge_get_issue_state result; "" models a lookup failure
    #   $11 branch_fully_captured ("true" / "false", default "false") # #6694:
    #      has the local branch landed on the default branch (i.e.
    #      `branch_has_landed`, #7812)? Only consulted when the issue
    #      gate itself says "preserve" — a never-closing programme issue
    #      never satisfies _gate_allows_removal, so without this the
    #      worktree/branch would preserve forever.
    local preserve="$1" cleanup="$2" override="$3" default_exists="$4" \
          override_has_sentinel="$5" discovered="$6" discovered_has_sentinel="$7" \
          issue_num="${8:-}" is_close_target="${9:-false}" issue_state="${10:-}" \
          branch_fully_captured="${11:-false}"

    if [[ "$cleanup" != "true" ]]; then
        echo "skip:no-cleanup"; return 0
    fi
    if [[ "$preserve" == "1" ]]; then
        echo "skip:env"; return 0
    fi
    if [[ -n "$override" ]]; then
        # --worktree-path bypasses sentinel (and the #4186 issue gate below —
        # the operator explicitly took responsibility for this path).
        if [[ "$override_has_sentinel" == "true" ]]; then
            echo "remove:override-managed"
        else
            echo "remove:override-bypass-sentinel"
        fi
        return 0
    fi

    # Close-target-aware issue gate (#4186), mirroring
    # _issue_is_closed_for_cleanup: no issue_num (pr-<N> path) always allows
    # removal; a close-target issue always allows removal (the merge itself
    # closes it, no race); otherwise fall back to the live state lookup,
    # where CLOSED allows and anything else (including a lookup failure,
    # modeled by issue_state="") preserves.
    _gate_allows_removal() {
        if [[ -z "$issue_num" ]]; then
            return 0
        fi
        if [[ "$is_close_target" == "true" ]]; then
            return 0
        fi
        [[ "$issue_state" == "CLOSED" ]]
    }

    if [[ "$default_exists" == "true" ]]; then
        if _gate_allows_removal; then
            echo "remove:default"
        elif [[ "$branch_fully_captured" == "true" ]]; then
            # #6694: the issue gate says preserve, but the branch's content is
            # already fully on the default branch — nothing is lost by
            # removing it now, regardless of whether the issue ever closes.
            echo "remove:default-fully-captured"
        else
            echo "preserve:default-open-issue"
        fi
        return 0
    fi
    # Fallback discovery
    if [[ -n "$discovered" ]]; then
        if [[ "$discovered_has_sentinel" == "true" ]]; then
            if _gate_allows_removal; then
                echo "remove:discovered-managed"
            elif [[ "$branch_fully_captured" == "true" ]]; then
                echo "remove:discovered-fully-captured"
            else
                echo "preserve:discovered-open-issue"
            fi
        else
            echo "warn:discovered-user-owned"
        fi
        return 0
    fi
    echo "skip:nothing-to-do"
}

# Args: preserve cleanup override default_exists override_has_sentinel discovered discovered_has_sentinel

# Case A: LOOM_PRESERVE_WORKTREE=1 wins over everything
result=$(simulate_cleanup 1 true "/path/x" false true "" false)
if [[ "$result" == "skip:env" ]]; then
    pass "case A: LOOM_PRESERVE_WORKTREE=1 short-circuits everything"
else
    fail "case A: expected 'skip:env', got '$result'"
fi

# Case B: --no-cleanup-worktree wins (cleanup=false)
result=$(simulate_cleanup 0 false "/path/x" false true "" false)
if [[ "$result" == "skip:no-cleanup" ]]; then
    pass "case B: --no-cleanup-worktree short-circuits even with override"
else
    fail "case B: expected 'skip:no-cleanup', got '$result'"
fi

# Case C: --worktree-path bypasses sentinel (no sentinel on override path)
result=$(simulate_cleanup 0 true "/path/x" true false "" false)
if [[ "$result" == "remove:override-bypass-sentinel" ]]; then
    pass "case C: --worktree-path bypasses sentinel for non-Loom worktree"
else
    fail "case C: expected 'remove:override-bypass-sentinel', got '$result'"
fi

# Case D: --worktree-path on Loom-managed worktree — still removes
result=$(simulate_cleanup 0 true "/path/x" true true "" false)
if [[ "$result" == "remove:override-managed" ]]; then
    pass "case D: --worktree-path also removes Loom-managed worktrees"
else
    fail "case D: expected 'remove:override-managed', got '$result'"
fi

# Case E: default path exists — remove via sentinel-guarded path
result=$(simulate_cleanup 0 true "" true false "" false)
if [[ "$result" == "remove:default" ]]; then
    pass "case E: default Loom-convention path used when present"
else
    fail "case E: expected 'remove:default', got '$result'"
fi

# Case F: default missing, discovered worktree has sentinel — remove
result=$(simulate_cleanup 0 true "" false false "/found" true)
if [[ "$result" == "remove:discovered-managed" ]]; then
    pass "case F: discovery removes Loom-managed worktree at non-standard path"
else
    fail "case F: expected 'remove:discovered-managed', got '$result'"
fi

# Case G: default missing, discovered worktree LACKS sentinel — warn-only
result=$(simulate_cleanup 0 true "" false false "/found" false)
if [[ "$result" == "warn:discovered-user-owned" ]]; then
    pass "case G: discovery warns but does NOT remove user-owned worktree"
else
    fail "case G: expected 'warn:discovered-user-owned', got '$result'"
fi

# Case H: nothing found anywhere — quiet success
result=$(simulate_cleanup 0 true "" false false "" false)
if [[ "$result" == "skip:nothing-to-do" ]]; then
    pass "case H: nothing-found is a quiet no-op"
else
    fail "case H: expected 'skip:nothing-to-do', got '$result'"
fi

# --- Test 5: async-close-race issue gate (#4186) ---
echo ""
echo "Test 5: close-target-aware issue gate on the default and discovered paths"

# Case I: default path, issue IS a close target of the merged PR — a normal
# `Closes #N` merge must clean up exactly as before this change, no state
# lookup consulted at all.
result=$(simulate_cleanup 0 true "" true false "" false 42 true "")
if [[ "$result" == "remove:default" ]]; then
    pass "case I: Closes-target issue removes unconditionally (no regression)"
else
    fail "case I: expected 'remove:default', got '$result'"
fi

# Case J: default path, issue is NOT a close target and its live state is
# OPEN — the partial-increment shape (#3667); preserve.
result=$(simulate_cleanup 0 true "" true false "" false 42 false "OPEN")
if [[ "$result" == "preserve:default-open-issue" ]]; then
    pass "case J: non-target open issue preserves the worktree"
else
    fail "case J: expected 'preserve:default-open-issue', got '$result'"
fi

# Case K: default path, issue is NOT a close target and the state lookup
# failed (modeled as an empty issue_state) — fail-unsafe-to-preserve.
result=$(simulate_cleanup 0 true "" true false "" false 42 false "")
if [[ "$result" == "preserve:default-open-issue" ]]; then
    pass "case K: issue-state lookup failure preserves the worktree (fail-unsafe)"
else
    fail "case K: expected 'preserve:default-open-issue', got '$result'"
fi

# Case L: default path, issue is NOT a close target but its live state is
# CLOSED (e.g. closed independently of this PR) — safe to remove.
result=$(simulate_cleanup 0 true "" true false "" false 42 false "CLOSED")
if [[ "$result" == "remove:default" ]]; then
    pass "case L: non-target issue whose live state is CLOSED still removes"
else
    fail "case L: expected 'remove:default', got '$result'"
fi

# Case M: no issue_num at all (the pr-<N> worktree path) — gate is skipped
# entirely; default-path removal is unaffected.
result=$(simulate_cleanup 0 true "" true false "" false "" false "")
if [[ "$result" == "remove:default" ]]; then
    pass "case M: pr-<N> path (no ISSUE_NUM) is unaffected by the issue gate"
else
    fail "case M: expected 'remove:default', got '$result'"
fi

# Case N: discovered (non-standard-path) Loom-managed worktree, issue is NOT
# a close target and is OPEN — preserve at the discovered-path call site too.
result=$(simulate_cleanup 0 true "" false false "/found" true 42 false "OPEN")
if [[ "$result" == "preserve:discovered-open-issue" ]]; then
    pass "case N: discovered-path gate also preserves for a non-target open issue"
else
    fail "case N: expected 'preserve:discovered-open-issue', got '$result'"
fi

# --- Test 5b: never-closing programme issue (#6694) — distinct from the
# eventually-closes-later reaping path (case J/K/N above, and
# test-merge-pr-closed-issue-cleanup.sh's separate reaping coverage). These
# cases model an issue that is OPEN, not a close target of this PR, and never
# will close (every merge to it is `Part of #N` by design) — the exact shape
# where the pre-#6694 behavior preserved the worktree/branch indefinitely,
# because _issue_is_closed_for_cleanup can never flip true for it.
echo ""
echo "Test 5b: never-closing programme issue (#6694)"

# Case T: default path, issue open + not a close target (never will close),
# but the branch's tip IS the merged PR's head SHA — nothing is unmerged, so
# clean up now instead of waiting for a close that will never happen.
result=$(simulate_cleanup 0 true "" true false "" false 42 false "OPEN" true)
if [[ "$result" == "remove:default-fully-captured" ]]; then
    pass "case T: never-closing issue + branch fully captured -> removed at merge time, not preserved forever"
else
    fail "case T: expected 'remove:default-fully-captured', got '$result'"
fi

# Case U: same never-closing shape, but the branch carries LOCAL commits
# beyond the merged PR's head (e.g. an in-flight follow-up not yet pushed) —
# still genuinely preserve; the fully-captured check must not be a blanket
# bypass of the issue gate.
result=$(simulate_cleanup 0 true "" true false "" false 42 false "OPEN" false)
if [[ "$result" == "preserve:default-open-issue" ]]; then
    pass "case U: never-closing issue + branch NOT fully captured -> still genuinely preserved"
else
    fail "case U: expected 'preserve:default-open-issue', got '$result'"
fi

# Case V: same fully-captured shape, but at the discovered (non-standard-path)
# call site — the #6694 fix applies there too, not just the default path.
result=$(simulate_cleanup 0 true "" false false "/found" true 42 false "OPEN" true)
if [[ "$result" == "remove:discovered-fully-captured" ]]; then
    pass "case V: discovered-path never-closing issue + branch fully captured -> removed too"
else
    fail "case V: expected 'remove:discovered-fully-captured', got '$result'"
fi

# Case W: an issue-state LOOKUP FAILURE (fail-unsafe-to-preserve, issue_state=
# "") combined with a fully-captured branch still cleans up — the #6694 check
# is independent of *why* the issue gate said preserve (never-closing design
# vs. a transient lookup failure), only whether the branch's content is safe.
result=$(simulate_cleanup 0 true "" true false "" false 42 false "" true)
if [[ "$result" == "remove:default-fully-captured" ]]; then
    pass "case W: issue-state lookup failure + branch fully captured -> still removed (not just the never-closing case)"
else
    fail "case W: expected 'remove:default-fully-captured', got '$result'"
fi

# --- Test 6: co-existing Judge review worktree (pr-<N> alongside issue-<N>, #6264) ---
echo ""
echo "Test 6: co-existing pr-<N> Judge review worktree cleanup (#6264)"

# Replicates the independent JUDGE_PR_WT_PATH check added by #6264: it runs
# ONLY when PR_BRANCH matched feature/issue-<N> (issue_num non-empty is the
# precondition here — the external-fork branch never sets JUDGE_PR_WT_PATH at
# all, see Case R below), keyed purely by whether the pr-$PR_NUMBER path
# EXISTS on disk — independent of the issue-<N> path's own outcome above it,
# and independent of whether the branch checked out inside it is attached or
# detached (a detached pr-<N> worktree has no branch line for the discovery
# fallback's porcelain search to match, which is exactly the gap #6264 closes
# by checking the path directly instead).
simulate_judge_pr_cleanup() {
    # Args:
    #   $1 issue_num        (string or "")      # "" models the external-fork
    #      branch, where JUDGE_PR_WT_PATH is never set
    #   $2 pr_wt_exists     ("true"/"false")     # does pr-$PR_NUMBER exist?
    #   $3 is_close_target  ("true"/"false", default "false")
    #   $4 issue_state      ("OPEN"/"CLOSED"/"", default "")
    #   $5 branch_fully_captured ("true"/"false", default "false") # #6694
    local issue_num="$1" pr_wt_exists="$2" is_close_target="${3:-false}" issue_state="${4:-}" \
          branch_fully_captured="${5:-false}"

    if [[ -z "$issue_num" ]]; then
        echo "skip:not-applicable"
        return 0
    fi
    if [[ "$pr_wt_exists" != "true" ]]; then
        echo "skip:no-pr-worktree"
        return 0
    fi
    if [[ "$is_close_target" == "true" ]] || [[ "$issue_state" == "CLOSED" ]]; then
        echo "remove:judge-pr-worktree"
    elif [[ "$branch_fully_captured" == "true" ]]; then
        # #6694: same fully-captured escape hatch as the issue-<N> worktree.
        echo "remove:judge-pr-worktree-fully-captured"
    else
        echo "preserve:judge-pr-worktree-open-issue"
    fi
}

# Case O: a co-existing pr-<N> Judge review worktree exists for a close-target
# issue and gets removed — deliberately independent of whether the issue-<N>
# worktree handled elsewhere in the script also existed (this is the whole
# point of #6264's fix: the real code's JUDGE_PR_WT_PATH check never
# consults DEFAULT_WT_PATH's own outcome). Covers BOTH incident shapes: (a)
# `git worktree list` showing both an issue-<N> AND a detached pr-<N> entry
# (the exact incident from the issue body), and (b) only pr-<N> existing
# locally at all (e.g. a standalone Judge pass with no local builder
# worktree) — the simulation is identical either way, which is the property
# under test.
result=$(simulate_judge_pr_cleanup 42 true true)
if [[ "$result" == "remove:judge-pr-worktree" ]]; then
    pass "case O: co-existing pr-<N> removed when its issue is a close target (regardless of issue-<N> presence)"
else
    fail "case O: expected 'remove:judge-pr-worktree', got '$result'"
fi

# Case Q: partial-increment shape — issue is NOT a close target and its live
# state is OPEN — preserve the Judge review worktree too, mirroring the
# issue-<N> path's own #4186 gate (a future merge that closes the issue will
# retry cleanup).
result=$(simulate_judge_pr_cleanup 42 true false "OPEN")
if [[ "$result" == "preserve:judge-pr-worktree-open-issue" ]]; then
    pass "case Q: non-target open issue preserves the co-existing pr-<N> worktree too"
else
    fail "case Q: expected 'preserve:judge-pr-worktree-open-issue', got '$result'"
fi

# Case Q2: never-closing programme issue (#6694) — open, not a close target,
# but the branch's tip IS the merged PR's head SHA — the co-existing
# Judge/Doctor review worktree is cleaned up now instead of preserved
# indefinitely for a close that will never arrive.
result=$(simulate_judge_pr_cleanup 42 true false "OPEN" true)
if [[ "$result" == "remove:judge-pr-worktree-fully-captured" ]]; then
    pass "case Q2: never-closing issue + branch fully captured removes the co-existing pr-<N> worktree too (#6694)"
else
    fail "case Q2: expected 'remove:judge-pr-worktree-fully-captured', got '$result'"
fi

# Case R: external-fork / ad-hoc branch (#3358) — JUDGE_PR_WT_PATH is never
# set (issue_num is empty), so this check is a no-op regardless of whether a
# pr-<N> worktree exists; that worktree is exactly DEFAULT_WT_PATH and is
# already handled unchanged by the pre-existing pr-<N>-as-default-path logic
# (Cases A-N above, with issue_num=""). Confirms #6264 introduces no new
# behavior on the external-fork path.
result=$(simulate_judge_pr_cleanup "" true true)
if [[ "$result" == "skip:not-applicable" ]]; then
    pass "case R: external-fork branch (#3358) is unaffected — no JUDGE_PR_WT_PATH check runs"
else
    fail "case R: expected 'skip:not-applicable', got '$result'"
fi

# Case S: nothing to do — no co-existing pr-<N> worktree on disk.
result=$(simulate_judge_pr_cleanup 42 false)
if [[ "$result" == "skip:no-pr-worktree" ]]; then
    pass "case S: no pr-<N> worktree present is a quiet no-op"
else
    fail "case S: expected 'skip:no-pr-worktree', got '$result'"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
