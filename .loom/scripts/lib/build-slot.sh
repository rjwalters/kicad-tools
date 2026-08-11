#!/usr/bin/env bash
# build-slot.sh — machine-wide build-slot lease for the genuinely CPU-heavy
# sweep stages (#4512). Bash half of `loom-daemon/src/build_slot.rs`.
#
# Source this file (do not exec). It defines:
#
#   loom_build_slot_acquire [label]
#       Take a machine-wide build slot, blocking at most
#       LOOM_BUILD_SLOT_WAIT_SECS, then DEGRADING OPEN (returning success with no
#       slot held) rather than blocking longer. Sets LOOM_BUILD_SLOT_PATH to the
#       held lock dir (empty when none) and exports LOOM_BUILD_SLOT_HELD=1 for
#       child processes so a nested acquire is a re-entrant no-op. Always
#       returns 0 — a broken lock store must never fail a build.
#
#   loom_build_slot_release
#       Release the slot taken by the last acquire (idempotent, safe in a trap).
#
# ---------------------------------------------------------------------------
# Why this exists
# ---------------------------------------------------------------------------
# Until #4512, the daemon's admission formula carried a CPU term
# (`estCoresPerSweep` × `cpuUtilizationTarget` minus live idle) that priced
# EVERY sweep as if it were a build. Sweep wall-clock is dominated by API wait
# (curator/builder/judge conversations), so that throttled the low-CPU majority
# to defend against the heavy-build minority — an 8-core worker measured 95%
# idle was capped at 2 concurrent sweeps. #4512 deleted the term and moved the
# protection to where the load actually is: N sweeps run concurrently, but at
# most LOOM_BUILD_SLOTS of them hold this slot while running `cargo
# build`/`test` or the build gate.
#
# The protocol is `mkdir` (POSIX-atomic, and available on stock macOS unlike
# `flock`), matching the token-pool lock and the per-issue claim lock. The slot
# directory is MACHINE-WIDE — ~/.loom/locks/build-slot — deliberately not a
# repo's .loom/locks: cores are one machine-level resource shared by every
# workspace and every worktree on the host.
#
# ---------------------------------------------------------------------------
# Safety properties (all three are load-bearing)
# ---------------------------------------------------------------------------
#   1. NEVER deadlocks — the wait is bounded; on expiry we proceed unserialized.
#   2. Degrades OPEN on any lock-store failure (unwritable/missing dir).
#   3. Re-entrant — LOOM_BUILD_SLOT_HELD short-circuits a nested acquire, so the
#      daemon's main-health gate holding a slot around build-gate.sh does not
#      make build-gate.sh wait on the slot its own parent holds.
#
# A slot is held for the duration of a build (minutes), so the stale-reap
# threshold is deliberately long (LOOM_BUILD_SLOT_STALE_SECS, default 1h): an
# mkdir lock carries no heartbeat, and a short threshold would let a peer reap a
# perfectly healthy in-progress build. A crashed holder is covered by property 1.
#
# Constants (env-tunable; keep in lock-step with build_slot.rs):
#   LOOM_BUILD_SLOTS            default 1     concurrent slots (0 disables)
#   LOOM_BUILD_SLOT_WAIT_SECS   default 300   bounded wait before degrading open
#   LOOM_BUILD_SLOT_STALE_SECS  default 3600  age at which a slot is reaped
#   LOOM_BUILD_SLOT_DIR         default ~/.loom/locks/build-slot
#   LOOM_BUILD_SLOT_HELD        re-entrancy sentinel (set by the holder)

# Resolved lock dir currently held by this shell (empty = none).
LOOM_BUILD_SLOT_PATH="${LOOM_BUILD_SLOT_PATH:-}"

# Echo the machine-wide slot directory.
loom_build_slot_dir() {
    local dir="${LOOM_BUILD_SLOT_DIR:-}"
    # Trim whitespace; an empty/blank override falls through to $HOME.
    dir="${dir#"${dir%%[![:space:]]*}"}"
    dir="${dir%"${dir##*[![:space:]]}"}"
    if [[ -n "$dir" ]]; then
        printf '%s\n' "$dir"
        return 0
    fi
    printf '%s\n' "${HOME:-/tmp}/.loom/locks/build-slot"
}

# Echo a positive integer env value, or the given default when unset/invalid.
_loom_build_slot_int() {
    local raw="${1:-}" default="$2" allow_zero="${3:-0}"
    if [[ "$raw" =~ ^[0-9]+$ ]]; then
        if [[ "$raw" == "0" && "$allow_zero" != "1" ]]; then
            printf '%s\n' "$default"
        else
            printf '%s\n' "$raw"
        fi
    else
        printf '%s\n' "$default"
    fi
}

# True when this shell (or an ancestor) already holds a slot.
loom_build_slot_held() {
    case "$(printf '%s' "${LOOM_BUILD_SLOT_HELD:-}" | tr '[:upper:]' '[:lower:]')" in
        1 | true | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

# Age of a path in whole seconds. An unreadable mtime echoes 0 (= "brand new"),
# so a lock we cannot age is NEVER reaped — the conservative direction, matching
# `MkdirLock::is_stale` in the Rust half.
_loom_build_slot_age_secs() {
    local path="$1" mtime now
    # GNU `stat -c` first (it is an illegal option on BSD/macOS, so it fails
    # cleanly there), then BSD `stat -f %m`. Note the reverse order would
    # MISFIRE on GNU: `stat -f %m <path>` there means --file-system and prints a
    # multi-line report rather than failing usefully.
    mtime="$(stat -c %Y "$path" 2>/dev/null || true)"
    if [[ ! "$mtime" =~ ^[0-9]+$ ]]; then
        mtime="$(stat -f %m "$path" 2>/dev/null || true)"
    fi
    [[ "$mtime" =~ ^[0-9]+$ ]] || { echo 0; return 0; }
    now="$(date +%s)"
    if (( now > mtime )); then echo $(( now - mtime )); else echo 0; fi
}

# Try every slot once, without blocking. Echoes the acquired path on success.
# Exit: 0 acquired, 1 all busy, 2 lock store unusable.
_loom_build_slot_try_round() {
    local dir="$1" slots="$2" stale="$3"
    local i path busy=0 err=0
    for (( i = 0; i < slots; i++ )); do
        path="$dir/slot-$i"
        if mkdir "$path" 2>/dev/null; then
            printf '%s\n' "$path"
            return 0
        fi
        if [[ -d "$path" ]]; then
            if (( $(_loom_build_slot_age_secs "$path") > stale )); then
                rmdir "$path" 2>/dev/null || true
                if mkdir "$path" 2>/dev/null; then
                    printf '%s\n' "$path"
                    return 0
                fi
            fi
            busy=$((busy + 1))
        else
            # mkdir failed and the path is not a directory ⇒ the store itself is
            # unusable (no permission, missing parent, a FILE in the way).
            err=1
        fi
    done
    if (( busy == 0 && err == 1 )); then
        return 2
    fi
    return 1
}

# Acquire a build slot. Always returns 0 (see the safety properties above).
loom_build_slot_acquire() {
    local label="${1:-build}"
    LOOM_BUILD_SLOT_PATH=""

    if loom_build_slot_held; then
        echo "[build-slot] already inside a build slot (LOOM_BUILD_SLOT_HELD) — re-entrant no-op for '${label}'" >&2
        return 0
    fi

    local slots wait_secs stale dir
    slots="$(_loom_build_slot_int "${LOOM_BUILD_SLOTS:-}" 1 1)"
    if [[ "$slots" == "0" ]]; then
        echo "[build-slot] LOOM_BUILD_SLOTS=0 — build-slot serialization disabled for '${label}'" >&2
        return 0
    fi
    wait_secs="$(_loom_build_slot_int "${LOOM_BUILD_SLOT_WAIT_SECS:-}" 300)"
    stale="$(_loom_build_slot_int "${LOOM_BUILD_SLOT_STALE_SECS:-}" 3600)"
    dir="$(loom_build_slot_dir)"

    if ! mkdir -p "$dir" 2>/dev/null; then
        echo "[build-slot] WARNING: slot dir ${dir} is unusable — proceeding WITHOUT a slot for '${label}' (degrading open)" >&2
        return 0
    fi

    local start now path rc logged_wait=0
    start="$(date +%s)"
    while :; do
        set +e
        path="$(_loom_build_slot_try_round "$dir" "$slots" "$stale")"
        rc=$?
        set -e
        if (( rc == 0 )); then
            LOOM_BUILD_SLOT_PATH="$path"
            export LOOM_BUILD_SLOT_HELD=1
            now="$(date +%s)"
            echo "[build-slot] acquired ${path} for '${label}' after $(( now - start ))s wait" >&2
            return 0
        fi
        if (( rc == 2 )); then
            echo "[build-slot] WARNING: slot lock under ${dir} is unusable — proceeding WITHOUT a slot for '${label}' (degrading open)" >&2
            return 0
        fi
        if (( logged_wait == 0 )); then
            echo "[build-slot] all ${slots} slot(s) busy — '${label}' waiting up to ${wait_secs}s" >&2
            logged_wait=1
        fi
        now="$(date +%s)"
        if (( now - start >= wait_secs )); then
            echo "[build-slot] WARNING: all ${slots} slot(s) still busy after ${wait_secs}s — proceeding WITHOUT a slot for '${label}' (degrading open; high-CPU stages are not serialized this run)" >&2
            return 0
        fi
        sleep 1
    done
}

# Release the slot held by this shell. Idempotent; safe to call from a trap.
loom_build_slot_release() {
    if [[ -n "${LOOM_BUILD_SLOT_PATH:-}" ]]; then
        rmdir "$LOOM_BUILD_SLOT_PATH" 2>/dev/null || true
        echo "[build-slot] released ${LOOM_BUILD_SLOT_PATH}" >&2
        LOOM_BUILD_SLOT_PATH=""
        unset LOOM_BUILD_SLOT_HELD
    fi
    return 0
}
