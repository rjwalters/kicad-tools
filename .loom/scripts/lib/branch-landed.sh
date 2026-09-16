#!/usr/bin/env bash
# branch-landed.sh — the ONE "has this branch landed on the default branch?"
# primitive (#7812).
#
# Five separate defects (#5189, #5665, #4918, #4889, #5657) were all the same
# question answered four different ways:
#
#   loom-daemon clean --aggressive : `is_ancestor_of_origin_main` (raw reachability)
#   worktree.sh remove             : `git branch -d`
#   worktree.sh N                  : reuse origin/feature/issue-N if it exists
#   merge-pr.sh                    : tip-matches-merged-PR-head, then `git branch -D`
#
# Every reachability-based answer is WRONG under squash merging (the squash
# commit is a brand-new commit with no parent link to the branch) and equally
# wrong under GitHub's rebase merge, which "always updates the committer
# information and creates new commit SHAs". Each site was patched separately,
# so the repo carried four private squash-awareness implementations that would
# each need re-patching for the next merge strategy. This file replaces them.
#
# Source this file (do not exec). Defines one public function:
#
#   branch_landed <branch> [<default-branch>] [<known-merged-head-sha>]
#
# It PRINTS exactly one of:
#
#   landed      — the default branch already contains everything this branch has
#   not-landed  — the branch carries content the default branch does not have
#   unknown     — could not be determined (forge unreachable AND the local
#                 tree comparison unavailable)
#
# and ALWAYS returns 0 — a non-zero return would abort a `set -e` caller that
# invokes it as a plain statement, and "not landed" is an answer, not an error.
#
# `unknown` MUST fail closed at every call site: never reap, never delete,
# never skip a branch on it. That is the whole point of a three-way answer —
# coercing it to `landed` loses work, and coercing it to `not-landed`
# resurrects the pre-#4889 "can never clean up a squash-merged branch" bug.
#
# # Answer ladder (first definitive answer wins)
#
#   1. ANCESTRY — `git merge-base --is-ancestor <branch> <default>`. Only ever
#      proves `landed` (true under a merge-commit strategy or a fast-forward);
#      its falsity proves nothing, which is exactly the trap the four private
#      heuristics fell into. Cheap, offline, works on every git version.
#   2. CALLER HINT — an already-known merged-PR head SHA (`merge-pr.sh` has
#      just merged the PR and holds `$PR_HEAD_SHA`). Matching tip ⇒ `landed`
#      with zero forge calls; a mismatch skips the forge probe (we already
#      know what the forge would say) and falls through to the tree check.
#   3. FORGE — a MERGED pull request whose head branch is <branch>. Matching
#      head SHA ⇒ `landed`. This is the squash/rebase-proof answer, and the
#      only one available when the local tree comparison cannot run.
#   4. TREE EQUALITY — `git merge-tree --write-tree <default> <branch>` and
#      compare the resulting tree OID with `<default>^{tree}`. Equal trees mean
#      merging the branch would change nothing, i.e. the default branch already
#      contains every change the branch carries — true for a squash, a rebase
#      merge, or a merge commit, and completely independent of SHAs. Requires
#      git >= 2.38; no working tree is mutated, so it is safe against a bare
#      mirror or a linked worktree.
#
# Note the tree comparison is against the default branch's CURRENT tip, not its
# tip at merge time. That degrades gracefully: as the default branch moves
# further ahead, a landed branch stays landed (tree equality really says
# "already contains everything this branch has"), which strengthens over time
# unless something on the default branch reverts the change.
#
# On git < 2.38 step 4 is unavailable. There is deliberately NO rebase-based
# substitute: `git diff --quiet <default>...<branch>` compares against the
# merge base, which is non-empty for exactly the squash/rebase cases that
# matter, so it would confidently report `not-landed` for a landed branch —
# worse than admitting `unknown`. Instead the forge answer stands alone there,
# and a forge-unavailable probe resolves to `unknown` (fail closed).
#
# # Side-channel globals
#
# `branch_landed` also sets these globals for callers that need more than the
# verdict (messages naming the PR, distinguishing "forge said no" from "forge
# was down"). They are only observable when it is called as a PLAIN STATEMENT —
# `$(branch_landed ...)` runs in a subshell and discards them:
#
#   BRANCH_LANDED_VERDICT      — same string as stdout
#   BRANCH_LANDED_EVIDENCE     — which rung answered: ancestor | merged-head-match |
#                                forge-merged-pr | tree-equal | tree-differs |
#                                tree-conflict | forge-no-merged-pr |
#                                merged-head-mismatch | inconclusive
#   BRANCH_LANDED_PR_NUMBER    — merged PR number when the forge answered
#   BRANCH_LANDED_PR_HEAD_SHA  — that PR's head SHA
#   BRANCH_LANDED_FORGE_STATUS — found | not_found | unavailable | hinted | skipped
#
# `unavailable` is the one every caller must special-case: it is "the safety
# check could not even be attempted", not "checked, and it is unmerged".
#
# # Test seams
#
#   LOOM_BRANCH_LANDED_OFFLINE=1        — skip the forge probe entirely.
#   LOOM_BRANCH_LANDED_GIT_VERSION=x.y  — override the detected git version
#                                         (simulates a pre-2.38 host).
#   BRANCH_LANDED_REPO_DIR=<path>       — repo to run git in; defaults to
#                                         $REPO_ROOT, else the cwd's repo.
#
# The forge probe itself lives in `_branch_landed_forge_probe`, a separate
# function so a test can redefine it after sourcing (no network, no `gh`).

# The BRANCH_LANDED_* globals are this library's documented side channel (see
# the header): written here, read by callers in other files that the linter
# cannot see from this one.
# shellcheck disable=SC2034

# Idempotent: worktree.sh, merge-pr.sh and cleanup-branches.sh each source this
# directly, so one process can easily source it twice.
if [[ -n "${_LOOM_BRANCH_LANDED_LOADED:-}" ]]; then
    return 0 2>/dev/null || true
fi
_LOOM_BRANCH_LANDED_LOADED=1

# Repo directory every git call below runs against.
_branch_landed_repo_dir() {
    if [[ -n "${BRANCH_LANDED_REPO_DIR:-}" ]]; then
        printf '%s\n' "$BRANCH_LANDED_REPO_DIR"
    elif [[ -n "${REPO_ROOT:-}" ]]; then
        printf '%s\n' "$REPO_ROOT"
    else
        printf '%s\n' "."
    fi
}

# Resolve a rev to a commit SHA, or print nothing.
_branch_landed_commit() {
    local dir="$1" rev="$2"
    [[ -n "$rev" ]] || return 0
    git -C "$dir" rev-parse --verify -q "${rev}^{commit}" 2>/dev/null || true
}

# Resolve the branch argument to a commit: accept a local branch, a remote
# branch, a bare ref name, or any rev. Prints nothing when unresolvable.
_branch_landed_resolve_branch() {
    local dir="$1" branch="$2" sha
    sha="$(_branch_landed_commit "$dir" "refs/heads/$branch")"
    [[ -n "$sha" ]] || sha="$(_branch_landed_commit "$dir" "$branch")"
    [[ -n "$sha" ]] || sha="$(_branch_landed_commit "$dir" "refs/remotes/origin/$branch")"
    printf '%s\n' "$sha"
}

# Resolve the default branch argument (may be empty) to a commit. Prefers the
# remote-tracking ref — it is what the branch actually has to land ON — then
# the local branch, then origin/HEAD's target.
_branch_landed_resolve_default() {
    local dir="$1" name="$2" sha=""
    if [[ -n "$name" ]]; then
        sha="$(_branch_landed_commit "$dir" "refs/remotes/origin/${name#origin/}")"
        [[ -n "$sha" ]] || sha="$(_branch_landed_commit "$dir" "refs/heads/${name#origin/}")"
        [[ -n "$sha" ]] || sha="$(_branch_landed_commit "$dir" "$name")"
    fi
    if [[ -z "$sha" ]] && declare -F loom_default_branch >/dev/null 2>&1; then
        local detected
        detected="$(cd "$dir" 2>/dev/null && loom_default_branch 2>/dev/null || true)"
        if [[ -n "$detected" ]]; then
            sha="$(_branch_landed_commit "$dir" "refs/remotes/origin/$detected")"
            [[ -n "$sha" ]] || sha="$(_branch_landed_commit "$dir" "refs/heads/$detected")"
        fi
    fi
    if [[ -z "$sha" ]]; then
        local fallback
        for fallback in refs/remotes/origin/main refs/heads/main refs/remotes/origin/master refs/heads/master; do
            sha="$(_branch_landed_commit "$dir" "$fallback")"
            [[ -n "$sha" ]] && break
        done
    fi
    printf '%s\n' "$sha"
}

# True when this git supports `git merge-tree --write-tree` (git >= 2.38).
_branch_landed_supports_merge_tree() {
    local version="${LOOM_BRANCH_LANDED_GIT_VERSION:-}"
    if [[ -z "$version" ]]; then
        version="$(git --version 2>/dev/null | awk '{print $3}')"
    fi
    [[ -n "$version" ]] || return 1
    local major="${version%%.*}" rest="${version#*.}" minor
    minor="${rest%%.*}"
    [[ "$major" =~ ^[0-9]+$ ]] || return 1
    [[ "$minor" =~ ^[0-9]+$ ]] || minor=0
    if (( major > 2 )); then return 0; fi
    (( major == 2 && minor >= 38 ))
}

# Look up the head SHA + number of a MERGED pull request whose head branch is
# <branch>, via the forge (`loom-daemon forge` when present for Gitea
# passthrough, else `gh` — the same $FORGE convention cleanup-branches.sh uses).
#
# Sets (never prints): _BRANCH_LANDED_PROBE_STATUS (found|not_found|unavailable),
# _BRANCH_LANDED_PROBE_SHA, _BRANCH_LANDED_PROBE_NUMBER. Always returns 0.
# Redefine this function after sourcing to stub the forge in a test.
_branch_landed_forge_probe() {
    local branch="${1#origin/}"
    _BRANCH_LANDED_PROBE_STATUS="unavailable"
    _BRANCH_LANDED_PROBE_SHA=""
    _BRANCH_LANDED_PROBE_NUMBER=""
    [[ -n "$branch" ]] || return 0
    [[ "${LOOM_BRANCH_LANDED_OFFLINE:-}" != "1" ]] || return 0
    command -v jq >/dev/null 2>&1 || return 0
    local forge_cmd
    if command -v loom-daemon >/dev/null 2>&1; then
        forge_cmd="loom-daemon forge"
    elif command -v gh >/dev/null 2>&1; then
        forge_cmd="gh"
    else
        return 0
    fi
    local pr_json
    pr_json="$($forge_cmd pr list --head "$branch" --state merged --json headRefOid,number --limit 1 2>/dev/null)" || return 0
    local sha number
    sha="$(printf '%s' "$pr_json" | jq -r '.[0].headRefOid // empty' 2>/dev/null || true)"
    number="$(printf '%s' "$pr_json" | jq -r '.[0].number // empty' 2>/dev/null || true)"
    if [[ -n "$sha" ]]; then
        _BRANCH_LANDED_PROBE_STATUS="found"
        _BRANCH_LANDED_PROBE_SHA="$sha"
        _BRANCH_LANDED_PROBE_NUMBER="$number"
    else
        _BRANCH_LANDED_PROBE_STATUS="not_found"
    fi
    return 0
}

# branch_landed <branch> [<default-branch>] [<known-merged-head-sha>]
# See the header for the full contract. Prints landed|not-landed|unknown,
# sets the BRANCH_LANDED_* globals, always returns 0.
branch_landed() {
    local branch="${1:-}" default_name="${2:-}" hint_sha="${3:-}"
    BRANCH_LANDED_VERDICT="unknown"
    BRANCH_LANDED_EVIDENCE="inconclusive"
    BRANCH_LANDED_PR_NUMBER=""
    BRANCH_LANDED_PR_HEAD_SHA=""
    BRANCH_LANDED_FORGE_STATUS="skipped"

    if [[ -z "$branch" ]]; then
        printf '%s\n' "$BRANCH_LANDED_VERDICT"
        return 0
    fi

    local dir tip default_sha
    dir="$(_branch_landed_repo_dir)"
    tip="$(_branch_landed_resolve_branch "$dir" "$branch")"
    default_sha="$(_branch_landed_resolve_default "$dir" "$default_name")"

    # Rung 1: ancestry. Proves `landed` only; never proves the negative.
    if [[ -n "$tip" && -n "$default_sha" ]] && \
       git -C "$dir" merge-base --is-ancestor "$tip" "$default_sha" 2>/dev/null; then
        BRANCH_LANDED_VERDICT="landed"
        BRANCH_LANDED_EVIDENCE="ancestor"
        printf '%s\n' "$BRANCH_LANDED_VERDICT"
        return 0
    fi

    # Rung 2: the caller's already-known merged-PR head SHA.
    local forge_answered_negative=false
    if [[ -n "$hint_sha" ]]; then
        BRANCH_LANDED_FORGE_STATUS="hinted"
        BRANCH_LANDED_PR_HEAD_SHA="$hint_sha"
        if [[ -n "$tip" && "$tip" == "$hint_sha" ]]; then
            BRANCH_LANDED_VERDICT="landed"
            BRANCH_LANDED_EVIDENCE="merged-head-match"
            printf '%s\n' "$BRANCH_LANDED_VERDICT"
            return 0
        fi
        # The caller already knows the merged head; the branch tip is not it.
        # No forge round-trip can add anything — go straight to the tree check.
        BRANCH_LANDED_EVIDENCE="merged-head-mismatch"
        forge_answered_negative=true
    else
        # Rung 3: the forge. Pre-seeded so a stubbed probe that forgets to set
        # one of these cannot trip `set -u` in the caller.
        _BRANCH_LANDED_PROBE_STATUS="unavailable"
        _BRANCH_LANDED_PROBE_SHA=""
        _BRANCH_LANDED_PROBE_NUMBER=""
        _branch_landed_forge_probe "$branch"
        BRANCH_LANDED_FORGE_STATUS="$_BRANCH_LANDED_PROBE_STATUS"
        case "$_BRANCH_LANDED_PROBE_STATUS" in
            found)
                BRANCH_LANDED_PR_HEAD_SHA="$_BRANCH_LANDED_PROBE_SHA"
                BRANCH_LANDED_PR_NUMBER="$_BRANCH_LANDED_PROBE_NUMBER"
                if [[ -z "$tip" || "$tip" == "$_BRANCH_LANDED_PROBE_SHA" ]]; then
                    BRANCH_LANDED_VERDICT="landed"
                    BRANCH_LANDED_EVIDENCE="forge-merged-pr"
                    printf '%s\n' "$BRANCH_LANDED_VERDICT"
                    return 0
                fi
                # A merged PR exists for this branch NAME but the local tip has
                # moved past it (unpushed work, or the next partial-increment
                # slice reusing the name) — the tree check decides.
                BRANCH_LANDED_EVIDENCE="merged-head-mismatch"
                forge_answered_negative=true
                ;;
            not_found)
                BRANCH_LANDED_EVIDENCE="forge-no-merged-pr"
                forge_answered_negative=true
                ;;
            *) : ;;  # unavailable — only the tree check can answer now
        esac
    fi

    # Rung 4: tree equality. Squash/rebase/merge-commit proof, fully offline.
    if [[ -n "$tip" && -n "$default_sha" ]] && _branch_landed_supports_merge_tree; then
        local merged_tree rc=0
        merged_tree="$(git -C "$dir" merge-tree --write-tree "$default_sha" "$tip" 2>/dev/null)" || rc=$?
        if [[ "$rc" -eq 0 && -n "$merged_tree" ]]; then
            local default_tree
            default_tree="$(git -C "$dir" rev-parse --verify -q "${default_sha}^{tree}" 2>/dev/null || true)"
            # merge-tree prints the OID on the first line (plus a conflict
            # report on later lines when rc != 0, already excluded here).
            merged_tree="${merged_tree%%$'\n'*}"
            if [[ -n "$default_tree" && "$merged_tree" == "$default_tree" ]]; then
                BRANCH_LANDED_VERDICT="landed"
                BRANCH_LANDED_EVIDENCE="tree-equal"
            else
                BRANCH_LANDED_VERDICT="not-landed"
                BRANCH_LANDED_EVIDENCE="tree-differs"
            fi
            printf '%s\n' "$BRANCH_LANDED_VERDICT"
            return 0
        fi
        if [[ "$rc" -eq 1 ]]; then
            # Exit 1 is "merge conflicts" — the branch carries content that
            # collides with the default branch, so it definitively has not
            # landed. Any other non-zero exit is a real failure (bad args,
            # unreadable object) and must NOT be read as an answer.
            BRANCH_LANDED_VERDICT="not-landed"
            BRANCH_LANDED_EVIDENCE="tree-conflict"
            printf '%s\n' "$BRANCH_LANDED_VERDICT"
            return 0
        fi
    fi

    # The tree check could not run (git < 2.38, unresolvable refs, or a
    # merge-tree failure). A definitive forge/hint negative still stands;
    # otherwise nothing answered and the verdict stays `unknown`.
    if [[ "$forge_answered_negative" == true ]]; then
        BRANCH_LANDED_VERDICT="not-landed"
    fi
    printf '%s\n' "$BRANCH_LANDED_VERDICT"
    return 0
}

# branch_has_landed <branch> [<default-branch>] [<known-merged-head-sha>]
#
# Boolean convenience wrapper: rc 0 iff the verdict is `landed`. Calls
# `branch_landed` as a plain statement (never `$(...)`, which would run in a
# subshell and discard the BRANCH_LANDED_* globals the caller may want for its
# message), so `not-landed` AND the fail-closed `unknown` are both false here.
branch_has_landed() {
    branch_landed "$@" >/dev/null
    [[ "$BRANCH_LANDED_VERDICT" == "landed" ]]
}
