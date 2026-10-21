#!/usr/bin/env bash
# post-merge-cleanup-harness.sh — drive merge-pr.sh's post-merge cleanup the way
# the script does, for the retained test-merge-pr-* suites (#8191, orchestrator
# slice 1).
#
# Source this file (do not exec) AFTER `loom_test_require_daemon_bin` has pinned
# a loom-daemon built from this tree. Defines:
#
#   pmc_load <merge-pr.sh>
#       Stubs `info`/`warning`/`success` (so output reads `INFO: …`, `WARN: …`,
#       `OK: …` as the retired per-function harnesses did), defines forge/landed
#       stubs driven by $PMC_CLOSE_TARGETS / $PMC_ISSUE_STATE, and extracts the
#       REAL `_post_merge_cleanup` wrapper from merge-pr.sh (no copy to drift).
#       Everything it calls — cleanup-paths, the preserve decision, the guards,
#       the removal, the branch delete — is the REAL `merge-pr
#       post-merge-cleanup` verb against real git repos.
#
#   pmc_run_issue <N> [<pr>]
#       Cleans up after a merge of PR <pr> (default 1) from branch
#       feature/issue-<N> in $REPO_ROOT, treating <N> as a close target of the
#       PR (the common `Closes #N` case), so the worktree at
#       $REPO_ROOT/.loom/worktrees/issue-<N> is eligible for removal.
#
# Why this replaced the old per-function `extract_fn` harnesses: the suites used
# to eval `_remove_loom_worktree`, `_maybe_delete_local_branch` and friends out
# of merge-pr.sh and call them one at a time. Those functions are Rust now, and
# calling the verb through the same wrapper merge-pr.sh uses is a STRONGER test
# of the same properties: it exercises the sequencing as well as each step.

# The globals assigned below are read by the extracted _post_merge_cleanup,
# which the linter cannot see.
# shellcheck disable=SC2034

pmc_extract_fn() { # <name> <file>
    awk -v fn="$1" '
      $0 ~ "^"fn"\\(\\) \\{" { grab=1 }
      grab { print }
      grab && /^}/ { exit }
    ' "$2"
}

pmc_load() { # <merge-pr.sh>
    local src="$1"
    info()    { echo "INFO: $*"; }
    warning() { echo "WARN: $*"; }
    success() { echo "OK: $*"; }
    error()   { echo "ERROR: $*" >&2; return 1; }
    # Offline: these suites exercise merge-pr.sh's local logic, not the forge
    # rung of the shared landed primitive, and must stay hermetic.
    export LOOM_BRANCH_LANDED_OFFLINE=1
    # shellcheck source=../../lib/branch-landed.sh
    source "$(dirname "$src")/lib/branch-landed.sh"
    forge_pr_close_targets() { printf '%s' "${PMC_CLOSE_TARGETS:-}"; }
    forge_get_issue_state()  { printf '%s' "${PMC_ISSUE_STATE:-}"; }
    eval "$(pmc_extract_fn _post_merge_cleanup "$src")"
    GH=gh; REPO_NWO="o/r"
    CLEANUP_WORKTREE=true; CLEANUP_PRIMARY_CHECKOUT=true; WORKTREE_PATH_OVERRIDE=""
    DEFAULT_BRANCH_NAME="main"; PR_HEAD_SHA=""
}

pmc_run_issue() { # <N> [<pr>]
    PR_NUMBER="${2:-1}"
    PR_BRANCH="feature/issue-$1"
    PMC_CLOSE_TARGETS="$1" _post_merge_cleanup
}

# pmc_delete_branch <branch> [<expected-head-sha>]
#   The merged PR's branch is <branch> and has NO convention worktree: runs the
#   whole cleanup, so the discovery fallback and the local-branch delete both
#   run, exactly as for a PR whose worktree is already gone. Used by the suites
#   that used to call `_maybe_delete_local_branch` directly.
pmc_delete_branch() {
    PR_BRANCH="$1" PR_HEAD_SHA="${2:-}" PR_NUMBER="${PR_NUMBER:-1}" _post_merge_cleanup
}

# pmc_remove_worktree_override <path>
#   `--worktree-path <path>`: the explicit-opt-in arm (allow_unmanaged), which
#   also tidies the worktree's attached local branch.
pmc_remove_worktree_override() {
    PR_BRANCH="${PR_BRANCH:-feature/override}" PR_NUMBER="${PR_NUMBER:-1}" WORKTREE_PATH_OVERRIDE="$1" _post_merge_cleanup
}
