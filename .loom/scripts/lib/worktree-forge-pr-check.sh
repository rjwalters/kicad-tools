#!/usr/bin/env bash
# lib/worktree-forge-pr-check.sh
#
# #7765: forge-aware guard against creating a FRESH branch that silently
# shadows an already-open pull request.
#
# `worktree.sh N` resolves the issue branch against `origin` only: fetch
# `origin/<branch>`, and if no such ref exists, create a brand-new branch of
# that name at the base ref's HEAD and report success. That fall-through is
# blind to two real cases:
#
#   - a cross-repository (fork) PR, whose head branch never appears as
#     `origin/<branch>` at all — pushing to `origin/<branch>` afterwards does
#     not touch the fork's PR, so the two histories diverge silently;
#   - a same-repo PR whose head simply was not fetched by the plain-name
#     fetch (the #4823 in-flight-cycle case, reachable even when that fetch
#     fails or misses).
#
# It is also blind to the difference between "origin confirmed it has no such
# ref" and "the fetch itself failed" — only the former is evidence.
#
# This file holds the fix and the branch-resolution decision it gates:
#
#   _worktree_open_pr_for_branch                    forge lookup (open PR
#                                                   head-matching a branch)
#   _worktree_guard_fresh_branch_against_open_pr    the #7765 decision arm
#   _worktree_resolve_origin_branch_reuse           worktree.sh's whole
#                                                   "reuse origin/<branch> or
#                                                   branch fresh?" decision
#                                                   (#4823 / #5657 / #7765)
#
# They live here rather than inline in worktree.sh per
# `.loom/docs/file-size-policy.md` — worktree.sh is over the 1000-line ratchet
# threshold and therefore frozen at its current size, and "new sibling module,
# small dispatch arm left behind" is that policy's own preferred remedy.
# worktree.sh calls `_worktree_resolve_origin_branch_reuse` and reads back
# `_WT_REUSE_REMOTE_BRANCH`; the other two are internal to this file.
#
# Both functions depend on worktree.sh's `print_error` / `print_info`, and on
# fd 3 being open for `--json` output; fallbacks are defined below so the file
# can also be sourced standalone.

if ! declare -F print_error >/dev/null 2>&1; then
    print_error() { echo "ERROR: $1" >&2; }
fi
if ! declare -F print_info >/dev/null 2>&1; then
    print_info() { echo "INFO: $1"; }
fi

# Does this repository have ANY git remote that could plausibly be a forge?
#
# Mirrors `gh`'s own rule ("none of the git remotes configured for this
# repository point to a known GitHub host") but decided LOCALLY from the
# remote URLs, with zero forge calls — because `gh` only reports that message
# once it is authenticated. An UNAUTHENTICATED `gh` bails out first, with a
# completely different message and exit code:
#
#   $ gh pr list ...        # origin = /tmp/xxx/origin.git, no credential
#   To get started with GitHub CLI, please run:  gh auth login
#   Alternatively, populate the GH_TOKEN environment variable ...
#   exit 4
#
# That text does not match the "no known GitHub host" signal, so before this
# helper existed the throwaway-local-clone case was misclassified as
# `unavailable` and `worktree.sh` REFUSED to create a worktree at all — which
# broke every hermetic shell suite that builds a synthetic repo with
# `git remote add origin "$TMP/origin.git"` on an unauthenticated runner
# (#7863), and would equally break any real offline/sandboxed clone.
#
# A URL with a host component (scheme://… or scp-like user@host:path) is
# treated as "possibly a forge" — deliberately permissive, since the genuine
# `unavailable` refusal (auth/rate-limit/network failure against a REAL forge
# remote) must stay a refusal. Only URLs that are unambiguously local
# filesystem paths (absolute, relative, ~-relative, or file://) count as
# evidence of no forge relationship. A repo with no remotes at all likewise
# has nothing to shadow.
#
# Returns 0 when at least one remote could be a forge, 1 otherwise.
_worktree_repo_has_forge_remote() {
    local remote url
    while read -r remote; do
        [[ -n "$remote" ]] || continue
        url="$(git remote get-url "$remote" 2>/dev/null || true)"
        case "$url" in
            ""|file://*|/*|./*|../*|"~"/*) continue ;;
            *://*) return 0 ;;
            *@*:*) return 0 ;;
        esac
    done < <(git remote 2>/dev/null)
    return 1
}

# Look up an OPEN pull request whose head branch matches <branch>, via the
# forge (same `loom-daemon forge` / `gh` convention as `branch_landed` in
# lib/branch-landed.sh, the #5657/#7812 sibling check). Unlike that
# helper, this one is used to decide whether it is SAFE to fall through to
# creating a brand-new branch named <branch> off the base ref — so, per
# #7765, "could not determine" must NOT be treated the same as "confirmed
# none exists": the whole defect this closes is a silent fresh main-HEAD
# branch shadowing a real PR (same-repo, unfetched — #4823 — or a fork's
# cross-repo head), so the caller must be able to tell "checked, no PR" apart
# from "could not check". MUST be called as a plain statement, never inside
# `$(...)` (a subshell would discard the globals):
#   _WT_OPEN_PR_STATUS        - one of:
#     found          - an open PR head-matches <branch>; the rest of the
#                       globals below are populated from it
#     not_found      - the forge was reachable and confirmed no open PR has
#                       this head branch — safe to create a fresh branch
#     no_forge_remote - no git remote could be a forge at all (every remote
#                       URL is a local filesystem path, or there are no
#                       remotes — a throwaway/offline local clone; decided
#                       locally by _worktree_repo_has_forge_remote, and also
#                       via gh's own "no known GitHub host" error), OR `jq`
#                       itself is missing (a tooling gap, not a
#                       forge-reachability signal) — either way there is no PR
#                       to shadow, so this is treated the same as not_found by
#                       callers, kept distinct here only for testability
#     unavailable    - gh/loom-daemon missing, not authenticated,
#                       rate-limited, or the query otherwise failed —
#                       genuinely unknown, distinct from not_found so callers
#                       can refuse instead of guessing "safe"
#   _WT_OPEN_PR_NUMBER        - PR number (found only)
#   _WT_OPEN_PR_IS_CROSS_REPO - "true"/"false" (found only)
#   _WT_OPEN_PR_HEAD_REPO     - "<owner>/<repo>" the PR's head branch lives on
#                               (found only; differs from the base repo iff
#                               IS_CROSS_REPO is "true" — a fork PR)
#   _WT_OPEN_PR_HEAD_REF      - head ref name on that repo (found only)
#   _WT_OPEN_PR_URL           - PR URL, for messaging (found only)
# Never fails the caller — always returns 0.
_worktree_open_pr_for_branch() {
    local branch="$1"
    _WT_OPEN_PR_STATUS="unavailable"
    _WT_OPEN_PR_NUMBER=""
    _WT_OPEN_PR_IS_CROSS_REPO=""
    _WT_OPEN_PR_HEAD_REPO=""
    _WT_OPEN_PR_HEAD_REF=""
    _WT_OPEN_PR_URL=""
    if [[ -z "$branch" ]]; then
        return 0
    fi
    # A missing `jq` is a basic tooling gap, not a forge-reachability signal
    # (`branch_landed` already treats it that way for the #5657 check, and
    # other worktree.sh features are exercised with jq deliberately absent
    # expecting plain worktree creation to keep working) - degrade the same
    # way as "no forge remote" rather than refusing.
    if ! command -v jq >/dev/null 2>&1; then
        _WT_OPEN_PR_STATUS="no_forge_remote"
        return 0
    fi
    # No remote that could be a forge => there is no PR to shadow, and no
    # query worth making. Decided from the remote URLs BEFORE spending a forge
    # round-trip, because an unauthenticated `gh` never gets far enough to tell
    # us this itself (see _worktree_repo_has_forge_remote above, #7863).
    if ! _worktree_repo_has_forge_remote; then
        _WT_OPEN_PR_STATUS="no_forge_remote"
        return 0
    fi
    local forge_cmd
    if command -v loom-daemon >/dev/null 2>&1; then
        forge_cmd="loom-daemon forge"
    elif command -v gh >/dev/null 2>&1; then
        forge_cmd="gh"
    else
        return 0
    fi
    # stdout and stderr are captured SEPARATELY (a temp file for stderr, not
    # 2>&1): on success gh/loom-daemon can still write incidental warnings to
    # stderr (auth notices, update nags, ...) alongside the clean JSON array
    # on stdout - merging them would corrupt the jq parse below on an
    # otherwise-successful call. Failure text (used to tell "this repo has no
    # forge relationship at all" - a throwaway/offline clone, safe to skip -
    # from a genuine auth/rate-limit/network failure, via gh's own stable
    # error text) only needs to be inspected when the command itself failed.
    # `if VAR=$(...); then` (not a bare assignment) so a non-zero exit
    # doesn't trip `set -e` before we can inspect it.
    local pr_output rc pr_stderr_file
    pr_stderr_file="$(mktemp 2>/dev/null || echo /tmp/loom-wt-open-pr-stderr.$$)"
    if pr_output="$($forge_cmd pr list --state open --head "$branch" \
        --json number,isCrossRepository,headRepository,headRefName,url --limit 5 2>"$pr_stderr_file")"; then
        rc=0
    else
        rc=$?
    fi
    if [[ $rc -ne 0 ]]; then
        if grep -qi "none of the git remotes configured for this repository point to a known GitHub host" "$pr_stderr_file" 2>/dev/null; then
            _WT_OPEN_PR_STATUS="no_forge_remote"
        elif grep -qi "is not handled natively" "$pr_stderr_file" 2>/dev/null; then
            # `loom-daemon forge` DECLINES (EX_FORGE_DECLINED, exit 3) forges it
            # does not handle natively -- Gitea today. This check is a GitHub
            # `gh pr list --json isCrossRepository,headRepository` query and has
            # no Gitea shape at all, so a decline means "nothing this check can
            # consult", not "the forge is down". Filing it as `unavailable`
            # refused EVERY fresh-branch worktree on a Gitea repo; there is no
            # PR shadow this check can detect there, so proceed as if the repo
            # had no forge remote.
            _WT_OPEN_PR_STATUS="no_forge_remote"
        fi
        rm -f "$pr_stderr_file"
        return 0
    fi
    rm -f "$pr_stderr_file"
    local count
    count="$(echo "$pr_output" | jq 'length' 2>/dev/null || true)"
    if [[ -z "$count" || ! "$count" =~ ^[0-9]+$ ]]; then
        # Unparseable output — treat like any other query failure.
        return 0
    fi
    if [[ "$count" -eq 0 ]]; then
        _WT_OPEN_PR_STATUS="not_found"
        return 0
    fi
    _WT_OPEN_PR_STATUS="found"
    _WT_OPEN_PR_NUMBER="$(echo "$pr_output" | jq -r '.[0].number // empty' 2>/dev/null)"
    _WT_OPEN_PR_IS_CROSS_REPO="$(echo "$pr_output" | jq -r '.[0].isCrossRepository // false' 2>/dev/null)"
    _WT_OPEN_PR_HEAD_REPO="$(echo "$pr_output" | jq -r '.[0].headRepository.nameWithOwner // empty' 2>/dev/null)"
    _WT_OPEN_PR_HEAD_REF="$(echo "$pr_output" | jq -r '.[0].headRefName // empty' 2>/dev/null)"
    _WT_OPEN_PR_URL="$(echo "$pr_output" | jq -r '.[0].url // empty' 2>/dev/null)"
    return 0
}

# _worktree_guard_fresh_branch_against_open_pr <branch> <issue-number>
#         <json-output> <base-display> <base-ref> <origin-fetch-result>
#
# Run by worktree.sh at the exact point where `refs/remotes/origin/<branch>`
# does NOT exist and it is about to create a fresh branch of that name off the
# base ref. Decides whether that is actually safe:
#
#   - cross-repo (fork) PR owns the name -> refuse (exit 1), naming the real
#     head and how to reach it
#   - same-repo PR owns the name but its ref is not local yet -> fetch
#     refs/pull/<n>/head and reuse it (sets _WT_REUSE_REMOTE_BRANCH=true);
#     refuse if even that fetch cannot materialize the ref
#   - forge confirmed no PR, or origin has no forge relationship at all ->
#     return 0, letting the caller create the fresh branch exactly as before
#   - forge could not be asked at all -> refuse rather than guess "safe"
#
# <origin-fetch-result> is the caller's own classification of the preceding
# `git fetch origin <branch>` ("ok" / "no-such-ref" / "fetch-failed"), used
# only for messaging. Sets the caller's `_WT_REUSE_REMOTE_BRANCH` global
# (deliberately not declared local here); exits the whole script on refusal,
# which is the same control flow this code had when it was inline.
_worktree_guard_fresh_branch_against_open_pr() {
    local branch="$1"
    local issue_number="$2"
    local json_output="$3"
    local base_display="$4"
    local base_ref="$5"
    local origin_fetch_result="$6"

    # origin has no branch of this name (confirmed absent, or the caller's
    # fetch simply failed — $origin_fetch_result). Either way we are about to
    # fall through to creating a FRESH branch under this exact name, which is
    # precisely the silent-divergence trap #7765 describes: a fork PR's head
    # (e.g. turian/loom:feature/issue-7717) never shows up as
    # origin/feature/issue-7717, so ref-absence on origin is NOT proof that no
    # PR already claims this branch name. Ask the forge directly before
    # deciding.
    _worktree_open_pr_for_branch "$branch"
    case "$_WT_OPEN_PR_STATUS" in
        found)
            if [[ "$_WT_OPEN_PR_IS_CROSS_REPO" == "true" ]]; then
                # A fork PR already owns this branch name. Creating a
                # same-named branch from $base_display here would silently
                # shadow it (same name, unrelated history, and pushing to
                # origin/$branch would NOT touch the fork's PR) - so refuse
                # rather than proceed blind, naming the real head and how to
                # reach it (per the AC: refuse OR fetch; refuse is the
                # simpler, unambiguous choice here).
                if [[ "$json_output" == "true" ]]; then
                    echo '{"success": false, "error": "shadowed-cross-repo-pr", "issueNumber": '"$issue_number"', "prNumber": '"${_WT_OPEN_PR_NUMBER:-null}"', "headRepo": "'"$_WT_OPEN_PR_HEAD_REPO"'", "headRef": "'"$_WT_OPEN_PR_HEAD_REF"'"}' >&3
                else
                    print_error "Open PR #${_WT_OPEN_PR_NUMBER} for '$branch' already exists with its head on a FORK ($_WT_OPEN_PR_HEAD_REPO:$_WT_OPEN_PR_HEAD_REF) - refusing to create a same-named branch from $base_display, which would silently shadow it instead of the real work."
                    echo "  PR: ${_WT_OPEN_PR_URL:-<no url>}"
                    echo "  To work on it, either fetch the PR's merge ref directly:"
                    echo "    git fetch origin refs/pull/${_WT_OPEN_PR_NUMBER}/head:$branch"
                    echo "  or add the fork as a remote and push back there (not origin):"
                    echo "    git remote add pr-${_WT_OPEN_PR_NUMBER}-fork https://github.com/${_WT_OPEN_PR_HEAD_REPO}.git"
                    echo "    git fetch pr-${_WT_OPEN_PR_NUMBER}-fork $_WT_OPEN_PR_HEAD_REF"
                fi
                exit 1
            fi
            # Same-repo PR whose head simply was not fetched yet by the
            # plain-name fetch (it failed, or gh's --head match found it under
            # a ref this repo had not seen) — this is exactly the #4823
            # in-flight-cycle case, just reachable even when that fetch did
            # not resolve it. Fetch it explicitly (by PR number, which always
            # resolves regardless of why the plain-name fetch missed) and
            # reuse it — never fall through to a fresh branch here.
            if [[ "$json_output" != "true" ]]; then
                print_info "Open PR #${_WT_OPEN_PR_NUMBER} already exists for '$branch' on origin (not yet fetched) - fetching and reusing it"
            fi
            git fetch origin "refs/pull/${_WT_OPEN_PR_NUMBER}/head:refs/remotes/origin/$branch" 2>/dev/null || true
            if git show-ref --verify --quiet "refs/remotes/origin/$branch"; then
                _WT_REUSE_REMOTE_BRANCH=true
                return 0
            fi
            # The forge confirmed the PR exists but we still could not
            # materialize its ref locally - refuse rather than silently
            # branching fresh under its name.
            if [[ "$json_output" == "true" ]]; then
                echo '{"success": false, "error": "open-pr-ref-fetch-failed", "issueNumber": '"$issue_number"', "prNumber": '"${_WT_OPEN_PR_NUMBER:-null}"'}' >&3
            else
                print_error "Open PR #${_WT_OPEN_PR_NUMBER} exists for '$branch' but its ref could not be fetched from origin - refusing to create a same-named branch from $base_display."
            fi
            exit 1
            ;;
        not_found|no_forge_remote)
            # not_found: the forge confirmed no open PR (same-repo or
            # cross-repo) claims this branch name. no_forge_remote: origin
            # isn't a recognized forge remote at all (offline/throwaway
            # clone) - there is no PR to shadow either way. Both are safe to
            # fall through to a fresh branch.
            return 0
            ;;
        unavailable)
            # Could not ask the forge at all (gh/loom-daemon missing,
            # unauthenticated, rate-limited, network down, ...), and the
            # caller's origin fetch ($origin_fetch_result) gives no
            # independent confirmation either. Silently proceeding here IS the
            # #7765 defect - refuse rather than guess "safe".
            if [[ "$json_output" == "true" ]]; then
                echo '{"success": false, "error": "forge-check-unavailable", "issueNumber": '"$issue_number"', "originFetch": "'"$origin_fetch_result"'"}' >&3
            else
                print_error "Could not verify via the forge whether an open PR already exists for '$branch' (gh unavailable, unauthenticated, or rate-limited) - refusing to create a same-named branch from $base_display blind."
                if [[ "$origin_fetch_result" == "fetch-failed" ]]; then
                    echo "  (the earlier 'git fetch origin $branch' also failed - see above)"
                fi
                echo "  Once gh access is restored, re-run: ./.loom/scripts/worktree.sh $issue_number"
                echo "  If you are certain no PR exists for this issue, create the local branch"
                echo "  manually first (git branch $branch $base_ref) - worktree.sh will then reuse it."
            fi
            exit 1
            ;;
    esac
    return 0
}

# _worktree_resolve_origin_branch_reuse <branch> <issue-number> <json-output>
#                          <base-display> <base-ref> <default-branch>
#
# Decide, for a branch that has no LOCAL ref yet, whether worktree.sh should
# reuse origin's branch of the same name or create a fresh one off the base
# ref. Moved out of worktree.sh together with the #7765 check it now gates on
# (file-size ratchet — see the header of this file); the three arms are:
#
#   - origin has the ref and it has NOT landed on the default branch -> reuse
#     it (the #4823 in-flight-cycle case: a Doctor fixing review feedback must
#     continue the real PR history, not a fresh branch off main)
#   - origin has the ref but it has already LANDED -> do not reuse; fall
#     through to a fresh branch (#5657, the reused partial-slice branch name)
#   - origin has no such ref -> hand off to
#     `_worktree_guard_fresh_branch_against_open_pr` above, which asks the
#     forge before allowing the fresh branch (#7765)
#
# The origin fetch's own result is classified rather than swallowed: "no such
# ref on origin" and "the fetch itself failed" (network/auth/rate-limit) look
# identical to a bare `|| true`, but only the former is proof that origin has
# no branch of this name, and the #7765 arm needs to tell them apart.
# `if VAR=$(...); then` (not a bare assignment) so a non-zero exit does not
# trip `set -e` before we inspect it.
#
# Sets the caller's `_WT_REUSE_REMOTE_BRANCH` global (deliberately not local);
# depends on the shared `branch_landed` primitive (lib/branch-landed.sh,
# #7812/#7869) for the landed arm — worktree.sh sources it, with a fail-open
# shim when it is missing. Always returns 0 — refusals exit the script
# outright, same control flow as when this ran inline.
_worktree_resolve_origin_branch_reuse() {
    local branch="$1"
    local issue_number="$2"
    local json_output="$3"
    local base_display="$4"
    local base_ref="$5"
    local default_branch="$6"
    local origin_fetch_result origin_fetch_output
    origin_fetch_result="ok"
    if ! origin_fetch_output="$(git fetch origin "$branch" 2>&1)"; then
        if echo "$origin_fetch_output" | grep -qi "couldn't find remote ref"; then
            origin_fetch_result="no-such-ref"
        else
            origin_fetch_result="fetch-failed"
        fi
    fi
    _WT_REUSE_REMOTE_BRANCH=false
    if git show-ref --verify --quiet "refs/remotes/origin/$branch"; then
        # #5657: before reusing the remote branch, check whether it has
        # already LANDED on the default branch (e.g. a partial-increment
        # slice whose branch name — feature/issue-N — gets reused by the next
        # slice, and the forge left the ref on origin because
        # auto-delete-head-branches is off). Reusing an already-merged branch
        # produces a worktree whose history conflicts with main and a PR with
        # zero real diff, not the in-flight-cycle case #4823 was written to
        # protect.
        #
        # #7812: asked via the shared `branch_landed` primitive, so this is
        # now correct for a squash merge (forge PR state), a rebase merge
        # (SHAs rewritten — tree equality), and a merge commit (ancestry)
        # alike. `unknown` fails closed to REUSE: a forge outage must never
        # block worktree creation, and reuse is the pre-#5657 behaviour.
        #
        # Plain statement, NOT `$(...)` — command substitution runs in a
        # subshell, which would silently discard the BRANCH_LANDED_* globals
        # this sets.
        # No LOCAL branch of this name exists here (that is this arm's whole
        # premise), so `branch_landed`'s resolution ladder lands on
        # refs/remotes/origin/$branch — the ref actually under question.
        branch_landed "$branch" "$default_branch" >/dev/null
        if [[ "$BRANCH_LANDED_VERDICT" == "landed" ]]; then
            if [[ "$json_output" != "true" ]]; then
                if [[ -n "$BRANCH_LANDED_PR_NUMBER" ]]; then
                    print_info "origin/$branch is the head of already-merged PR #${BRANCH_LANDED_PR_NUMBER} - creating a fresh branch from $base_display instead"
                else
                    print_info "origin/$branch has already landed on $base_display (${BRANCH_LANDED_EVIDENCE}) - creating a fresh branch from $base_display instead"
                fi
            fi
        else
            # not-landed (checked, still live) or unknown (forge unreachable
            # AND the tree comparison unavailable — fail open, never block
            # worktree creation on an outage): preserve today's reuse
            # behavior exactly.
            _WT_REUSE_REMOTE_BRANCH=true
        fi
    else
        # #7765: origin has no ref of this name, so we are about to create a
        # FRESH branch under it. Ref-absence on origin is NOT proof that no PR
        # already claims the name (a fork PR's head never appears as
        # origin/<branch>), and the fetch above may simply have failed. Ask
        # the forge before deciding. May set _WT_REUSE_REMOTE_BRANCH=true
        # (same-repo PR, fetched via refs/pull/<n>/head) or exit non-zero
        # rather than let a fresh branch shadow a real PR.
        _worktree_guard_fresh_branch_against_open_pr "$branch" "$issue_number" "$json_output" "$base_display" "$base_ref" "$origin_fetch_result"
    fi
    return 0
}
