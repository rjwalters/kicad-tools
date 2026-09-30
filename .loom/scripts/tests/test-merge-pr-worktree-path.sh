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
#   7. The REAL `_issue_is_closed_for_cleanup` (#4186), extracted and sourced
#      from merge-pr.sh (not the Test 4/5 simulation above), driven against
#      the actual `loom-daemon merge-pr issue-close-gate` binary (#8191) —
#      including the daemon-fault fail-unsafe-to-preserve path.
#
# This is the companion to test-merge-pr-help.sh. The help test verifies
# the documentation surface; this test verifies the implementation surface.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"
FORGE_HELPERS="$SCRIPTS_DIR/lib/forge-helpers.sh"

# #8191: `_issue_is_closed_for_cleanup`'s decision now delegates to
# `loom-daemon merge-pr issue-close-gate`, and the CLI's own --worktree-path
# validation (Test 1 below) now delegates to `loom-daemon merge-pr
# worktree-contains`. Pin the binary those exec and verify it HAS the
# subcommands, mirroring every other ported-decision suite in this family
# (e.g. test-merge-pr-closed-issue-cleanup.sh). Tests 2-6 above never invoke
# the real function (they re-simulate the decision tree in pure bash), so
# this does not gate them.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "merge-pr issue-close-gate" "merge-pr worktree-preserve" "merge-pr worktree-contains"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# An assertion that CANNOT survive the port to Rust, retired under the
# three-part test in defaults/docs/verification-recipes.md §6. Printed, not
# deleted: a reader must be able to see what was removed, why, and what proves
# the property now. Counted as run so the totals stay honest.
YELLOW='\033[0;33m'
retired() { # <what> <property> <why-structural> <successor>
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${YELLOW}RETIRED${NC}: $1"
    echo "      property:   $2"
    echo "      structural: $3"
    echo "      successor:  $4"
}

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
retired \
    "default-path preserved-worktree case logs a clear reason" \
    "the operator-visible preserve message names why a worktree at the Loom-convention path was left in place" \
    "#8191: the #4186/#6694 remove-vs-preserve decision, and the message text it builds, moved to loom-daemon/src/merge_pr/worktree_preserve.rs — merge-pr.sh's own text is now just \`_worktree_cleanup_decide default \"\$DEFAULT_WT_PATH\"\`, and the retired \"Preserving worktree at\" string is not a shell string anywhere in this file to grep for" \
    "loom-daemon/tests/merge_pr_worktree_preserve_differential.rs, which drives the frozen retired block (tests/fixtures/merge-pr-worktree-preserve-retired.sh) against the real CLI on a shared corpus and asserts the preserve message is unchanged byte for byte, plus loom-daemon's merge_pr::worktree_preserve::tests::preserve_check_and_not_landed_preserves_with_two_lines"
assert_grep "forge_get_issue_state" "$FORGE_HELPERS" \
    "forge-helpers.sh defines forge_get_issue_state"

# --- Test 2c: co-existing Judge review worktree cleanup (#6264) source surface ---
assert_grep "JUDGE_PR_WT_PATH" "$MERGE_PR" \
    "merge-pr.sh declares JUDGE_PR_WT_PATH for the co-existing pr-<N> check"
retired \
    "JUDGE_PR_WT_PATH is set to pr-\$PR_NUMBER only on the feature/issue-<N> branch" \
    "#6264's asymmetry: a co-existing Judge/Doctor pr-<N> review worktree is named only for an ordinary feature/issue-<N> PR, never for an external-fork/ad-hoc branch whose own default path is already pr-<N> (naming it there would make the second call site a pure duplicate of the first)" \
    "#8191: the assignment left the shell. All three names now arrive together from \`loom-daemon merge-pr cleanup-paths\` through one \`IFS=\$'\\t' read -r DEFAULT_WT_PATH ISSUE_NUM JUDGE_PR_WT_PATH\` (default path FIRST: tab is IFS whitespace, so an empty LEADING field cannot survive that read), so there is no JUDGE_PR_WT_PATH= assignment — and no \$WT_ROOT_DIR at all — left in this file to grep for" \
    "loom-daemon/tests/merge_pr_cleanup_paths_differential.rs, which drives the frozen retired block (tests/fixtures/merge-pr-cleanup-paths-retired.sh) against the port over every branch shape and asserts the asymmetry as an invariant of every case (issue field non-empty <=> judge-pr field non-empty), plus merge_pr::cleanup_paths::tests::a_non_issue_branch_names_only_the_pr_worktree; cases O-R below still exercise the behaviour"
retired \
    "co-existing pr-<N> removal logs a clear reason (#6264)" \
    "the operator-visible removal message for a co-existing Judge/Doctor review worktree names why (#6264)" \
    "#8191: same consolidation as the default/discovered messages above — the text is now loom-daemon/src/merge_pr/worktree_preserve.rs's Kind::JudgePr branch, reached via \`_worktree_cleanup_decide judge-pr \"\$JUDGE_PR_WT_PATH\"\`" \
    "loom-daemon/tests/merge_pr_worktree_preserve_differential.rs's Kind::JudgePr cases, plus merge_pr::worktree_preserve::tests::no_preserve_check_judge_pr_removes_with_a_note"
retired \
    "co-existing pr-<N> preserved-worktree case logs a clear reason (#6264)" \
    "the operator-visible preserve message for a co-existing Judge/Doctor review worktree names why (#6264)" \
    "#8191: same consolidation — the \"Preserving Judge/Doctor review worktree at\" text is loom-daemon/src/merge_pr/worktree_preserve.rs's Kind::JudgePr preserve branch, not a shell string" \
    "loom-daemon/tests/merge_pr_worktree_preserve_differential.rs's Kind::JudgePr preserve cases, plus merge_pr::worktree_preserve::tests::judge_pr_kind_uses_its_own_noun_in_both_6694_messages"

# --- Test 2d: never-closing-issue worktree/branch cleanup (#6694) source surface ---
assert_grep 'source "\$SCRIPT_DIR/lib/branch-landed.sh"' "$MERGE_PR" \
    "merge-pr.sh sources the shared branch-landed primitive (#7812)"
retired \
    "_maybe_delete_local_branch delegates its safety check to the shared primitive (#6694/#7812)" \
    "the local-branch -d -> -D upgrade is gated on the shared branch-landed verdict (fed the merged PR's head SHA), never a private re-derivation" \
    "#8191: the decision left the shell. _maybe_delete_local_branch now calls 'loom-daemon merge-pr delete-branch', whose only rule is worktree_cli::branch_delete, which calls worktree_cli::branch_landed::probe (the Rust twin of lib/branch-landed.sh that worktree.sh remove already uses) — the shell holds no branch_landed call to grep for" \
    "the assertion immediately below (the shell threads \$expected_head_sha into the daemon), plus loom-daemon's branch_delete::tests::only_a_landed_verdict_may_escalate_to_force_delete and the behavioural cases in test-merge-pr-local-branch-cleanup.sh / test-merge-pr-primary-checkout-advice.sh that force-delete on a tip match"
assert_grep 'merge-pr delete-branch .*--expected-head-sha "\$expected_head_sha"' "$MERGE_PR" \
    "_maybe_delete_local_branch hands the merged head SHA to the shared rule (loom-daemon merge-pr delete-branch, #8191)"
assert_grep 'branch_has_landed "\$PR_BRANCH" "\$DEFAULT_BRANCH_NAME" "\$PR_HEAD_SHA"' "$MERGE_PR" \
    "the worktree-preserve decision reuses the shared primitive at every call site (#6694/#7812)"
retired \
    "a fully-captured branch is cleaned up even when the issue-close gate says preserve (#6694)" \
    "when the #4186 issue gate says preserve but branch_has_landed says the branch's content is already on the default branch, cleanup proceeds anyway and says so" \
    "#8191: the landed-branch override IS the decision that moved — it is now loom-daemon/src/merge_pr/worktree_preserve.rs's \`ctx.preserve_check && ctx.landed\` arm, and the \"holds nothing unmerged; removing it (#6694)\" text is a Rust format string, not a shell string this file can grep for" \
    "loom-daemon/tests/merge_pr_worktree_preserve_differential.rs, whose corpus asserts (via saw_landed_override_remove) that the override fires for all three kinds with byte-identical text to the frozen pre-port shell, plus merge_pr::worktree_preserve::tests::preserve_check_and_landed_removes_with_one_line — and, behaviourally end to end, cases T/V/W in Test 5b below, which still drive the real merge-pr.sh and observe the worktree actually being removed"
retired \
    "the reworded preserve message names a remedy that does not depend on the issue closing (#6694)" \
    "the preserve message tells an operator how to clean up by hand when the issue is a programme issue that will never close, so the automatic retry never fires" \
    "#8191: same move — the second (info) line of the preserve branch is built in loom-daemon/src/merge_pr/worktree_preserve.rs and replayed through the shell's \`info\`, so \"designed never to close (#6694), that retry never fires: remove manually\" is not a shell string in this file" \
    "loom-daemon/tests/merge_pr_worktree_preserve_differential.rs (the preserve cases compare both lines byte for byte against the frozen shell, so a dropped or reworded remedy line fails), plus merge_pr::worktree_preserve::tests::preserve_check_and_not_landed_preserves_with_two_lines"

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

# --- Test 7: the REAL _issue_is_closed_for_cleanup (#4186/#8191) ---
echo ""
echo "Test 7: _issue_is_closed_for_cleanup driven against the real daemon"

# Extract just the function under test from merge-pr.sh and source it — the
# same "extract from source, don't replicate" strategy
# test-merge-pr-closed-issue-cleanup.sh uses, so this stays in lockstep with
# the script rather than testing a hand-copied rewrite of it.
GATE_FUNCS_FILE="$(mktemp)"
GATE_STUB_DIR="$(mktemp -d)"
awk '
  /^_issue_is_closed_for_cleanup\(\) \{/ { capture=1 }
  capture { print }
  capture && /^}/ { capture=0 }
' "$MERGE_PR" > "$GATE_FUNCS_FILE"
if ! grep -q "^_issue_is_closed_for_cleanup() {" "$GATE_FUNCS_FILE"; then
    fail "could not extract _issue_is_closed_for_cleanup from $MERGE_PR"
else
    # Minimal shims the extracted body calls.
    warning() { echo "WARN: $*" >&2; }
    # shellcheck disable=SC1090
    source "$GATE_FUNCS_FILE"

    # REPO_NWO/PR_NUMBER/GH are read only by the extracted+sourced function
    # above, which shellcheck cannot see.
    # shellcheck disable=SC2034
    REPO_NWO="owner/repo"
    # shellcheck disable=SC2034
    PR_NUMBER="999"
    # shellcheck disable=SC2034
    GH="gh"
    GATE_CLOSE_TARGETS=""
    GATE_STATE=""
    forge_pr_close_targets() { printf '%s' "$GATE_CLOSE_TARGETS"; }
    forge_get_issue_state() { printf '%s' "$GATE_STATE"; }

    # T1: issue IS a close target -> clean up, no live-state read needed.
    GATE_CLOSE_TARGETS="42"; GATE_STATE=""
    if _issue_is_closed_for_cleanup 42; then
        pass "T1: a close-target issue authorizes cleanup with no live-state read"
    else
        fail "T1: expected cleanup authorized for a close-target issue"
    fi

    # T2: not a close target, live state CLOSED -> clean up.
    GATE_CLOSE_TARGETS="7"; GATE_STATE="CLOSED"
    if _issue_is_closed_for_cleanup 42; then
        pass "T2: a non-close-target issue with live state CLOSED authorizes cleanup"
    else
        fail "T2: expected cleanup authorized when live state is CLOSED"
    fi

    # T3: not a close target, live state OPEN -> preserve.
    GATE_CLOSE_TARGETS="7"; GATE_STATE="OPEN"
    set +e; _issue_is_closed_for_cleanup 42; rc=$?; set -e
    if [[ $rc -eq 1 ]]; then
        pass "T3: a non-close-target issue with live state OPEN preserves"
    else
        fail "T3: expected preserve (rc=1) for an OPEN non-close-target issue, got rc=$rc"
    fi

    # T4: not a close target, live-state lookup failure (empty) -> preserve
    # (fail-unsafe-to-preserve).
    GATE_CLOSE_TARGETS="7"; GATE_STATE=""
    set +e; _issue_is_closed_for_cleanup 42; rc=$?; set -e
    if [[ $rc -eq 1 ]]; then
        pass "T4: a live-state lookup failure preserves (fail-unsafe-to-preserve)"
    else
        fail "T4: expected preserve (rc=1) on a lookup failure, got rc=$rc"
    fi

    # T5 (#8191): a daemon predating the verb must preserve (never guess
    # cleanup) AND warn naming the fault — a guessed removal here is the
    # exact irreversible mistake #4186 exists to prevent.
    fake_no_verb="$GATE_STUB_DIR/fake-loom-daemon-no-verb"
    cat > "$fake_no_verb" <<'FAKEDAEMON'
#!/usr/bin/env bash
echo "error: unrecognized subcommand 'issue-close-gate'" >&2
exit 2
FAKEDAEMON
    chmod +x "$fake_no_verb"
    saved_bin="${LOOM_DAEMON_BIN:-}"
    GATE_CLOSE_TARGETS="42"; GATE_STATE=""
    export LOOM_DAEMON_BIN="$fake_no_verb"
    set +e; stderr_out="$(_issue_is_closed_for_cleanup 42 2>&1 >/dev/null)"; rc=$?; set -e
    export LOOM_DAEMON_BIN="$saved_bin"
    if [[ $rc -eq 1 ]]; then
        pass "T5: a daemon predating the verb preserves rather than guessing cleanup"
    else
        fail "T5: expected preserve (rc=1) when the daemon lacks the verb, got rc=$rc"
    fi
    if [[ "$stderr_out" == *"did not run"* ]]; then
        pass "T5: the daemon-fault path warns naming the cleanup gate"
    else
        fail "T5: expected a warning naming the fault; got: $stderr_out"
    fi
fi
rm -rf "$GATE_FUNCS_FILE" "$GATE_STUB_DIR" 2>/dev/null || true

# --- Test 8: an older daemon lacking `worktree-contains` never removes the
# unverified --worktree-path (#8191 slice; Judge's fail-safe fix on PR #9602).
# The registered-worktree check is a guard, and a guard that did not run must
# refuse the removal -- never lean on `git worktree remove` to refuse it. Runs
# the REAL parse-time validation block and the REAL post-merge cleanup dispatch,
# both extracted from merge-pr.sh, against a real repo + registered worktree.
# `_remove_loom_worktree` is stubbed to really `git worktree remove --force`
# the path, so a wrongly-dispatched removal would destroy it here.
echo ""
echo "Test 8: --worktree-path with a daemon lacking 'worktree-contains' (exit 2)"

extract_top_block() { # <exact-first-line> <file>: top-level `if` through its `fi`
    FIRST="$1" awk '$0 == ENVIRON["FIRST"] { grab=1 } grab { print } grab && /^fi$/ { exit }' "$2"
}
# The block headers and the expected call are literal merge-pr.sh source text.
# shellcheck disable=SC2016
WTC_VALIDATE="$(extract_top_block 'if [[ -n "$WORKTREE_PATH_OVERRIDE" ]]; then' "$MERGE_PR")"
# shellcheck disable=SC2016
WTC_CLEANUP="$(extract_top_block 'if [[ "$CLEANUP_WORKTREE" == "true" ]]; then' "$MERGE_PR")"
# shellcheck disable=SC2016
if [[ "$WTC_VALIDATE" != *"merge-pr worktree-contains"* || "$WTC_CLEANUP" != *'_remove_loom_worktree "$WORKTREE_PATH_OVERRIDE" "true"'* ]]; then
    fail "could not extract the --worktree-path validation / cleanup dispatch blocks from $MERGE_PR"
else
    WTC_TMP="$(mktemp -d)"; WTC_TMP="$(cd "$WTC_TMP" && pwd -P)"
    git init -q "$WTC_TMP/repo"
    git -C "$WTC_TMP/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
    git -C "$WTC_TMP/repo" worktree add -q -b feature/issue-8191 "$WTC_TMP/wt" 2>/dev/null
    cat > "$WTC_TMP/no-verb-daemon" <<'FAKEDAEMON'
#!/usr/bin/env bash
cat >/dev/null
echo "error: unrecognized subcommand 'worktree-contains'" >&2
exit 2
FAKEDAEMON
    chmod +x "$WTC_TMP/no-verb-daemon"

    # run_wtc <daemon-bin>: validation block, a merge marker, then the cleanup
    # dispatch -- in a subshell so the eval'd `error` exit cannot kill the suite.
    # shellcheck disable=SC2030,SC2034,SC2329  # subshell-local on purpose; the stubs and vars serve the eval'd blocks
    run_wtc() (
        set +e
        export LOOM_DAEMON_BIN="$1"; unset LOOM_PRESERVE_WORKTREE
        REPO_ROOT="$WTC_TMP/repo"; CLEANUP_WORKTREE=true; WORKTREE_PATH_OVERRIDE="$WTC_TMP/wt"
        info() { echo "INFO: $*"; }; warning() { echo "WARN: $*"; }
        error() { echo "ERROR: $*"; exit 1; }
        _remove_loom_worktree() { echo "REMOVED: $1"; git -C "$REPO_ROOT" worktree remove --force "$1"; }
        eval "$WTC_VALIDATE"; echo "validated; merge proceeds"
        eval "$WTC_CLEANUP"
    )

    out="$(run_wtc "$WTC_TMP/no-verb-daemon" 2>&1)"
    if [[ "$out" == *"validated; merge proceeds"* && "$out" != *"ERROR:"* ]]; then
        pass "T8a: an older daemon (exit 2) does not block the merge"
    else
        fail "T8a: expected the validation to fall through to the merge; got: $out"
    fi
    if [[ -d "$WTC_TMP/wt" && "$out" != *"REMOVED:"* ]] && \
       wl=$(git -C "$WTC_TMP/repo" worktree list --porcelain) && grep -qxF "worktree $WTC_TMP/wt" <<<"$wl"; then
        pass "T8b: the unverified --worktree-path survives untouched (no removal attempted)"
    else
        fail "T8b: the unverified worktree must never be removed; got: $out"
    fi
    if [[ "$out" == *"WARN:"*"did not run"*"$WTC_TMP/wt is left untouched and worktree cleanup is skipped"* ]]; then
        pass "T8c: the warning names the preserved path and why cleanup was skipped"
    else
        fail "T8c: expected a warning naming the preserved path; got: $out"
    fi

    # Control: the real daemon verifies the same path, so cleanup IS dispatched
    # to it -- proves T8b's survival is the fail-safe, not a dead harness.
    # shellcheck disable=SC2031  # the outer, unmodified value is the one wanted
    out="$(run_wtc "${LOOM_DAEMON_BIN:-loom-daemon}" 2>&1)"
    if [[ "$out" == *"REMOVED: $WTC_TMP/wt"* && ! -d "$WTC_TMP/wt" ]]; then
        pass "T8d: control -- a verified --worktree-path is still cleaned up"
    else
        fail "T8d: control: expected the verified path to be removed; got: $out"
    fi
    rm -rf "$WTC_TMP"
fi

# --- Test 9: an older daemon lacking `cleanup-paths` removes NOTHING (#8191
# slice). This is a fail-DIRECTION test, not a "does cleanup work" test, and the
# direction it pins is counter-intuitive enough to be worth stating: when
# `merge-pr cleanup-paths` cannot answer, all three names ($ISSUE_NUM,
# $DEFAULT_WT_PATH, $JUDGE_PR_WT_PATH) are empty, and an empty
# $DEFAULT_WT_PATH fails `[[ -d ]]` -- so WITHOUT the `elif [[ -n
# "$DEFAULT_WT_PATH" ]]` gate the script would fall into the porcelain discovery
# fallback, rediscover this very worktree by branch (a builder worktree at
# issue-<N> tracks feature/issue-<N> and carries .loom-managed), and remove it
# with $ISSUE_NUM empty -- i.e. with #4186's still-open-issue protection
# skipped. The degraded daemon would delete what the healthy one preserves.
# So T9 asserts the *absence* of a removal, and T9d's control proves the harness
# can see a real one.
#
# Runs the REAL post-merge cleanup block extracted from merge-pr.sh against a
# real repo with a real registered worktree; discovery is stubbed to SUCCEED, so
# the only thing standing between the fail-open path and a removal is the gate.
echo ""
echo "Test 9: a daemon lacking 'cleanup-paths' names no targets and removes nothing"

if [[ "$WTC_CLEANUP" != *"merge-pr cleanup-paths"* ]]; then
    fail "could not find the 'merge-pr cleanup-paths' call in the extracted cleanup block"
else
    CP_TMP="$(mktemp -d)"; CP_TMP="$(cd "$CP_TMP" && pwd -P)"
    git init -q "$CP_TMP/repo"
    git -C "$CP_TMP/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
    # The worktree at the Loom-convention path the plan would name, tracking the
    # PR branch -- so both the convention call site and discovery can reach it.
    CP_WT="$CP_TMP/repo/.loom/worktrees/issue-4242"
    git -C "$CP_TMP/repo" worktree add -q -b feature/issue-4242 "$CP_WT" 2>/dev/null
    : > "$CP_WT/.loom-managed"
    cat > "$CP_TMP/no-verb-daemon" <<'FAKEDAEMON'
#!/usr/bin/env bash
cat >/dev/null
echo "error: unrecognized subcommand 'cleanup-paths'" >&2
exit 2
FAKEDAEMON
    chmod +x "$CP_TMP/no-verb-daemon"

    # run_cp <daemon-bin> [block]: the real cleanup block on the NON-override
    # path. [block] defaults to $WTC_CLEANUP; T9b passes a deliberately weakened
    # copy so the gate it pins is the only difference between two live runs.
    # _worktree_cleanup_decide is stubbed to report which call site fired, with
    # what $ISSUE_NUM, and to really remove -- so a wrongly-reached removal both
    # shows up in the log and destroys the worktree.
    # shellcheck disable=SC2030,SC2034,SC2329  # subshell-local on purpose; the stubs and vars serve the eval'd block
    run_cp() (
        set +e
        export LOOM_DAEMON_BIN="$1"; unset LOOM_PRESERVE_WORKTREE
        REPO_ROOT="$CP_TMP/repo"; CLEANUP_WORKTREE=true; WORKTREE_PATH_OVERRIDE=""
        PR_BRANCH="feature/issue-4242"; PR_NUMBER="777"; PR_HEAD_SHA=""
        SCRIPT_DIR="$SCRIPTS_DIR"
        info() { echo "INFO: $*"; }; warning() { echo "WARN: $*"; }
        error() { echo "ERROR: $*"; exit 1; }
        _remove_loom_worktree() { git -C "$REPO_ROOT" worktree remove --force "$1"; }
        _worktree_cleanup_decide() {
            echo "DECIDE: kind=$1 path=$2 issue-num='${ISSUE_NUM:-}'"
            _remove_loom_worktree "$2"
        }
        # Discovery deliberately SUCCEEDS: the gate, not a dead stub, is what
        # must stop the fail-open path from reaching a removal.
        _find_worktree_by_branch() { echo "$CP_WT"; }
        _is_primary_worktree_path() { return 1; }
        _maybe_delete_local_branch() { echo "INFO: branch-delete considered for $1"; }
        eval "${2:-$WTC_CLEANUP}"
    )

    out="$(run_cp "$CP_TMP/no-verb-daemon" 2>&1)"
    if [[ "$out" != *"DECIDE:"* ]] && [[ -d "$CP_WT" ]] && \
       wl=$(git -C "$CP_TMP/repo" worktree list --porcelain) && grep -qxF "worktree $CP_WT" <<<"$wl"; then
        pass "T9a: no cleanup-paths answer => no removal decision at all; the worktree survives"
    else
        fail "T9a: a daemon without 'cleanup-paths' must remove nothing; got: $out"
    fi
    # The specific inversion, named -- and pinned LIVE rather than by restating
    # T9a: run the SAME block with only the `elif [[ -n "$DEFAULT_WT_PATH" ]]`
    # gate weakened back to a bare `else`, and require the removal to APPEAR.
    # Without this half, "no DECIDE: kind=discovered in the output" is only
    # reachable when T9a already established there is no DECIDE: line at all, so
    # it could pass for a reason unrelated to the gate. With it, the assertion
    # pair says: gate present => no discovery; gate absent => discovery removes
    # the worktree with ISSUE_NUM empty (#4186's protection bypassed).
    # Exact-line awk rather than `${var//…}`: the gate's text contains `[[`,
    # which bash would read as a glob bracket expression, so the parameter
    # expansion would silently never match.
    # shellcheck disable=SC2016  # literal merge-pr.sh source text, not an expansion
    WTC_UNGATED="$(printf '%s\n' "$WTC_CLEANUP" \
        | awk -v g='    elif [[ -n "$DEFAULT_WT_PATH" ]]; then' \
              '$0 == g { print "    else"; next } { print }')"
    if [[ "$WTC_UNGATED" == "$WTC_CLEANUP" ]]; then
        fail "T9b: could not find the 'elif [[ -n \"\$DEFAULT_WT_PATH\" ]]' gate to weaken -- the gate this test exists to pin is not in the extracted block"
    else
        ungated_out="$(run_cp "$CP_TMP/no-verb-daemon" "$WTC_UNGATED" 2>&1)"
        if [[ "$out" != *"DECIDE: kind=discovered"* ]] \
           && [[ "$ungated_out" == *"DECIDE: kind=discovered"*"issue-num=''"* ]] \
           && [[ ! -d "$CP_WT" ]]; then
            pass "T9b: the gate is what stops discovery -- removing it makes the degraded path delete the worktree with ISSUE_NUM empty"
        else
            fail "T9b: expected gated=no-discovery / ungated=discovered-removal; gated: $out || ungated: $ungated_out"
        fi
        # Re-register the worktree the ungated run deliberately destroyed, so
        # T9c/T9d below still see the same fixture state T9a did.
        git -C "$CP_TMP/repo" worktree prune 2>/dev/null
        git -C "$CP_TMP/repo" worktree add -q --force "$CP_WT" feature/issue-4242 2>/dev/null
        : > "$CP_WT/.loom-managed"
    fi
    if [[ "$out" == *"WARN:"*"cleanup-paths"*"Nothing is removed rather than guessed"* ]]; then
        pass "T9c: the warning says which verb was missing and that nothing was removed"
    else
        fail "T9c: expected a warning naming cleanup-paths; got: $out"
    fi

    # Control: the real daemon names the plan, so the convention call site IS
    # reached, WITH the issue number -- proves T9a/T9b are live assertions.
    # shellcheck disable=SC2031  # the outer, unmodified value is the one wanted
    out="$(run_cp "${LOOM_DAEMON_BIN:-loom-daemon}" 2>&1)"
    if [[ "$out" == *"DECIDE: kind=default path=$CP_WT issue-num='4242'"* && ! -d "$CP_WT" ]]; then
        pass "T9d: control -- a real plan reaches the convention call site with ISSUE_NUM set"
    else
        fail "T9d: control: expected the planned convention path to be decided/removed; got: $out"
    fi
    rm -rf "$CP_TMP"
fi

# --- Test 10: a NON-`feature/issue-<N>` branch survives the tab-framed parse
# (#8191 slice). This is the mirror image of T9d's control, and it exists because
# T9 only ever drove an issue branch -- where field 1 (the issue number) is
# non-empty, so nothing about the framing is under strain.
#
# For a PR-only branch (`docs/...`, `security/...`, `fix/...`, or a slice branch
# like `feature/issue-8195-slice-13` that the strict anchor rejects) the verb has
# no issue number and no #6264 review path to name, so TWO of the four fields are
# empty. Tab is an IFS *whitespace* character, so bash's `read` strips a leading
# run of it and collapses runs of it: an empty LEADING field is unrecoverable.
# With the fields ordered issue-first, `LOOM-CLEANUP-PATHS\t\t<default>\t` parsed
# as ISSUE_NUM=<default> with BOTH path names empty -- and because the verb had
# exited 0 with a well-formed sentinel, the fail-open warning never fired and the
# `elif [[ -n "$DEFAULT_WT_PATH" ]]` gate then swallowed the whole removal path.
# Post-merge cleanup silently did nothing for ~18% of merged PRs (and the local
# branch leaked too, still checked out in the surviving worktree).
#
# So this drives the SAME real extracted $WTC_CLEANUP block and the SAME real
# daemon as T9d, changing only $PR_BRANCH, and requires the default-path field to
# survive the round-trip non-empty -- observable as the convention call site
# firing at <root>/pr-<PR> with an empty ISSUE_NUM.
echo ""
echo "Test 10: a non-feature/issue-<N> branch's default path survives the tab-framed parse"

if [[ "$WTC_CLEANUP" != *"merge-pr cleanup-paths"* ]]; then
    fail "T10: could not find the 'merge-pr cleanup-paths' call in the extracted cleanup block"
else
    NI_TMP="$(mktemp -d)"; NI_TMP="$(cd "$NI_TMP" && pwd -P)"
    git init -q "$NI_TMP/repo"
    git -C "$NI_TMP/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
    # The path the plan names for a PR-only branch: pr-<PR_NUMBER>, which is this
    # branch's DEFAULT target (not a co-existing #6264 review worktree).
    NI_WT="$NI_TMP/repo/.loom/worktrees/pr-777"
    git -C "$NI_TMP/repo" worktree add -q -b fix/foo-bar "$NI_WT" 2>/dev/null
    : > "$NI_WT/.loom-managed"

    # shellcheck disable=SC2030,SC2034,SC2329  # subshell-local on purpose; the stubs and vars serve the eval'd block
    run_ni() (
        set +e
        export LOOM_DAEMON_BIN="$1"; unset LOOM_PRESERVE_WORKTREE
        REPO_ROOT="$NI_TMP/repo"; CLEANUP_WORKTREE=true; WORKTREE_PATH_OVERRIDE=""
        PR_BRANCH="fix/foo-bar"; PR_NUMBER="777"; PR_HEAD_SHA=""
        SCRIPT_DIR="$SCRIPTS_DIR"
        info() { echo "INFO: $*"; }; warning() { echo "WARN: $*"; }
        error() { echo "ERROR: $*"; exit 1; }
        _remove_loom_worktree() { git -C "$REPO_ROOT" worktree remove --force "$1"; }
        _worktree_cleanup_decide() {
            echo "DECIDE: kind=$1 path=$2 issue-num='${ISSUE_NUM:-}'"
            _remove_loom_worktree "$2"
        }
        # Discovery must never be needed here: the convention path EXISTS, so a
        # correctly-parsed plan takes the `[[ -d ]]` branch. If it is reached,
        # the parse lost $DEFAULT_WT_PATH.
        _find_worktree_by_branch() { echo "$NI_WT"; }
        _is_primary_worktree_path() { return 1; }
        _maybe_delete_local_branch() { echo "INFO: branch-delete considered for $1"; }
        eval "${2:-$WTC_CLEANUP}"
    )

    # shellcheck disable=SC2031  # the outer, unmodified value is the one wanted
    ni_out="$(run_ni "${LOOM_DAEMON_BIN:-loom-daemon}" 2>&1)"
    if [[ "$ni_out" == *"DECIDE: kind=default path=$NI_WT issue-num=''"* && ! -d "$NI_WT" ]]; then
        pass "T10a: the default-path field survives the read non-empty; the pr-<N> worktree is cleaned up"
    else
        fail "T10a: expected the convention call site at $NI_WT with an empty ISSUE_NUM (an empty \$DEFAULT_WT_PATH means the tab framing lost the leading field); got: $ni_out"
    fi
    # The parse succeeded, so the fail-open warning must NOT have fired. Without
    # this, a future regression that broke the verb outright would still satisfy
    # T10a's "no removal" half if the assertion were ever weakened to that.
    if [[ "$ni_out" != *"WARN:"*"cleanup-paths"* ]]; then
        pass "T10b: no fail-open warning -- the verb answered and the shell parsed its answer"
    else
        fail "T10b: unexpected cleanup-paths fail-open warning on a healthy daemon; got: $ni_out"
    fi
    # #6264's asymmetry, end to end: a PR-only branch names no SECOND pr-<N>
    # path, so exactly one decision is taken -- not a duplicate of the first.
    if [[ "$(grep -c "DECIDE:" <<<"$ni_out")" == "1" && "$ni_out" != *"DECIDE: kind=judge-pr"* ]]; then
        pass "T10c: exactly one decision -- the #6264 judge-pr call site stays empty for a PR-only branch"
    else
        fail "T10c: expected exactly one DECIDE line and no judge-pr call site; got: $ni_out"
    fi
    rm -rf "$NI_TMP"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
