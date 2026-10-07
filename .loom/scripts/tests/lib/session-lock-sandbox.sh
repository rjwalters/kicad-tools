#!/usr/bin/env bash
# session-lock-sandbox.sh — keep a suite's session-exec dispatch locks out of
# the real ~/.loom (#10364).
#
# `loom-daemon session-exec host` takes a per-container lock file under
# `$LOOM_SESSION_LOCK_DIR`, defaulting to `$HOME/.loom/session-locks`. A suite
# that runs the real binary must never write there: fixture account names
# would litter the operator's directory, and on a fleet host one could match a
# real account. Source this IN PLACE OF the suite's own cleanup trap, with the
# suite's temp root:
#
#   TMPROOT="$(mktemp -d)"
#   source "$SCRIPT_DIR/lib/session-lock-sandbox.sh" "$TMPROOT"
#
# It points LOOM_SESSION_LOCK_DIR inside the temp root and installs the EXIT
# trap, which removes the temp root and fails the suite if a lock named for
# one of the suites' FIXTURE containers appeared or changed under the real
# directory during the run. Only fixture names are watched: on a fleet host
# the live daemon legitimately creates other lock files meanwhile, and the
# real directory need not exist at all (a fresh CI runner). Every command is
# safe under `set -euo pipefail` and bash 3.2. The suite's own exit status is
# otherwise kept.

_lss_root="$1"
_lss_real="${HOME}/.loom/session-locks"
# Every fixture session container the wired suites name.
_lss_fixtures="loom-codex-session-acct loom-codex-session-session-acct loom-codex-session-forced"
export LOOM_SESSION_LOCK_DIR="${_lss_root}/session-locks"
: >"${_lss_root}/session-locks.marker"

# The fixture locks present under the real directory, space-separated.
_lss_present() {
    local name found=""
    for name in $_lss_fixtures; do
        if [[ -e "${_lss_real}/${name}.lock" ]]; then
            found="${found} ${name}"
        fi
    done
    printf '%s' "$found"
}
_lss_before="$(_lss_present)"

_lss_exit() {
    local rc=$?
    local name changed=""
    for name in $_lss_fixtures; do
        local lock="${_lss_real}/${name}.lock"
        if [[ ! -e "$lock" ]]; then
            continue
        fi
        if [[ " ${_lss_before} " != *" ${name} "* ]]; then
            changed="${changed} ${name}.lock(created)"
        elif [[ -n "$(find "$lock" -newer "${_lss_root}/session-locks.marker" 2>/dev/null || true)" ]]; then
            changed="${changed} ${name}.lock(modified)"
        fi
    done
    if [[ -n "$changed" ]]; then
        echo "FAIL: this suite wrote fixture locks under the real ${_lss_real}:${changed} (#10364)" >&2
        rc=1
    fi
    rm -rf "$_lss_root"
    exit "$rc"
}
trap _lss_exit EXIT
