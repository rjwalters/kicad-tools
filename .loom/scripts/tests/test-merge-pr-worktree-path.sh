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
#   4. The cleanup decision tree, driven for REAL (#8191 orchestrator slice 1:
#      the REAL `_post_merge_cleanup` wrapper extracted from merge-pr.sh and the
#      REAL `merge-pr post-merge-cleanup` verb, against real git worktrees —
#      this used to be a pure-bash model of the tree, which could not notice the
#      script drifting from it):
#      - LOOM_PRESERVE_WORKTREE=1 wins
#      - --no-cleanup-worktree wins over --worktree-path
#      - --worktree-path bypasses sentinel on the explicit path
#      - default path keeps sentinel guard
#      - discovery fallback emits hint without removing
#   7. The lazy forge-fact protocol (#4186): the verb asks for close targets and
#      the issue state only when it needs them, and a failed read preserves —
#      including the daemon-fault path (fail-unsafe-to-preserve).
#
# This is the companion to test-merge-pr-help.sh. The help test verifies
# the documentation surface; this test verifies the implementation surface.

# Globals assigned below (REPO_ROOT, PR_*, CLEANUP_*, ...) are read by the
# _post_merge_cleanup wrapper the harness extracts from merge-pr.sh, which the
# linter cannot see.
# shellcheck disable=SC2034
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"
FORGE_HELPERS="$SCRIPTS_DIR/lib/forge-helpers.sh"

# #8191: the cleanup is `loom-daemon merge-pr post-merge-cleanup`, and the CLI's
# own --worktree-path validation (Test 1 below) is `merge-pr worktree-contains`.
# Pin the binary those exec and verify it HAS the subcommands, mirroring every
# other ported-decision suite in this family.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "merge-pr post-merge-cleanup" "merge-pr worktree-contains"
# #9548: merge-pr.sh vets its repo (`loom_write_repo`) before it parses a
# single flag, so the cases that run it (Tests 1 and 3) run it from a scratch
# checkout registered as owner/repo (write-scope-fixture.sh), never from this
# checkout, with an offline stub `gh` first on PATH. The scratch checkout's own
# linked worktree is Test 3's registered --worktree-path.
# shellcheck source=lib/write-scope-fixture.sh
source "$SCRIPT_DIR/lib/write-scope-fixture.sh"
MP_FIX="$(mktemp -d)"; MP_FIX="$(cd "$MP_FIX" && pwd -P)"
trap 'rm -rf "$MP_FIX"' EXIT
mkdir -p "$MP_FIX/bin"
printf '#!/usr/bin/env bash\necho "stub gh: offline ($*)" >&2\nexit 1\n' > "$MP_FIX/bin/gh"
chmod +x "$MP_FIX/bin/gh"
git init -q "$MP_FIX/repo"
git -C "$MP_FIX/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
WRITE_SCOPE_INNER_GH="$MP_FIX/bin/gh" write_scope_register "$MP_FIX/repo" owner/repo
git -C "$MP_FIX/repo" worktree add -q -b feature/issue-1 "$MP_FIX/wt" 2>/dev/null
# merge_pr <args...>: the real merge-pr.sh, run from the registered checkout.
merge_pr() { (cd "$MP_FIX/repo" && PATH="$MP_FIX/bin:$PATH" "$MERGE_PR" "$@"); }

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

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
out=$(merge_pr 1 --worktree-path 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"--worktree-path requires a value"* ]]; then
    pass "missing value for --worktree-path errors with rc!=0 and clear message"
else
    fail "missing value: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# Nonexistent path
set +e
out=$(merge_pr --worktree-path /nonexistent-loom-test-path 1 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"does not exist"* ]]; then
    pass "nonexistent --worktree-path errors with rc!=0 and clear message"
else
    fail "nonexistent path: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# Path exists but is not a registered worktree of this repo
set +e
out=$(merge_pr --worktree-path /tmp 1 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"not a registered worktree"* ]]; then
    pass "unregistered --worktree-path errors with rc!=0 and clear message"
else
    fail "unregistered path: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# --- Test 2: Source contains the expected logic surface ---
echo ""
echo "Test 2: where the --worktree-path / cleanup logic lives now (#8191 orchestrator slice 1)"

RUST_DIR="$(cd "$SCRIPTS_DIR/../.." && pwd)/loom-daemon/src/merge_pr"
ORCH="$RUST_DIR/post_merge_cleanup.rs"

assert_grep "WORKTREE_PATH_OVERRIDE=" "$MERGE_PR" \
    "merge-pr.sh declares WORKTREE_PATH_OVERRIDE state variable"
assert_grep '[-]-worktree-path "\$WORKTREE_PATH_OVERRIDE"' "$MERGE_PR" \
    "the post-merge cleanup wrapper hands the validated override to the verb"
retired "the greps for merge-pr.sh's _find_worktree_by_branch / _worktree_branch_for / _maybe_delete_local_branch helpers, 'git branch -d', and _remove_loom_worktree's allow_unmanaged second argument" \
    "the cleanup finds a worktree by branch, learns an override's attached branch, tidies the local branch safely (-d, escalating to -D only when landed) and takes an explicit opt-in to bypass the sentinel" \
    "#8191 orchestrator slice 1 moved all of them into loom-daemon (merge_pr/post_merge_cleanup.rs, in-process), so merge-pr.sh no longer contains those names" \
    "the assertions immediately below (the orchestrator's own discovery, override and branch-delete code), Tests 4-6 and 10 (the REAL wrapper and verb against real git repos: every branch of the decision tree), and post_merge_cleanup/tests.rs::the_override_path_bypasses_the_sentinel_and_deletes_its_attached_branch_with_no_sha"
assert_grep 'worktrees::find_by_branch' "$ORCH" \
    "the orchestrator's discovery fallback walks the porcelain by branch"
assert_grep 'worktrees::branch_for_path' "$ORCH" \
    "the orchestrator learns an override path's attached branch from the porcelain"
assert_grep 'allow_unmanaged' "$ORCH" \
    "remove_loom_worktree takes allow_unmanaged (the explicit --worktree-path opt-in)"
assert_grep 'maybe_delete_local_branch' "$ORCH" \
    "the orchestrator ends in the safe local-branch delete"
retired "the grep for the in-shell 'Bypassing sentinel guard' text" \
    "explicit --worktree-path logs the sentinel-bypass action" \
    "the sentinel test and its message left merge-pr.sh in the #8191 remove-gate slice; they are Rust in loom-daemon/src/merge_pr/remove_gate.rs, so no grep of this file can pass" \
    "case C below (the REAL cleanup against a real unmarked worktree; the bypass is asserted from the output), loom-daemon/tests/merge_pr_remove_gate_differential.rs (frozen retired block vs the verb, allow=true/sentinel=absent) and an_unmarked_worktree_is_refused_unless_opted_in in src/merge_pr/remove_gate/tests.rs"
retired "the grep for the in-shell 'Discovered worktree for branch' / 're-run with: --worktree-path' text" \
    "the discovery fallback emits a hint naming the discovered path and suggesting --worktree-path for a user-owned worktree" \
    "the user-owned advice moved out of merge-pr.sh in the #8191 discovered-worktree slice; it is Rust in loom-daemon/src/merge_pr/discovered_worktree.rs, so no grep of this file can pass" \
    "case G below (the REAL cleanup against a real user-owned worktree), loom-daemon/tests/merge_pr_discovered_worktree_differential.rs (frozen retired block vs the verb, user-owned case byte for byte) and a_user_owned_worktree_is_never_decided_and_gets_four_warnings in src/merge_pr/discovered_worktree/tests.rs"
assert_grep 'discovered_worktree::decide' "$ORCH" \
    "discovery fallback delegates its classification to discovered_worktree::decide"

# --- Test 2b: async-close-race guard (#4186) source surface ---
retired "the greps for merge-pr.sh's _issue_is_closed_for_cleanup helper" \
    "a close-target-aware cleanup gate exists (#4186)" \
    "#8191 orchestrator slice 1: the helper is Rust (post_merge_cleanup.rs::issue_is_closed, over merge_pr/issue_close_gate.rs)" \
    "Tests 5 and 7 below"
assert_grep 'forge_pr_close_targets' "$MERGE_PR" \
    "merge-pr.sh's cleanup wrapper supplies forge_pr_close_targets when the verb asks (async-close-race adaptation)"
assert_grep 'forge_get_issue_state' "$MERGE_PR" \
    "merge-pr.sh's cleanup wrapper supplies forge_get_issue_state for non-close-target issues"
retired \
    "default-path preserved-worktree case logs a clear reason" \
    "the operator-visible preserve message names why a worktree at the Loom-convention path was left in place" \
    "#8191: the #4186/#6694 remove-vs-preserve decision, and the message text it builds, moved to loom-daemon/src/merge_pr/worktree_preserve.rs — the retired \"Preserving worktree at\" string is not a shell string anywhere in this file to grep for" \
    "case J below, which asserts the text reaches the operator from the REAL cleanup, loom-daemon/tests/merge_pr_worktree_preserve_differential.rs, which drives the frozen retired block (tests/fixtures/merge-pr-worktree-preserve-retired.sh) against the real CLI on a shared corpus and asserts the preserve message is unchanged byte for byte, plus loom-daemon's merge_pr::worktree_preserve::tests::preserve_check_and_not_landed_preserves_with_two_lines"
assert_grep "forge_get_issue_state" "$FORGE_HELPERS" \
    "forge-helpers.sh defines forge_get_issue_state"

# --- Test 2c: co-existing Judge review worktree cleanup (#6264) source surface ---
retired "the grep for merge-pr.sh's JUDGE_PR_WT_PATH variable" \
    "a co-existing Judge/Doctor pr-<N> review worktree is cleaned up alongside issue-<N> (#6264)" \
    "#8191: the name is a field of the cleanup-paths plan consumed in-process now, not a shell variable" \
    "cases O-S below (the REAL cleanup against real issue-<N> and pr-<N> worktrees) and the cleanup-paths differential named next"
retired \
    "JUDGE_PR_WT_PATH is set to pr-\$PR_NUMBER only on the feature/issue-<N> branch" \
    "#6264's asymmetry: a co-existing Judge/Doctor pr-<N> review worktree is named only for an ordinary feature/issue-<N> PR, never for an external-fork/ad-hoc branch whose own default path is already pr-<N> (naming it there would make the second call site a pure duplicate of the first)" \
    "#8191: the assignment left the shell. All three names now arrive together from \`loom-daemon merge-pr cleanup-paths\`, consumed in-process, so there is no JUDGE_PR_WT_PATH= assignment — and no \$WT_ROOT_DIR at all — left in this file to grep for" \
    "loom-daemon/tests/merge_pr_cleanup_paths_differential.rs, which drives the frozen retired block (tests/fixtures/merge-pr-cleanup-paths-retired.sh) against the port over every branch shape and asserts the asymmetry as an invariant of every case (issue field non-empty <=> judge-pr field non-empty), plus merge_pr::cleanup_paths::tests::a_non_issue_branch_names_only_the_pr_worktree; cases O-S and Test 10 below exercise the behaviour end to end"
retired \
    "co-existing pr-<N> removal logs a clear reason (#6264)" \
    "the operator-visible removal message for a co-existing Judge/Doctor review worktree names why (#6264)" \
    "#8191: same consolidation as the default/discovered messages above — the text is now loom-daemon/src/merge_pr/worktree_preserve.rs's Kind::JudgePr branch" \
    "case O below asserts the text from the REAL cleanup; loom-daemon/tests/merge_pr_worktree_preserve_differential.rs's Kind::JudgePr cases, plus merge_pr::worktree_preserve::tests::no_preserve_check_judge_pr_removes_with_a_note"
retired \
    "co-existing pr-<N> preserved-worktree case logs a clear reason (#6264)" \
    "the operator-visible preserve message for a co-existing Judge/Doctor review worktree names why (#6264)" \
    "#8191: same consolidation — the \"Preserving Judge/Doctor review worktree at\" text is loom-daemon/src/merge_pr/worktree_preserve.rs's Kind::JudgePr preserve branch, not a shell string" \
    "case Q below asserts the text from the REAL cleanup; loom-daemon/tests/merge_pr_worktree_preserve_differential.rs's Kind::JudgePr preserve cases, plus merge_pr::worktree_preserve::tests::judge_pr_kind_uses_its_own_noun_in_both_6694_messages"

# --- Test 2d: never-closing-issue worktree/branch cleanup (#6694) source surface ---
assert_grep 'source "\$SCRIPT_DIR/lib/branch-landed.sh"' "$MERGE_PR" \
    "merge-pr.sh sources the shared branch-landed primitive (#7812)"
assert_grep 'branch_has_landed "\$PR_BRANCH" "\$\{DEFAULT_BRANCH_NAME:-\}" "\$\{PR_HEAD_SHA:-\}"' "$MERGE_PR" \
    "the cleanup wrapper answers the verb's landed question with the shared primitive (#6694/#7812)"
retired \
    "_maybe_delete_local_branch delegates its safety check to the shared primitive (#6694/#7812)" \
    "the local-branch -d -> -D upgrade is gated on the shared branch-landed verdict (fed the merged PR's head SHA), never a private re-derivation" \
    "#8191: the decision left the shell. The local-branch delete is worktree_cli::branch_delete, which calls worktree_cli::branch_landed::probe (the Rust twin of lib/branch-landed.sh that worktree.sh remove already uses) — the shell holds no delete call to grep for" \
    "loom-daemon's branch_delete::tests::only_a_landed_verdict_may_escalate_to_force_delete, the orchestrator's delete_branch(&i.pr_branch, &i.pr_head_sha) call, and the behavioural cases in test-merge-pr-local-branch-cleanup.sh / test-merge-pr-primary-checkout-advice.sh that force-delete on a tip match"
retired \
    "a fully-captured branch is cleaned up even when the issue-close gate says preserve (#6694)" \
    "when the #4186 issue gate says preserve but branch_has_landed says the branch's content is already on the default branch, cleanup proceeds anyway and says so" \
    "#8191: the landed-branch override IS the decision that moved — it is loom-daemon/src/merge_pr/worktree_preserve.rs's \`ctx.preserve_check && ctx.landed\` arm, and the \"holds nothing unmerged; removing it (#6694)\" text is a Rust format string, not a shell string this file can grep for" \
    "cases T/V/W in Test 5b below, which drive the REAL cleanup and observe the worktree actually being removed with that message; loom-daemon/tests/merge_pr_worktree_preserve_differential.rs (saw_landed_override_remove) and merge_pr::worktree_preserve::tests::preserve_check_and_landed_removes_with_one_line"
retired \
    "the reworded preserve message names a remedy that does not depend on the issue closing (#6694)" \
    "the preserve message tells an operator how to clean up by hand when the issue is a programme issue that will never close, so the automatic retry never fires" \
    "#8191: same move — the second (info) line of the preserve branch is built in loom-daemon/src/merge_pr/worktree_preserve.rs, so \"designed never to close (#6694), that retry never fires: remove manually\" is not a shell string in this file" \
    "case U below asserts the remedy line from the REAL cleanup; loom-daemon/tests/merge_pr_worktree_preserve_differential.rs (the preserve cases compare both lines byte for byte against the frozen shell), plus merge_pr::worktree_preserve::tests::preserve_check_and_not_landed_preserves_with_two_lines"

# --- Test 3: Precedence — --no-cleanup-worktree warns when combined ---
echo ""
echo "Test 3: --no-cleanup-worktree wins over --worktree-path"

# This requires a registered worktree path: the registered scratch checkout's
# own linked worktree (resolved via porcelain, git's canonical recorded path).
ACTUAL_WT="$(cd "$MP_FIX/wt" && git rev-parse --show-toplevel 2>/dev/null || echo "")"

if [[ -n "$ACTUAL_WT" ]] && git -C "$ACTUAL_WT" worktree list --porcelain 2>/dev/null | \
   awk -v p="$ACTUAL_WT" '/^worktree / { if ($2 == p) { found=1; exit } } END { exit !found }'; then
    # ACTUAL_WT is a valid --worktree-path value. Run with --dry-run so the merge itself short-circuits (we only want to see
    # the validation + warning).
    set +e
    out=$(merge_pr --no-cleanup-worktree --worktree-path "$ACTUAL_WT" 1 --dry-run 2>&1)
    rc=$?
    set -e
    if [[ "$out" == *"--no-cleanup-worktree wins"* ]]; then
        pass "combining --no-cleanup-worktree + --worktree-path warns"
    else
        fail "expected '--no-cleanup-worktree wins' warning; got: $out"
    fi
else
    fail "the scratch checkout's linked worktree is not registered: $MP_FIX/wt"
fi

# --- The REAL cleanup, driven against real git repos (replaces the inline
# simulations Tests 4-6 used to run, which modelled the decision tree in pure
# bash and so could not catch the script's own drift) ---
#
# shellcheck source=lib/post-merge-cleanup-harness.sh
source "$SCRIPT_DIR/lib/post-merge-cleanup-harness.sh"
pmc_load "$MERGE_PR"

SIM_TMP="$(mktemp -d "${TMPDIR:-/tmp}/loom-merge-wtpath.XXXXXX")"; SIM_TMP="$(cd "$SIM_TMP" && pwd -P)"
trap 'rm -rf "$MP_FIX" "$SIM_TMP"' EXIT

# sim_repo -> sets R (a fresh repo, default branch main, .loom/ ignored the way
# every Loom repo ignores the removal ledger).
sim_repo() {
    SIM_D="$(mktemp -d "$SIM_TMP/case.XXXXXX")"
    R="$SIM_D/repo"; mkdir -p "$R"
    git -C "$R" init -q -b main
    git -C "$R" config user.email t@t; git -C "$R" config user.name t
    git -C "$R" commit -q --allow-empty -m init
    printf '.loom/\n' >> "$R/.git/info/exclude"
}

# sim_wt <path> <branch> <sentinel:true|false>: a linked worktree carrying one
# commit the default branch does not have (so it is genuinely UNLANDED unless the
# caller hands the verb its tip as the merged head). A real file, not an empty
# commit: an empty commit leaves the tree equal to main's, which the shared
# landed primitive correctly calls landed.
sim_wt() {
    mkdir -p "${1%/*}"
    git -C "$R" worktree add -q -b "$2" "$1" >/dev/null 2>&1
    echo "work on $2" > "$1/work.txt"; git -C "$1" add work.txt
    git -C "$1" commit -q -m "work on $2"
    [[ "$3" != "true" ]] || : > "$1/.loom-managed"
}

# simulate_cleanup: the SAME parameters and result tokens the retired inline
# model used, but every answer now comes from running the real wrapper and the
# real verb against real worktrees, and observing the filesystem and output.
simulate_cleanup() {
    local preserve="$1" cleanup="$2" override="$3" default_exists="$4" \
          override_has_sentinel="$5" discovered="$6" discovered_has_sentinel="$7" \
          issue_num="${8:-}" is_close_target="${9:-false}" issue_state="${10:-}" \
          branch_fully_captured="${11:-false}"
    sim_repo
    local branch dflt ovr="" disc="" out
    if [[ -n "$issue_num" ]]; then
        branch="feature/issue-$issue_num"; dflt="$R/.loom/worktrees/issue-$issue_num"
    else
        branch="fix/foo-bar"; dflt="$R/.loom/worktrees/pr-777"
    fi
    if [[ -n "$override" ]]; then
        ovr="$SIM_D/ovr"; sim_wt "$ovr" "feature/ovr" "$override_has_sentinel"
    fi
    if [[ "$default_exists" == "true" ]]; then
        sim_wt "$dflt" "$branch" true
    elif [[ -n "$discovered" ]]; then
        disc="$SIM_D/disc"; sim_wt "$disc" "$branch" "$discovered_has_sentinel"
    fi
    local tip="" head_sha=""
    tip="$(git -C "$R" rev-parse "refs/heads/$branch" 2>/dev/null || true)"
    if [[ "$branch_fully_captured" == "true" && -n "$tip" ]]; then head_sha="$tip"; else head_sha="0000000000000000000000000000000000000000"; fi

    local close_targets="999"
    [[ "$is_close_target" != "true" ]] || close_targets="$issue_num"
    # shellcheck disable=SC2034  # read by the extracted wrapper
    REPO_ROOT="$R"; PR_NUMBER=777; PR_BRANCH="$branch"; PR_HEAD_SHA="$head_sha"
    CLEANUP_WORKTREE="$cleanup"; WORKTREE_PATH_OVERRIDE="$ovr"
    PMC_CLOSE_TARGETS="$close_targets"; PMC_ISSUE_STATE="$issue_state"
    local lpw=0; [[ "$preserve" != "1" ]] || lpw=1
    out="$(LOOM_PRESERVE_WORKTREE="$lpw" _post_merge_cleanup 2>&1)" || true
    printf '%s' "$out" > "$SIM_TMP/last.out"

    [[ "$cleanup" == "true" ]] || { echo "skip:no-cleanup"; return 0; }
    [[ "$preserve" != "1" ]] || { echo "skip:env"; return 0; }
    if [[ -n "$ovr" ]]; then
        if [[ -d "$ovr" ]]; then echo "unexpected:override-survived"; return 0; fi
        if [[ "$override_has_sentinel" == "true" ]]; then echo "remove:override-managed"; else echo "remove:override-bypass-sentinel"; fi
        return 0
    fi
    local target="$dflt" kind="default"
    if [[ "$default_exists" != "true" && -n "$discovered" ]]; then target="$disc"; kind="discovered"; fi
    if [[ "$default_exists" == "true" || -n "$discovered" ]]; then
        if [[ ! -d "$target" ]]; then
            if [[ "$out" == *"holds nothing unmerged; removing it (#6694)"* ]]; then echo "remove:$kind-fully-captured"
            elif [[ "$kind" == "default" ]]; then echo "remove:default"
            else echo "remove:discovered-managed"; fi
        elif [[ "$kind" == "discovered" && "$discovered_has_sentinel" != "true" ]]; then
            echo "warn:discovered-user-owned"
        else
            echo "preserve:$kind-open-issue"
        fi
        return 0
    fi
    echo "skip:nothing-to-do"
}

# simulate_judge_pr_cleanup: the #6264 co-existing pr-<N> review worktree, driven
# for real. $1 issue_num ("" = external-fork branch), $2 pr_wt_exists, $3
# is_close_target, $4 issue_state, $5 branch_fully_captured.
simulate_judge_pr_cleanup() {
    local issue_num="$1" pr_wt_exists="$2" is_close_target="${3:-false}" issue_state="${4:-}" \
          branch_fully_captured="${5:-false}"
    sim_repo
    local branch="fix/foo-bar" pr_wt="$R/.loom/worktrees/pr-777" out
    [[ -z "$issue_num" ]] || branch="feature/issue-$issue_num"
    # The issue-<N> branch is created but not checked out anywhere; the review
    # worktree is detached at it, as pr-worktree.sh leaves it (#6264).
    git -C "$R" branch "$branch"
    git -C "$R" checkout -q "$branch"
    echo "work on $branch" > "$R/work.txt"; git -C "$R" add work.txt
    git -C "$R" commit -q -m "work on $branch"
    local tip; tip="$(git -C "$R" rev-parse HEAD)"
    git -C "$R" checkout -q main
    if [[ "$pr_wt_exists" == "true" ]]; then
        mkdir -p "${pr_wt%/*}"; git -C "$R" worktree add -q --detach "$pr_wt" "$tip" >/dev/null 2>&1
        : > "$pr_wt/.loom-managed"
    fi
    local head_sha="0000000000000000000000000000000000000000"
    [[ "$branch_fully_captured" != "true" ]] || head_sha="$tip"
    local close_targets="999"
    [[ "$is_close_target" != "true" || -z "$issue_num" ]] || close_targets="$issue_num"
    # shellcheck disable=SC2034
    REPO_ROOT="$R"; PR_NUMBER=777; PR_BRANCH="$branch"; PR_HEAD_SHA="$head_sha"
    CLEANUP_WORKTREE=true; WORKTREE_PATH_OVERRIDE=""
    PMC_CLOSE_TARGETS="$close_targets"; PMC_ISSUE_STATE="$issue_state"
    out="$(LOOM_PRESERVE_WORKTREE=0 _post_merge_cleanup 2>&1)" || true
    printf '%s' "$out" > "$SIM_TMP/last.out"
    if [[ -z "$issue_num" ]]; then
        # An external-fork branch's own default path IS pr-<N>; no SECOND, judge
        # call site runs, so this is not-applicable regardless of the worktree.
        [[ "$out" != *"co-existing Judge/Doctor"* ]] && echo "skip:not-applicable" || echo "unexpected:judge-site-ran"
        return 0
    fi
    [[ "$pr_wt_exists" == "true" ]] || { echo "skip:no-pr-worktree"; return 0; }
    if [[ ! -d "$pr_wt" ]]; then
        if [[ "$out" == *"holds nothing unmerged; removing it (#6694)"* ]]; then echo "remove:judge-pr-worktree-fully-captured"
        else echo "remove:judge-pr-worktree"; fi
    else
        echo "preserve:judge-pr-worktree-open-issue"
    fi
}

# --- Test 4: the cleanup decision tree, against the real thing ---
echo ""
echo "Test 4: cleanup decision tree (REAL wrapper + verb, real worktrees)"

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
if [[ "$result" == "remove:override-bypass-sentinel" && "$(<"$SIM_TMP/last.out")" == *"Bypassing sentinel guard (--worktree-path explicit opt-in"* ]]; then
    pass "case C: --worktree-path bypasses sentinel for non-Loom worktree, and says so"
else
    fail "case C: expected 'remove:override-bypass-sentinel' + bypass message, got '$result' / $(<"$SIM_TMP/last.out")"
fi

# Case D: --worktree-path on Loom-managed worktree — still removes
result=$(simulate_cleanup 0 true "/path/x" true true "" false)
if [[ "$result" == "remove:override-managed" ]]; then
    pass "case D: --worktree-path also removes Loom-managed worktrees"
else
    fail "case D: expected 'remove:override-managed', got '$result'"
fi

# Case E: default path exists — remove via sentinel-guarded path (issue is a
# close target here: the default path on the feature/issue-<N> branch is gated
# by #4186, so a PR-style branch is the gate-free shape).
result=$(simulate_cleanup 0 true "" true false "" false "" false "")
if [[ "$result" == "remove:default" ]]; then
    pass "case E: default Loom-convention path used when present"
else
    fail "case E: expected 'remove:default', got '$result'"
fi

# Case F: default missing, discovered worktree has sentinel — remove
result=$(simulate_cleanup 0 true "" false false "/found" true "" false "")
if [[ "$result" == "remove:discovered-managed" ]]; then
    pass "case F: discovery removes Loom-managed worktree at non-standard path"
else
    fail "case F: expected 'remove:discovered-managed', got '$result'"
fi

# Case G: default missing, discovered worktree LACKS sentinel — warn-only
result=$(simulate_cleanup 0 true "" false false "/found" false "" false "")
if [[ "$result" == "warn:discovered-user-owned" && "$(<"$SIM_TMP/last.out")" == *"re-run with: --worktree-path"* ]]; then
    pass "case G: discovery warns (naming --worktree-path) but does NOT remove user-owned worktree"
else
    fail "case G: expected 'warn:discovered-user-owned', got '$result' / $(<"$SIM_TMP/last.out")"
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
if [[ "$result" == "preserve:default-open-issue" && "$(<"$SIM_TMP/last.out")" == *"Preserving worktree at"* ]]; then
    pass "case J: non-target open issue preserves the worktree, and says why"
else
    fail "case J: expected 'preserve:default-open-issue', got '$result' / $(<"$SIM_TMP/last.out")"
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
# where the pre-#6694 behavior preserved the worktree/branch indefinitely.
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
# bypass of the issue gate. The preserve message must also name the remedy
# that does not depend on the issue ever closing.
result=$(simulate_cleanup 0 true "" true false "" false 42 false "OPEN" false)
if [[ "$result" == "preserve:default-open-issue" && "$(<"$SIM_TMP/last.out")" == *"that retry never fires: remove manually"* ]]; then
    pass "case U: never-closing issue + branch NOT fully captured -> still genuinely preserved, with a manual-removal remedy"
else
    fail "case U: expected 'preserve:default-open-issue' + remedy line, got '$result' / $(<"$SIM_TMP/last.out")"
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

# The independent JUDGE_PR_WT_PATH check added by #6264 runs ONLY when PR_BRANCH
# matched feature/issue-<N>, keyed purely by whether the pr-$PR_NUMBER path
# EXISTS on disk — independent of the issue-<N> path's own outcome, and
# independent of whether the branch checked out inside it is attached or
# detached (a detached pr-<N> worktree has no branch line for the discovery
# fallback's porcelain search to match, which is exactly the gap #6264 closes
# by checking the path directly instead). Here the pr-<N> worktree is DETACHED.

# Case O: a co-existing pr-<N> Judge review worktree exists for a close-target
# issue and gets removed — deliberately independent of whether the issue-<N>
# worktree also existed (this is the whole point of #6264's fix).
result=$(simulate_judge_pr_cleanup 42 true true)
if [[ "$result" == "remove:judge-pr-worktree" && "$(<"$SIM_TMP/last.out")" == *"Found co-existing Judge/Doctor review worktree at"* ]]; then
    pass "case O: co-existing pr-<N> removed when its issue is a close target (regardless of issue-<N> presence), and says why"
else
    fail "case O: expected 'remove:judge-pr-worktree', got '$result' / $(<"$SIM_TMP/last.out")"
fi

# Case Q: partial-increment shape — issue is NOT a close target and its live
# state is OPEN — preserve the Judge review worktree too, mirroring the
# issue-<N> path's own #4186 gate (a future merge that closes the issue will
# retry cleanup).
result=$(simulate_judge_pr_cleanup 42 true false "OPEN")
if [[ "$result" == "preserve:judge-pr-worktree-open-issue" && "$(<"$SIM_TMP/last.out")" == *"Preserving Judge/Doctor review worktree at"* ]]; then
    pass "case Q: non-target open issue preserves the co-existing pr-<N> worktree too, and says why"
else
    fail "case Q: expected 'preserve:judge-pr-worktree-open-issue', got '$result' / $(<"$SIM_TMP/last.out")"
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

# Case R: external-fork / ad-hoc branch (#3358) — no SECOND (judge) call site
# runs: that worktree is exactly DEFAULT_WT_PATH and is handled by the
# pre-existing pr-<N>-as-default-path logic (Cases A-N above, with no issue).
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

# --- Test 7: the lazy forge-fact protocol (#4186/#8191) ---
echo ""
echo "Test 7: the cleanup asks for forge facts lazily, and a failed read preserves"

# The verb prints `LOOM-NEED <fact>` (exit 3, nothing done) instead of fetching
# anything itself; the REAL wrapper fetches and re-calls. These cases count the
# forge reads the wrapper makes, driven against the real daemon.
GATE_LOG="$SIM_TMP/forge-calls.log"
forge_pr_close_targets() { echo "close-targets" >> "$GATE_LOG"; printf '%s' "$GATE_CLOSE_TARGETS"; }
forge_get_issue_state()  { echo "issue-state $2" >> "$GATE_LOG"; printf '%s' "$GATE_STATE"; }

gate_run() { # <close-targets> <state> -> sets GATE_OUT, GATE_REMOVED (yes|no)
    sim_repo
    local wt="$R/.loom/worktrees/issue-42"
    sim_wt "$wt" feature/issue-42 true
    : > "$GATE_LOG"
    GATE_CLOSE_TARGETS="$1"; GATE_STATE="$2"
    # shellcheck disable=SC2034
    REPO_ROOT="$R"; PR_NUMBER=999; PR_BRANCH="feature/issue-42"; PR_HEAD_SHA="0000000000000000000000000000000000000000"
    CLEANUP_WORKTREE=true; WORKTREE_PATH_OVERRIDE=""
    GATE_OUT="$(LOOM_PRESERVE_WORKTREE=0 _post_merge_cleanup 2>&1)" || true
    if [[ -d "$wt" ]]; then GATE_REMOVED=no; else GATE_REMOVED=yes; fi
}

# T1: issue IS a close target -> clean up, no live-state read needed.
gate_run "42" ""
if [[ "$GATE_REMOVED" == yes ]] && ! grep -q '^issue-state' "$GATE_LOG"; then
    pass "T1: a close-target issue authorizes cleanup with no live-state read"
else
    fail "T1: expected removal with no issue-state read; removed=$GATE_REMOVED, calls: $(cat "$GATE_LOG")"
fi

# T2: not a close target, live state CLOSED -> clean up (and the read happened).
gate_run "7" "CLOSED"
if [[ "$GATE_REMOVED" == yes ]] && grep -qx 'issue-state 42' "$GATE_LOG"; then
    pass "T2: a non-close-target issue with live state CLOSED authorizes cleanup (after exactly the state read)"
else
    fail "T2: expected removal after an issue-state read; removed=$GATE_REMOVED, calls: $(cat "$GATE_LOG")"
fi

# T3: not a close target, live state OPEN -> preserve.
gate_run "7" "OPEN"
if [[ "$GATE_REMOVED" == no ]]; then
    pass "T3: a non-close-target issue with live state OPEN preserves"
else
    fail "T3: expected preserve for an OPEN non-close-target issue"
fi

# T4: not a close target, live-state lookup failure (empty) -> preserve
# (fail-unsafe-to-preserve).
gate_run "7" ""
if [[ "$GATE_REMOVED" == no ]]; then
    pass "T4: a live-state lookup failure preserves (fail-unsafe-to-preserve)"
else
    fail "T4: expected preserve on a lookup failure"
fi

# T4b: a PR-only branch (no issue number) reads NO forge fact at all.
sim_repo
: > "$GATE_LOG"
sim_wt "$R/.loom/worktrees/pr-5" fix/foo-bar true
# shellcheck disable=SC2034
REPO_ROOT="$R"; PR_NUMBER=5; PR_BRANCH="fix/foo-bar"; PR_HEAD_SHA=""
LOOM_PRESERVE_WORKTREE=0 _post_merge_cleanup >/dev/null 2>&1 || true
if [[ ! -s "$GATE_LOG" && ! -d "$R/.loom/worktrees/pr-5" ]]; then
    pass "T4b: a PR-only branch is cleaned up without a single forge read"
else
    fail "T4b: expected removal with no forge reads; calls: $(cat "$GATE_LOG")"
fi

# T5 (#8191): a daemon predating the verb must remove NOTHING (never guess
# cleanup) AND warn naming the fault — a guessed removal here is the exact
# irreversible mistake #4186 exists to prevent.
gate_run_fake() { # <fake-daemon>
    sim_repo
    local wt="$R/.loom/worktrees/issue-42"
    sim_wt "$wt" feature/issue-42 true
    GATE_CLOSE_TARGETS="42"; GATE_STATE=""
    # shellcheck disable=SC2034
    REPO_ROOT="$R"; PR_NUMBER=999; PR_BRANCH="feature/issue-42"; PR_HEAD_SHA=""
    GATE_OUT="$(LOOM_DAEMON_SELF_BIN="$1" LOOM_PRESERVE_WORKTREE=0 _post_merge_cleanup 2>&1)" || true
    if [[ -d "$wt" ]]; then GATE_REMOVED=no; else GATE_REMOVED=yes; fi
}
fake_no_verb="$SIM_TMP/fake-loom-daemon-no-verb"
cat > "$fake_no_verb" <<'FAKEDAEMON'
#!/usr/bin/env bash
echo "error: unrecognized subcommand 'post-merge-cleanup'" >&2
exit 2
FAKEDAEMON
chmod +x "$fake_no_verb"
gate_run_fake "$fake_no_verb"
if [[ "$GATE_REMOVED" == no ]]; then
    pass "T5: a daemon predating the verb removes nothing rather than guessing cleanup"
else
    fail "T5: expected the worktree to survive when the daemon lacks the verb"
fi
if [[ "$GATE_OUT" == *"did not run"* ]]; then
    pass "T5: the daemon-fault path warns that the cleanup did not run"
else
    fail "T5: expected a warning naming the fault; got: $GATE_OUT"
fi

# T6: a verb that keeps asking for facts it never accepts must not loop: the
# wrapper gives up after a bounded number of rounds and removes nothing.
fake_needy="$SIM_TMP/fake-loom-daemon-needy"
cat > "$fake_needy" <<'FAKEDAEMON'
#!/usr/bin/env bash
echo "LOOM-NEED close-targets"
exit 3
FAKEDAEMON
chmod +x "$fake_needy"
gate_run_fake "$fake_needy"
if [[ "$GATE_REMOVED" == no && "$GATE_OUT" == *"did not run"* ]]; then
    pass "T6: a verb that never stops asking is bounded; nothing is removed and the operator is told"
else
    fail "T6: expected a bounded give-up; removed=$GATE_REMOVED out: $GATE_OUT"
fi

# --- Test 8: --worktree-path with a daemon lacking 'worktree-contains' (exit 2)
# never removes the unverified path (#8191 slice; Judge's fail-safe fix on PR
# #9602). The registered-worktree check is a guard, and a guard that did not run
# must refuse the removal. Runs the REAL parse-time validation block extracted
# from merge-pr.sh, then the REAL cleanup wrapper, against a real repo and
# registered worktree.
echo ""
echo "Test 8: --worktree-path with a daemon lacking 'worktree-contains' (exit 2)"

extract_top_block() { # <exact-first-line> <file>: top-level `if` through its `fi`
    FIRST="$1" awk '$0 == ENVIRON["FIRST"] { grab=1 } grab { print } grab && /^fi$/ { exit }' "$2"
}
# The block header and the expected call are literal merge-pr.sh source text.
# shellcheck disable=SC2016
WTC_VALIDATE="$(extract_top_block 'if [[ -n "$WORKTREE_PATH_OVERRIDE" ]]; then' "$MERGE_PR")"
if [[ "$WTC_VALIDATE" != *"merge-pr worktree-contains"* ]]; then
    fail "could not extract the --worktree-path validation block from $MERGE_PR"
else
    WTC_TMP="$(mktemp -d)"; WTC_TMP="$(cd "$WTC_TMP" && pwd -P)"
    git init -q "$WTC_TMP/repo"
    git -C "$WTC_TMP/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
    git -C "$WTC_TMP/repo" worktree add -q -b feature/issue-8191 "$WTC_TMP/wt" 2>/dev/null
    printf '.loom/\n' >> "$WTC_TMP/repo/.git/info/exclude"
    cat > "$WTC_TMP/no-verb-daemon" <<'FAKEDAEMON'
#!/usr/bin/env bash
cat >/dev/null
echo "error: unrecognized subcommand 'worktree-contains'" >&2
exit 2
FAKEDAEMON
    chmod +x "$WTC_TMP/no-verb-daemon"

    # run_wtc <daemon-bin>: validation block, a merge marker, then the cleanup
    # wrapper -- in a subshell so the eval'd `error` exit cannot kill the suite.
    # The validation block calls the daemon via LOOM_DAEMON_BIN (a fake that lacks
    # the verb, or the real one); the cleanup itself always runs the real binary,
    # so the survival of the path is the validation's decision alone.
    # shellcheck disable=SC2030,SC2034,SC2329  # subshell-local on purpose; the vars serve the eval'd block
    run_wtc() (
        set +e
        export LOOM_DAEMON_BIN="$1"; unset LOOM_PRESERVE_WORKTREE
        REPO_ROOT="$WTC_TMP/repo"; CLEANUP_WORKTREE=true; WORKTREE_PATH_OVERRIDE="$WTC_TMP/wt"
        PR_NUMBER=1; PR_BRANCH="feature/issue-8191"; PR_HEAD_SHA=""
        error() { echo "ERROR: $*"; exit 1; }
        eval "$WTC_VALIDATE"; echo "validated; merge proceeds"
        LOOM_DAEMON_SELF_BIN="$LOOM_DAEMON_SELF_REAL" _post_merge_cleanup
    )
    LOOM_DAEMON_SELF_REAL="$LOOM_DAEMON_SELF_BIN"

    out="$(run_wtc "$WTC_TMP/no-verb-daemon" 2>&1)"
    if [[ "$out" == *"validated; merge proceeds"* && "$out" != *"ERROR:"* ]]; then
        pass "T8a: an older daemon (exit 2) does not block the merge"
    else
        fail "T8a: expected the validation to fall through to the merge; got: $out"
    fi
    if [[ -d "$WTC_TMP/wt" && "$out" != *"Removing worktree"* ]] && \
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
    out="$(run_wtc "$LOOM_DAEMON_SELF_REAL" 2>&1)"
    if [[ "$out" == *"Removing worktree: $WTC_TMP/wt"* && ! -d "$WTC_TMP/wt" ]]; then
        pass "T8d: control -- a verified --worktree-path is still cleaned up"
    else
        fail "T8d: control: expected the verified path to be removed; got: $out"
    fi
    rm -rf "$WTC_TMP"
fi

# --- Test 9: a daemon that cannot plan the cleanup removes NOTHING (#8191).
# This is a fail-DIRECTION test. The retired version of this test pinned an
# `elif [[ -n "$DEFAULT_WT_PATH" ]]` gate: when `cleanup-paths` could not answer
# all three names came back empty, and without the gate the porcelain discovery
# fallback would rediscover this very worktree by branch with $ISSUE_NUM empty
# (#4186's still-open-issue protection skipped) -- a degraded daemon deleting
# what the healthy one preserves. The orchestrator now plans and acts in ONE
# process: there is no half-answered state to fall out of, so the gate and the
# `else`-weakening control (T9b) have nothing left to pin.
echo ""
echo "Test 9: a daemon that cannot plan the cleanup removes nothing"

retired "T9b: removing the 'elif [[ -n \"\$DEFAULT_WT_PATH\" ]]' gate makes the degraded path delete a worktree that the healthy path preserves" \
    "an unanswered cleanup-paths plan never lets discovery run without #4186's issue protection" \
    "#8191 orchestrator slice 1 deleted the shell block that holds the gate; plan and removal are one process, so a plan that cannot be framed ends the pass (and still runs the branch step, which is independent of the worktree) instead of falling into discovery with empty names" \
    "T9a/T9c/T9d below (a daemon lacking the verb removes nothing; the healthy daemon removes with the issue gate applied) and post_merge_cleanup/tests.rs::an_unframeable_worktree_root_removes_nothing_but_still_runs_the_branch_step (the in-process analogue of 'the plan could not be named')"

CP_TMP="$(mktemp -d)"; CP_TMP="$(cd "$CP_TMP" && pwd -P)"
git init -q "$CP_TMP/repo"
git -C "$CP_TMP/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
printf '.loom/\n' >> "$CP_TMP/repo/.git/info/exclude"
CP_WT="$CP_TMP/repo/.loom/worktrees/issue-4242"
git -C "$CP_TMP/repo" worktree add -q -b feature/issue-4242 "$CP_WT" 2>/dev/null
echo unlanded > "$CP_WT/work.txt"; git -C "$CP_WT" add work.txt
git -C "$CP_WT" -c user.email=t@t -c user.name=t commit -q -m "unlanded work"
: > "$CP_WT/.loom-managed"
# shellcheck disable=SC2034
REPO_ROOT="$CP_TMP/repo"; PR_NUMBER=777; PR_BRANCH="feature/issue-4242"; PR_HEAD_SHA=""
WORKTREE_PATH_OVERRIDE=""; CLEANUP_WORKTREE=true
# Issue 4242 is NOT a close target and its state reads OPEN: the healthy daemon
# must PRESERVE (#4186), so a removal by the degraded path is unmistakable.
GATE_CLOSE_TARGETS="7"; GATE_STATE="OPEN"

out="$(LOOM_DAEMON_SELF_BIN="$fake_no_verb" LOOM_PRESERVE_WORKTREE=0 _post_merge_cleanup 2>&1)" || true
if [[ -d "$CP_WT" && "$out" != *"Removing worktree"* ]]; then
    pass "T9a: no cleanup plan => no removal at all; the worktree survives"
else
    fail "T9a: a daemon without the verb must remove nothing; got: $out"
fi
if [[ "$out" == *"WARN:"*"post-merge-cleanup"*"Nothing is removed and no local branch is deleted rather than guessed"* ]]; then
    pass "T9c: the warning says which verb was missing and that nothing was removed"
else
    fail "T9c: expected a warning naming post-merge-cleanup; got: $out"
fi
# Control: the healthy daemon, same fixture, PRESERVES because of the open issue
# (the protection the degraded path must not bypass)...
out="$(LOOM_PRESERVE_WORKTREE=0 _post_merge_cleanup 2>&1)" || true
if [[ -d "$CP_WT" && "$out" == *"Preserving worktree at $CP_WT"* ]]; then
    pass "T9d: control -- the healthy daemon applies the issue gate (preserves an open, non-target issue)"
else
    fail "T9d: control: expected the healthy daemon to preserve; got: $out"
fi
# ...and removes once the issue gate allows it, so the harness can see a removal.
GATE_CLOSE_TARGETS="4242"
out="$(LOOM_PRESERVE_WORKTREE=0 _post_merge_cleanup 2>&1)" || true
if [[ ! -d "$CP_WT" && "$out" == *"Removing worktree: $CP_WT"* ]]; then
    pass "T9e: control -- with the issue gate satisfied the same worktree IS removed"
else
    fail "T9e: control: expected removal; got: $out"
fi
rm -rf "$CP_TMP"

# --- Test 10: a NON-`feature/issue-<N>` branch's default path survives the
# plan (#8191 slice). Originally a regression test for the tab-framed parse: for
# a PR-only branch (`docs/...`, `fix/...`, or a slice branch like
# `feature/issue-8195-slice-13` that the strict anchor rejects) TWO of the plan's
# fields are empty, and bash's `read` collapsed the tabs and lost the leading
# field, silently skipping cleanup for ~18% of merged PRs. The plan is consumed
# in-process now, so the framing bug cannot recur, but the property -- a PR-only
# branch's pr-<PR> worktree IS cleaned up -- is still worth driving end to end.
echo ""
echo "Test 10: a non-feature/issue-<N> branch's pr-<N> worktree is still cleaned up"

for ni_branch in fix/foo-bar feature/issue-8195-slice-13; do
    sim_repo
    NI_WT="$R/.loom/worktrees/pr-777"
    sim_wt "$NI_WT" "$ni_branch" true
    # shellcheck disable=SC2034
    REPO_ROOT="$R"; PR_NUMBER=777; PR_BRANCH="$ni_branch"; PR_HEAD_SHA=""
    PMC_CLOSE_TARGETS=""; PMC_ISSUE_STATE=""
    ni_out="$(LOOM_PRESERVE_WORKTREE=0 _post_merge_cleanup 2>&1)" || true
    if [[ ! -d "$NI_WT" && "$ni_out" == *"Removing worktree: $NI_WT"* ]]; then
        pass "T10a ($ni_branch): the pr-<N> worktree is found and cleaned up"
    else
        fail "T10a ($ni_branch): expected the convention path $NI_WT to be removed; got: $ni_out"
    fi
    if [[ "$ni_out" != *"did not run"* && "$ni_out" != *"No worktree found"* ]]; then
        pass "T10b ($ni_branch): no fail-open warning and no discovery fallback -- the plan named the path"
    else
        fail "T10b ($ni_branch): unexpected degraded-path output; got: $ni_out"
    fi
    if [[ "$(grep -c "Removing worktree" <<<"$ni_out")" == "1" && "$ni_out" != *"Judge/Doctor review worktree"* ]]; then
        pass "T10c ($ni_branch): exactly one removal -- the #6264 judge-pr site stays empty for a PR-only branch"
    else
        fail "T10c ($ni_branch): expected exactly one removal and no judge-pr site; got: $ni_out"
    fi
done

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
