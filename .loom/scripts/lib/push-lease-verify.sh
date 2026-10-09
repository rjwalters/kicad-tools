#!/usr/bin/env bash
# push-lease-verify.sh - Verify the *actual* post-push ref state after a
# `git push --force-with-lease` reports a rejection (#6695).
#
# Background: Git LFS's pre-push hook can race the lease re-check on a
# branch with pending LFS objects. The hook uploads LFS objects and the ref
# update proceeds on the remote, while the client-side lease comparison
# that produced the printed rejection
#
#   ! [rejected]  <branch> -> <branch> (stale info)
#   error: failed to push some refs to '...'
#   remote rejected ... is at <new-sha> but expected <old-sha>
#
# was evaluated against a different (already-stale) view. The net effect
# observed live: `git push --force-with-lease` prints a rejection and exits
# non-zero, yet the ref update actually landed — confirmed via
# `git ls-remote` and the `origin/<branch>` reflog. Both occurrences were on
# branches with LFS objects to upload; a push on a branch with no LFS
# objects did not exhibit it.
#
# A caller that trusts the exit status / stderr text alone will wrongly
# conclude the push failed, and any of the "safe" responses it might take —
# retry the push, re-rebase, report failure upstream — are wrong against a
# ref that already moved.
#
# Usage:
#   source ".../lib/push-lease-verify.sh"
#   if ! run git push --force-with-lease; then
#       if push_landed_despite_rejection origin "$branch" "$expected_sha"; then
#           warn "PUSH-LEASE-RACE-DETECTED: ..."
#       else
#           err "... genuinely rejected ..."
#       fi
#   fi
#
# Optionally pass a `git -C <dir>`-style command prefix as trailing args when
# the push ran against a branch checked out in a worktree other than the
# caller's own cwd:
#   push_landed_despite_rejection origin "$branch" "$expected_sha" git -C "$worktree"

# push_landed_despite_rejection <remote> <branch> <expected-local-sha> [git-cmd...]
#
# Queries the LIVE remote ref (never a local remote-tracking ref, which can
# be stale) for <branch> and compares it against <expected-local-sha> — the
# sha the failed push was trying to publish. Returns 0 (landed despite the
# reported rejection) when they match, 1 (genuinely rejected, or the remote
# state could not be determined) otherwise.
push_landed_despite_rejection() {
    local remote="$1" branch="$2" expected_sha="$3"
    shift 3
    local -a git_cmd=("$@")
    if [[ ${#git_cmd[@]} -eq 0 ]]; then
        git_cmd=(git)
    fi

    [[ -n "$expected_sha" ]] || return 1

    local remote_sha
    remote_sha="$("${git_cmd[@]}" ls-remote "$remote" "refs/heads/$branch" 2>/dev/null | cut -f1)"

    [[ -n "$remote_sha" && "$remote_sha" == "$expected_sha" ]]
}
