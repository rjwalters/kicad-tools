#!/usr/bin/env bash
# lib/worktree-race-rescue.sh
#
# #6334: worktree.sh's "stale worktree" reset path (`git reset --hard
# <base>`) is gated on a point-in-time staleness check (0 commits ahead of
# base, no uncommitted changes). Between that check and the reset itself, a
# second builder — or any other process re-entering the same worktree — can
# write foreign work into it. The lease record (#6165/#6320/#6333) is
# evidence that a claim exists, not a mutex that prevents this: a `gh` read
# failure, a lease that was never written because the write failed, or a
# claim predating the lease feature all fail open. When that race lands, an
# unqualified `git reset --hard` silently discards whatever the other
# process wrote (the #6320 incident).
#
# Design decision recorded on #6334: this repo does NOT build a new
# fleet-wide (forge-backed) worktree-entry lock. Two reasons:
#
#   1. Host-local exclusion at this exact chokepoint already exists, as a
#      side effect of the pre-existing concurrency lock (#3380/#6014):
#      `acquire_worktree_lock` is taken before the "worktree already exists"
#      branch below is ever reached, and — unlike the `git worktree add`
#      path, which releases the lock explicitly the moment `add` returns —
#      nothing in the "already exists" branch releases it early. It is held
#      until the script exits, which covers this entire reset-in-place
#      sequence. So two `worktree.sh <N>` invocations for the SAME issue on
#      the SAME host are already fully serialized through this file today;
#      the second one only proceeds after the first has finished and
#      released the lock, at which point it re-reads the (now current)
#      worktree state rather than racing it.
#   2. A lock that also spans HOSTS would need to be forge-backed (there is
#      no shared filesystem across hosts) and would inherit the exact
#      fail-open pressures the lease already has (a `gh` read failure or a
#      write that never landed looks identical to "no lock held") — i.e. it
#      would not actually close the gap, only relabel it, at the cost of a
#      new distributed primitive with its own stale-holder-recovery problem
#      (Champion's review of this issue flagged exactly this bundling as
#      too large for one change).
#
# What this file actually does is the issue's own cheaper alternative: make
# the destructive step safe rather than the entry. It narrows the reset
# path's race window by re-deriving both risk signals (new commits, new
# uncommitted tracked changes) immediately before the destructive reset, and
# rescues whatever it finds instead of discarding it:
#
#   - New commits since the caller's staleness check: refuse to reset at all
#     (the same "has real work, preserve it" outcome the caller's own
#     up-front check would have produced had it run a moment later).
#   - New uncommitted TRACKED changes: captured as a patch file under
#     `<worktree>/.snapshots/` — the SAME per-worktree, non-`git-stash`
#     mechanism `worktree.sh snapshot` uses (issue #4778), deliberately NOT
#     `git stash`: `refs/stash` is repo-global across every linked worktree,
#     so stashing here could collide with an unrelated concurrent stash
#     elsewhere (#4821) — the exact hazard class this fix exists to close,
#     reintroduced one level down. A patch file has no shared list to
#     collide on.
#   - Untracked files are never at risk from `git reset --hard` in the first
#     place (this script never runs `git clean`), so there is nothing to
#     rescue for them — they are left exactly where they are.
#
# This does not build a mutex; it converts unrecoverable data loss into a
# recoverable patch file (or a refused reset) for whatever race survives the
# host-local lock above (cross-host, or any future path that bypasses
# worktree.sh entirely).

# loom_worktree_has_live_process <worktree_path>
#
# Issue #7463: `loom_worktree_reset_or_rescue` re-derives git-level risk
# signals (new commits, new tracked changes) immediately before the reset,
# but had zero process-liveness checking — unlike loom-daemon's own
# mid-build-death watchdog (`SweepRegistry::worktree_in_use`, #4449), which
# refuses its own `git reset --hard` + `git clean -fd` whenever a live
# process still has the worktree open. A prior, interrupted session's
# orphaned background process (a build daemon, simulation harness, watch
# script, etc.) can still be alive and writing into this worktree's TRACKED
# files at the exact moment a fresh dispatch decides the worktree looks
# "stale" and safe to reset. This function is the bash-side counterpart of
# `worktree_in_use()`'s live-process-cwd signal, so this reset path gets the
# same live-process veto.
#
# Scope note (do not overstate what this protects): this reset path never
# runs `git clean` (see the file header above), so untracked/gitignored
# output artifacts were never at risk from `git reset --hard` in the first
# place — there is nothing here for a live orphan's *untracked* output to be
# rescued FROM. What a live orphan's process CAN lose to an unguarded
# `git reset --hard` is its **tracked**, uncommitted in-progress edits (the
# same content class `loom_worktree_reset_or_rescue`'s tracked-diff rescue
# already protects against a one-shot race — see below). This check adds a
# second, independent line of defense for that same tracked-edit content:
# even if the git-level tracked-diff snapshot below the caller took a moment
# ago is stale by the time we reach the reset, a still-live writer is direct
# evidence more tracked edits may be in flight that a point-in-time `git
# diff` cannot see yet.
#
# Reuses the lsof cwd-scan pattern already used at
# .loom/scripts/agent-destroy.sh:172 (`lsof <dir> -F ... | awk ...`,
# filtering out the caller's own PID) rather than inventing a new signal
# set — with two fixes that call site does not have:
#
#   1. Field selector. agent-destroy.sh requests `-F pt` (PID + file TYPE)
#      and matches the TYPE field against `"cwd"`, but lsof's TYPE field
#      only ever carries values like REG/DIR/VREG/CHR — `"cwd"` lives in
#      the FD field (`f`), not TYPE (`t`). That mismatch means
#      agent-destroy.sh's own check can never actually match a live
#      process; it is the identical bug already found and fixed on the Rust
#      side by #7259 (for `find_processes_lsof`), just not yet ported back
#      to this bash copy (filed separately — see the PR for #7463 for the
#      issue reference). This function requests `-F pf` (PID + FD) and
#      matches the FD field instead, so it does not reproduce that bug.
#
#   2. Recursion. agent-destroy.sh uses `+d` (lowercase), which scans the
#      target directory NON-recursively: it only matches a process whose
#      cwd is a *direct child* of the directory. A process sitting two or
#      more levels down — an entirely ordinary depth for real work, e.g.
#      `loom-daemon/src/worktree_ops/` — is invisible to `+d`, so the reset
#      would proceed and discard exactly the in-flight tracked edits this
#      check exists to protect. This function uses `+D` (uppercase), the
#      recursive variant, so a live process at ANY depth under the worktree
#      is detected. `+D` walks the tree, so it costs more than `+d`
#      (measured: ~0.4s on a typical source-only worktree, ~4s on an 866MB
#      tree with build artifacts) — acceptable for a one-shot check that
#      runs immediately before a destructive reset, and cheap next to
#      losing a live agent's work.
#
# Probe order and posture mirror the Rust twin exactly
# (`worktree_ops::safety::find_processes_using_directory`, which is what
# `worktree_in_use()` calls for this signal):
#
#   - Linux: walk `/proc/<pid>/cwd` directly. No external tool involved, so
#     a minimal host or a hermetic CI runner without `lsof` still gets a real
#     answer — the original lsof-only shape refused EVERY reset on such a
#     host (the CI runner has no lsof), which silently disabled the
#     stale-worktree rescue path there rather than protecting it.
#   - Everywhere else (macOS/BSD): `lsof +D <dir> -F pf`, cwd-field matches.
#   - Neither probe available, or the probe itself fails: contribute
#     NOTHING — "unknown ⇒ no live holder found", the same
#     fail-open-to-empty contract the Rust side documents. This is the one
#     place the two twins must agree: this signal is defense in depth behind
#     the git-level checks `loom_worktree_reset_or_rescue` already makes
#     (new commits, new tracked changes → rescue patch), not the primary
#     gate, and an unprobable host must not be a host where stale worktrees
#     can never be reset. The degraded case is reported on stderr so it is
#     visible in the role log.
#
# Returns 0 (true) if a live process other than the caller has its cwd
# inside <worktree_path>; 1 (false) if none was found or the host cannot be
# probed.
loom_worktree_has_live_process() {
    local worktree_path="$1"

    local worktree_real
    worktree_real="$(cd "$worktree_path" 2>/dev/null && pwd -P)" || {
        # Nothing to protect if the directory cannot even be entered; the
        # caller's own `git -C` will report the real problem. Mirrors
        # `worktree_in_use()`'s `!wt.exists() ⇒ empty`.
        return 1
    }

    local active_pids=""
    if [[ "$(uname -s 2>/dev/null)" == "Linux" && -d /proc/self ]]; then
        # Linux: /proc/<pid>/cwd symlinks. An unreadable entry (another
        # user's process, or one that exited mid-scan) is skipped, never an
        # error — same as the Rust walk.
        local proc_dir pid cwd
        for proc_dir in /proc/[0-9]*; do
            pid="${proc_dir#/proc/}"
            [[ "$pid" == "$$" || "$pid" == "${BASHPID:-}" ]] && continue
            cwd="$(readlink "$proc_dir/cwd" 2>/dev/null)" || continue
            if [[ "$cwd" == "$worktree_real" || "$cwd" == "$worktree_real"/* ]]; then
                active_pids+="$pid"$'\n'
            fi
        done
    elif command -v lsof >/dev/null 2>&1; then
        local lsof_output
        # Not gated on lsof's exit status (lsof exits 1 for "no matches",
        # and on macOS even for some genuine matches — #7488); the awk
        # parse below treats empty/garbled stdout as "no matches".
        lsof_output="$(lsof +D "$worktree_real" -F pf 2>/dev/null || true)"
        active_pids="$(printf '%s\n' "$lsof_output" | awk '/^p/{pid=substr($0,2)} /^fcwd/{print pid}' | grep -v -x "$$" || true)"
    else
        echo "loom_worktree_has_live_process: no process probe available on this host (no /proc, no lsof) -- cannot verify liveness of $worktree_path; contributing no evidence (matches worktree_in_use()'s unknown-is-empty contract)" >&2
        return 1
    fi

    [[ -n "${active_pids//[[:space:]]/}" ]]
}

# loom_worktree_reset_or_rescue <worktree_path> <target_ref> [<rescue_label>]
#
# Re-checks the worktree's commits-ahead and tracked-diff state immediately
# before the destructive reset; if either shows work that was not there at
# the caller's earlier staleness check, rescues or refuses instead of
# discarding it. Also refuses outright — before touching git at all — if a
# live process still has the worktree open (see
# `loom_worktree_has_live_process` above, #7463): git-level signals are a
# point-in-time snapshot, so a live writer is evidence more tracked edits
# may already be in flight that such a snapshot cannot see yet.
#
# Returns:
#   0  reset succeeded — the worktree was clean/stale, or its foreign
#      tracked changes were rescued to a patch file first
#   1  refused to reset — a live process still holds the worktree open, the
#      worktree gained real commits since the staleness check, or its
#      foreign tracked changes could not be captured to a patch file (reset
#      was NOT attempted in any of these cases; the worktree is unchanged
#      from before this call)
#   2  the reset itself failed (bad ref, git error) — any rescue that
#      happened above already succeeded; only the reset step failed
loom_worktree_reset_or_rescue() {
    local worktree_path="$1"
    local target_ref="$2"
    local rescue_label="${3:-loom-race-rescue}"

    if loom_worktree_has_live_process "$worktree_path"; then
        echo "loom_worktree_reset_or_rescue: refusing to reset $worktree_path — a live process still has it open (cwd inside the worktree); leaving it untouched instead of discarding its in-progress tracked edits" >&2
        return 1
    fi

    local ahead
    ahead="$(git -C "$worktree_path" rev-list --count "${target_ref}..HEAD" 2>/dev/null)" || ahead="0"
    if [[ "$ahead" != "0" ]]; then
        echo "loom_worktree_reset_or_rescue: refusing to reset $worktree_path to $target_ref — it gained $ahead commit(s) ahead since the staleness check; leaving it untouched instead of discarding them" >&2
        return 1
    fi

    # `git diff HEAD --quiet` exits 1 when tracked content differs from HEAD
    # (staged or unstaged), 0 when it does not, and >1 on a genuine git
    # error. This mirrors exactly what `git reset --hard` is about to
    # discard — untracked files are deliberately excluded (see header:
    # `reset --hard` never touches them, so there is nothing to rescue).
    local diff_check_status=0
    git -C "$worktree_path" diff HEAD --quiet 2>/dev/null || diff_check_status=$?

    if [[ "$diff_check_status" -gt 1 ]]; then
        echo "loom_worktree_reset_or_rescue: refusing to reset $worktree_path — could not determine its tracked-diff state against HEAD (git diff exit $diff_check_status)" >&2
        return 1
    fi

    if [[ "$diff_check_status" -eq 1 ]]; then
        local rescue_dir="$worktree_path/.snapshots"
        if ! mkdir -p "$rescue_dir" 2>/dev/null; then
            echo "loom_worktree_reset_or_rescue: refusing to reset $worktree_path — could not create rescue directory $rescue_dir for its foreign tracked changes" >&2
            return 1
        fi

        local patch_path
        patch_path="$rescue_dir/${rescue_label}-$(date -u +%Y%m%dT%H%M%SZ).patch"

        local write_status=0
        git -C "$worktree_path" diff HEAD > "$patch_path" 2>/dev/null || write_status=$?
        if [[ "$write_status" -ne 0 || ! -s "$patch_path" ]]; then
            rm -f "$patch_path" 2>/dev/null || true
            echo "loom_worktree_reset_or_rescue: refusing to reset $worktree_path — failed writing its foreign tracked changes to a rescue patch" >&2
            return 1
        fi

        echo "loom_worktree_reset_or_rescue: rescued foreign tracked changes in $worktree_path to $patch_path before resetting to $target_ref (replay with: git apply $patch_path)" >&2
    fi

    if git -C "$worktree_path" reset --hard "$target_ref" >/dev/null 2>&1; then
        return 0
    fi
    return 2
}
