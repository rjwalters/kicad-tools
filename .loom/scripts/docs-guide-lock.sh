#!/usr/bin/env bash
# Loom Docs Guide Lock - atomic guard around the Guide role's Document
# Maintenance phase, from Step 1's open-docs-PR check through Step 5's
# `gh pr create` (issue #5573).
#
# Usage:
#   ./.loom/scripts/docs-guide-lock.sh acquire   # exit 0 = acquired, 1 = busy
#   ./.loom/scripts/docs-guide-lock.sh release   # always exits 0 (idempotent)
#   ./.loom/scripts/docs-guide-lock.sh status    # exit 0 = held (not stale), 1 = free
#
# Why this exists
# ----------------
# guide.md's Document Maintenance Step 1 used to be a plain check-then-act:
# query for an open `docs/guide-update*` PR, and if none is open, proceed
# through docs-worktree.sh (which resets a SINGLE shared worktree slot),
# write the docs, and `gh pr create`. Two Guide ticks starting within the same
# short window (a role-runner tick overlapping a manual `/loom:guide` session,
# or two role-runner ticks overlapping each other) could both see "no open
# PR" before either had pushed a branch, both call docs-worktree.sh (the
# second clobbering the first's in-progress worktree state), and both end up
# creating their own docs PR. This happened: #5571 and #5572, opened 49
# seconds apart with an identical diff shape.
#
# This lock closes the gap: the phase acquires it BEFORE the open-PR check
# and releases it only after `gh pr create` succeeds (or the phase decides
# there is nothing to do). A second tick that arrives while the lock is held
# fails to acquire and skips outright — it never reaches docs-worktree.sh or
# `gh pr create`, so the race is structurally impossible, not just narrowed.
#
# Concurrency primitive: POSIX-atomic `mkdir` under
# .loom/locks/docs-guide-lock/ — the same primitive worktree.sh's
# worktree-add lock and lib/build-slot.sh use (`flock` is not available on
# stock macOS, so `mkdir` is the only portable atomic filesystem op we can
# rely on).
#
# Staleness is AGE-based only, deliberately NOT PID-liveness (unlike
# worktree.sh's lock). worktree.sh's lock is held by one continuously running
# `worktree.sh` process for the duration of a single `git worktree add`, so
# checking whether that PID is still alive is a meaningful staleness signal.
# This lock is different: the Guide's Document Maintenance phase is a role
# PROMPT (defaults/.claude/commands/loom/guide.md) that an agent executes as a
# sequence of separate Bash tool-call subprocesses (interleaved with Edit tool
# calls to write the docs) across one turn — there is no single process alive
# for the whole Step-1-through-Step-5 window. The PID recorded by the
# `acquire` subcommand exits the instant that one command returns, long
# before Step 5 runs, so a PID-liveness check would misclassify every
# legitimate in-flight lock as stale within seconds. Age is the only signal
# available here — the same tradeoff lib/build-slot.sh made for the same
# reason (a build-gate stage is not one continuously running shell either).
#
# Crash recovery: if a tick dies mid-phase without releasing (agent crash,
# host restart, cancelled sweep), the lock is reaped automatically the next
# time `acquire` is attempted, once it is older than
# LOOM_DOCS_GUIDE_LOCK_STALE_SECS (default 1800s / 30 min — generous headroom
# over the phase's normal one-to-few-minute runtime, so a crashed tick can
# never permanently block future ticks, and a live tick is never reaped out
# from under itself).
#
# Exit codes:
#   acquire: 0 = lock acquired, 1 = busy (another tick holds a live lock)
#   release: always 0 (idempotent; safe to call even if never acquired)
#   status:  0 = held and not stale, 1 = free (absent or stale)
#   invalid usage: 2

# Scope: SAME-HOST ONLY (#5615)
# ------------------------------
# This lock is a local filesystem `mkdir` under THIS repo checkout's
# .loom/locks/ directory. It only ever serializes Document Maintenance ticks
# that share a filesystem -- i.e. concurrent ticks on the SAME host (a
# role-runner tick overlapping a manual `/loom:guide` session, or two
# role-runner ticks on the same host overlapping each other, the #5573 case
# this lock was built for).
#
# It provides ZERO mutual exclusion across the fleet. Every independent host
# running the daemon's role runner (or a cron-dispatched Guide) has its own
# checkout and its own .loom/locks/ -- two hosts can each acquire their OWN
# local lock, both see "no open docs PR" at Step 1's check, and both proceed
# to create a docs PR. This was observed live: #5615, PR #5612 appearing
# while a manual session's local lock was continuously held on a different
# host. Do NOT assume acquiring this lock guarantees you are the only Guide
# tick in flight fleet-wide -- it only guarantees that for THIS host.
#
# The cross-host gap is closed separately, in guide.md's create_docs_pr()
# (Step 5): an immediate re-check of the open-docs-PR search right before
# `gh pr create`, using an uncached `gh` call so a PR opened moments earlier
# by another host is never masked by gh-cached's read TTL. That shrinks the
# cross-host TOCTOU window from "the entire Step 1-5 phase" (this lock's
# window) down to "the gap between that recheck and `gh pr create`" -- the
# same narrowing tactic Judge/Champion's Verdict-Time CAS Recheck uses for
# the analogous PR-label race, not a hard fleet-wide guarantee. See
# guide.md's Step 1 and Step 5 comments for the full mechanism.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

print_error() { echo -e "${RED}ERROR: $1${NC}" >&2; }
print_info() { echo -e "${BLUE}ℹ $1${NC}" >&2; }
print_success() { echo -e "${GREEN}✓ $1${NC}" >&2; }

show_help() {
    cat <<'EOF'
Loom Docs Guide Lock

Usage: ./.loom/scripts/docs-guide-lock.sh <acquire|release|status>

Atomic mkdir-based lock serializing the Guide role's Document Maintenance
phase (Step 1's open-docs-PR check through Step 5's `gh pr create`) across
concurrent Guide ticks. See the header comment in this file for the full
rationale (issue #5573).

Commands:
  acquire   Non-blocking. Exit 0 if the lock was acquired, 1 if another tick
            already holds a live (non-stale) lock.
  release   Release the lock. Idempotent -- exit 0 even if not held.
  status    Exit 0 if the lock is currently held and not stale, 1 otherwise.
  age       Print the age (whole seconds) of the currently-held lock to
            stdout and exit 0, or print nothing and exit 1 if the lock is
            not held. Used by guide.md to compute a Document Maintenance
            phase-duration proxy for guide-docs-telemetry.sh (issue #6136) --
            staleness does not affect this, it reports raw elapsed time.

Tunables (env vars):
  LOOM_DOCS_GUIDE_LOCK_STALE_SECS   Age in seconds after which a held lock is
                                    treated as abandoned and reaped on the
                                    next acquire attempt (default 1800 = 30m).
EOF
}

CMD="${1:-}"
if [[ "$CMD" == "--help" ]] || [[ "$CMD" == "-h" ]]; then
    show_help
    exit 0
fi
if [[ "$CMD" != "acquire" ]] && [[ "$CMD" != "release" ]] && [[ "$CMD" != "status" ]] && [[ "$CMD" != "age" ]]; then
    print_error "Unknown or missing command: '${CMD}'"
    show_help >&2
    exit 2
fi

LOOM_DOCS_GUIDE_LOCK_STALE_SECS="${LOOM_DOCS_GUIDE_LOCK_STALE_SECS:-1800}"
if ! [[ "$LOOM_DOCS_GUIDE_LOCK_STALE_SECS" =~ ^[0-9]+$ ]]; then
    print_error "LOOM_DOCS_GUIDE_LOCK_STALE_SECS must be a non-negative integer (got: '$LOOM_DOCS_GUIDE_LOCK_STALE_SECS')"
    exit 2
fi

# Resolve the locks directory the same way worktree.sh's worktree-add lock
# does: relative to the canonical git-common-dir, so the main workspace and
# every worktree share the same lock namespace regardless of cwd.
GIT_COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null) || {
    print_error "Not in a git repository"
    exit 1
}
REPO_ROOT=$(cd "$(dirname "$GIT_COMMON_DIR")" && pwd -P)
LOCKS_DIR="$REPO_ROOT/.loom/locks"
LOCK_PATH="$LOCKS_DIR/docs-guide-lock"

# Age of a path in whole seconds. An unreadable mtime echoes 0 (= "brand
# new"), so a lock we cannot age is NEVER reaped -- the conservative
# direction (mirrors lib/build-slot.sh's _loom_build_slot_age_secs).
_lock_age_secs() {
    local path="$1" mtime now
    mtime="$(stat -c %Y "$path" 2>/dev/null || true)"
    if [[ ! "$mtime" =~ ^[0-9]+$ ]]; then
        mtime="$(stat -f %m "$path" 2>/dev/null || true)"
    fi
    [[ "$mtime" =~ ^[0-9]+$ ]] || { echo 0; return 0; }
    now="$(date +%s)"
    if (( now > mtime )); then echo $(( now - mtime )); else echo 0; fi
}

_lock_is_stale() {
    [[ -d "$LOCK_PATH" ]] || return 1
    (( $(_lock_age_secs "$LOCK_PATH") > LOOM_DOCS_GUIDE_LOCK_STALE_SECS ))
}

case "$CMD" in
    acquire)
        mkdir -p "$LOCKS_DIR" 2>/dev/null || true

        if mkdir "$LOCK_PATH" 2>/dev/null; then
            : # acquired on the first try
        elif _lock_is_stale; then
            print_info "Reaping stale docs-guide lock (age > ${LOOM_DOCS_GUIDE_LOCK_STALE_SECS}s) — a prior tick likely crashed mid-phase"
            rm -rf "$LOCK_PATH" 2>/dev/null || true
            if ! mkdir "$LOCK_PATH" 2>/dev/null; then
                print_info "Lost the race to re-acquire after reaping — another tick got there first"
                exit 1
            fi
        else
            print_info "docs-guide lock is held by another (non-stale) tick — skipping"
            exit 1
        fi

        cat > "$LOCK_PATH/owner.json" <<EOF
{
  "acquired_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "acquired_by_pid": $$,
  "note": "PID is informational only -- staleness is age-based, see header comment"
}
EOF
        print_success "docs-guide lock acquired"
        exit 0
        ;;
    release)
        rm -rf "$LOCK_PATH" 2>/dev/null || true
        print_success "docs-guide lock released"
        exit 0
        ;;
    status)
        if [[ -d "$LOCK_PATH" ]] && ! _lock_is_stale; then
            exit 0
        fi
        exit 1
        ;;
    age)
        if [[ -d "$LOCK_PATH" ]]; then
            _lock_age_secs "$LOCK_PATH"
            exit 0
        fi
        exit 1
        ;;
esac
