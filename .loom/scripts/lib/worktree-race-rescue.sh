#!/usr/bin/env bash
# lib/worktree-race-rescue.sh
#
# #6334: worktree.sh's "stale worktree" reset path (`git reset --hard <base>`)
# is gated on a point-in-time staleness check (0 commits ahead of base, no
# uncommitted changes). Between that check and the reset itself, a second
# builder — or any other process re-entering the same worktree — can write
# foreign work into it; the lease record is evidence that a claim exists, not a
# mutex that prevents this. When that race lands, an unqualified
# `git reset --hard` silently discards whatever the other process wrote (the
# #6320 incident).
#
# What this does is the issue's own cheaper alternative to a fleet-wide
# worktree-entry lock: make the destructive STEP safe rather than the entry. It
# re-derives every risk signal immediately before the reset and rescues or
# refuses instead of discarding — plus, since #7463, refuses outright while a
# live process still has the worktree open, because a git-level snapshot cannot
# see tracked edits that are still in flight.
#
# ---------------------------------------------------------------------------
# PORTED TO `loom-daemon worktree-reset` (#8195 slice 6, epic #7810)
# ---------------------------------------------------------------------------
#
# Both function bodies — the four-rung guard and the `/proc`/`lsof` liveness
# probe — now live in `loom-daemon/src/worktree_cli/reset.rs`, where the full
# design rationale they used to carry inline also moved (including why this repo
# deliberately did NOT build a forge-backed cross-host worktree lock instead,
# and why untracked files need no rescue from a `git reset --hard`).
#
# This file keeps the function NAME and its exact contract, because both
# consumers reach it by name: `worktree.sh`'s stale-worktree arm, and
# `defaults/scripts/tests/test-worktree-race-rescue.sh`, whose 25 assertions are
# unchanged from the shell implementation and are the equivalence evidence for
# retiring it.
#
# The contract this wrapper preserves, verbatim:
#
#   loom_worktree_reset_or_rescue <worktree_path> <target_ref> [<rescue_label>]
#     0  reset succeeded — the worktree was clean/stale, or its foreign tracked
#        changes were rescued to a patch file under <worktree>/.snapshots/ first
#     1  refused to reset — a live process still holds the worktree open, it
#        gained commits since the staleness check, or its foreign tracked
#        changes could not be captured (the reset was NOT attempted in any of
#        these cases; the worktree is unchanged from before the call)
#     2  the reset itself failed (bad ref, git error) — any rescue that happened
#        above already succeeded; only the reset step failed
#
#   …plus every message, on stderr, byte for byte.
#
# WHY A MISSING BINARY RETURNS 1, and not the 2 every other epic-#7810 stub
# reserves for "could not run at all": here 2 is already an ANSWER, and it is
# the one an operator reads as "git refused the ref". 1 is the only truthful
# code available — the worktree was not reset and nothing was changed, which is
# exactly what 1 means. 0 is the one outcome that must never happen: the caller
# would go on believing a stale worktree had been resynced to its base and hand
# it to a Judge or Doctor as if it had.
#
# That degradation is also SAFE in the direction that matters. worktree.sh's
# call site reads any non-zero as "Could not reset stale worktree (continuing to
# use as-is)" and exits 0 — the pre-#6334 behaviour, a worktree left alone. So a
# host with no loom-daemon loses a reset, never data. This is why the delegation
# is safe where slice 1's lock delegation was not (#8226): that one sat on the
# always-taken create path and could only fail; this one is reached solely when
# the worktree directory already exists AND looks stale, and it degrades.
#
# requires-daemon: worktree-reset optional  #8195 slice 6 — a daemon predating the port cannot run the guard; this wrapper then returns 1 ("refused, nothing changed") and worktree.sh keeps the stale worktree as-is
#
# shellcheck source=locate-daemon-bin.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/locate-daemon-bin.sh" 2>/dev/null || true

loom_worktree_reset_or_rescue() {
    local worktree_path="$1"
    local target_ref="$2"
    local rescue_label="${3:-loom-race-rescue}"

    local bin
    bin="$(loom_resolve_self_daemon_bin 2>/dev/null || true)"
    # A binary that predates the port is checked for HERE rather than left to
    # fail on the real invocation, because clap answers an unknown subcommand
    # with exit 2 — and 2 is an ANSWER in this contract ("the reset itself
    # failed"), so an un-ported daemon would report that a reset was attempted
    # and refused by git. This costs one extra ~0.2s spawn, and only on the
    # stale-worktree arm of worktree.sh (a re-invocation against a worktree that
    # exists and looks stale), never on first creation.
    if [[ -z "$bin" ]] || ! "$bin" worktree-reset --help >/dev/null 2>&1; then
        echo "loom_worktree_reset_or_rescue: refusing to reset $worktree_path — no loom-daemon with a 'worktree-reset' subcommand could be resolved to run the guard (this install predates #8195 slice 6, or is incomplete; re-run the Loom installer or resync .loom/); leaving the worktree untouched" >&2
        return 1
    fi

    # --ignore-pid: the probe used to run INSIDE this shell, so it could exclude
    # itself with `$$`/`$BASHPID`. It now runs in a child, so the caller's PIDs
    # have to be named or the caller becomes its own live holder — which is
    # exactly the shape test-worktree-race-rescue.sh drives it in (its shell sits
    # at `cd "$TMP/repo"` for the whole file).
    "$bin" worktree-reset \
        --worktree "$worktree_path" \
        --target-ref "$target_ref" \
        --rescue-label "$rescue_label" \
        --ignore-pid "$$" \
        --ignore-pid "${BASHPID:-$$}"
}
