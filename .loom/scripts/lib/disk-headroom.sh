#!/usr/bin/env bash
# disk-headroom.sh — Resource-gated wave-size math for /loom:sweep (#3566).
#
# Source this file (do not exec). It defines two functions used by the sweep
# skill's Stage -1 "Resolve auto wave size" step:
#
#   loom_worktree_root_free_gb <repo_root>
#       Resolve the worktree-root filesystem (via loom_worktree_root) and echo
#       the integer free space on THAT volume in GB. This is the dedicated
#       scratch volume when LOOM_WORKTREE_ROOT / worktree.root is set (#3539,
#       #3541) — NOT the repo's own drive.
#
#   loom_wave_size_from_disk <mechanism> <candidate_count> <free_gb>
#       Pure integer arithmetic (no I/O). Given the chosen parallelism
#       mechanism, the number of candidate issues, and the free GB on the
#       scratch volume, echo the clamped wave size on line 1 and a machine
#       reason token on line 2. Deterministic and trivially unit-testable.
#
# Two mechanisms, two targets (see #3566, #3289):
#   - daemon   → detached-process path (mcp__loom__dispatch_sweep). Each sweep
#                is its own OS process with its own rotated token, so the #3289
#                nested-subagent stall does not apply. Scales toward N=10.
#   - subagent → in-session Task subagents, one level deep. Bounded by the
#                #3289 stream-pump stall; the validated ceiling is 3. NEVER
#                raise this cap toward 10 — route high parallelism through the
#                daemon path instead.
#
# Constants (all overridable via env for large-repo / tuning cases):
#   LOOM_PER_WORKTREE_GB   default 2   Conservative per-worktree disk estimate.
#                                      A fixed estimate keeps the math pure and
#                                      testable; runtime footprint measurement
#                                      is intentionally out of scope for v1.
#   LOOM_DAEMON_WAVE_TARGET  default 10  Concurrency target for the daemon path.
#   LOOM_SUBAGENT_WAVE_CAP   default 3   #3289-safe ceiling for the subagent path.
#
# Reason tokens emitted on line 2 of loom_wave_size_from_disk (which constraint
# bound the result):
#   target      — result equals the mechanism target; nothing reduced it.
#   candidates  — reduced by the candidate count (fewer issues than the target).
#   disk        — reduced by scratch-volume disk headroom.
#   floor       — the math produced < 1 (e.g. a nearly full disk); floored to 1.

# Resolve worktree-root.sh relative to this file so sourcing works from any cwd.
_LOOM_DISK_HEADROOM_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./worktree-root.sh
source "$_LOOM_DISK_HEADROOM_LIB_DIR/worktree-root.sh"

# loom_worktree_root_free_gb <repo_root>
#
# Echoes the integer free space (GB) on the filesystem that hosts the resolved
# worktree root. The worktree-root leaf (e.g. ${root}/<repo>/issue-N) usually
# does not exist yet, so this walks up to the nearest existing ancestor before
# calling df — df on a non-existent path errors. df -Pk forces the portable
# POSIX 512-independent 1024-byte-block output (macOS df differs from GNU df;
# -P pins the single-line-per-fs columnar format, -k pins 1K blocks), and the
# 4th column of the data row is "Available" in 1K blocks.
loom_worktree_root_free_gb() {
    local repo_root="$1"
    if [[ -z "$repo_root" ]]; then
        echo "loom_worktree_root_free_gb: repo_root argument required" >&2
        echo "0"
        return 0
    fi

    local wt_root
    wt_root="$(loom_worktree_root "$repo_root")"

    # Walk up to the nearest existing ancestor (read-only; never mkdir).
    local probe="$wt_root"
    while [[ -n "$probe" && "$probe" != "/" && ! -e "$probe" ]]; do
        probe="$(dirname "$probe")"
    done

    local avail_k
    avail_k="$(df -Pk "$probe" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [[ -z "$avail_k" || ! "$avail_k" =~ ^[0-9]+$ ]]; then
        # df failed or produced an unexpected shape — report 0 free so the
        # caller floors to a single worktree rather than crashing.
        echo "0"
        return 0
    fi

    # 1K blocks -> GB (integer floor).
    echo "$(( avail_k / 1024 / 1024 ))"
}

# loom_wave_size_from_disk <mechanism> <candidate_count> <free_gb>
#
# Pure integer arithmetic. Echoes two lines:
#   line 1: the clamped wave size K = min(target, floor(free_gb/PER), candidates), floor 1
#   line 2: a reason token (target|candidates|disk|floor)
# where target is 10 for mechanism=daemon and 3 for mechanism=subagent.
loom_wave_size_from_disk() {
    local mechanism="$1" candidate_count="$2" free_gb="$3"

    local per="${LOOM_PER_WORKTREE_GB:-2}"
    local daemon_target="${LOOM_DAEMON_WAVE_TARGET:-10}"
    local subagent_cap="${LOOM_SUBAGENT_WAVE_CAP:-3}"

    # Validate integer inputs (defensive — the pure math must not silently
    # coerce garbage into a plausible-looking wave size).
    local n
    for n in "$candidate_count" "$free_gb" "$per" "$daemon_target" "$subagent_cap"; do
        if [[ ! "$n" =~ ^[0-9]+$ ]]; then
            echo "loom_wave_size_from_disk: non-integer input '$n'" >&2
            return 2
        fi
    done
    if (( per < 1 )); then
        echo "loom_wave_size_from_disk: LOOM_PER_WORKTREE_GB must be >= 1 (got $per)" >&2
        return 2
    fi

    local target
    case "$mechanism" in
        daemon)   target="$daemon_target" ;;
        subagent) target="$subagent_cap" ;;
        *)
            echo "loom_wave_size_from_disk: unknown mechanism '$mechanism' (expected 'daemon' or 'subagent')" >&2
            return 2
            ;;
    esac

    local max_by_disk=$(( free_gb / per ))

    # Start at the mechanism target and reduce by each binding constraint.
    # Precedence when constraints tie: candidates before disk before floor, so
    # a strictly-smaller disk headroom wins the reason, and a hard floor wins
    # over everything.
    local k="$target" reason="target"
    if (( candidate_count < k )); then
        k="$candidate_count"
        reason="candidates"
    fi
    if (( max_by_disk < k )); then
        k="$max_by_disk"
        reason="disk"
    fi
    if (( k < 1 )); then
        k=1
        reason="floor"
    fi

    echo "$k"
    echo "$reason"
}
