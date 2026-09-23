#!/usr/bin/env bash
# test-run-ci-suites-daemon-guard.sh — the live-daemon guard in
# run-ci-suites.sh (issue #6386).
#
# #6386: an Auditor ran `bash defaults/scripts/tests/run-ci-suites.sh` from a
# fleet host's LIVE checkout. The daemon-lifecycle suites execute the real
# loom-daemon-{start,stop,update,quiesce}.sh, one of their cases resolved the
# live `.loom/.daemon.pid`, and the fleet's authoritative dispatcher was
# SIGTERM'd for 11 hours. The per-suite sandboxes are necessary but not
# sufficient — one forgotten pin in one case is enough — so run-ci-suites.sh
# now refuses to run those suites at all when a daemon pid file exists on the
# host.
#
# Driven entirely through `run-ci-suites.sh --plan`, which prints the RUN/SKIP
# decision for every wired suite and exits WITHOUT executing any of them. That
# keeps this suite hermetic (nothing is started, killed, or written outside
# $WORKDIR) and fast.
#
# Every case pins the candidate pid-file list via the guard's TEST-ONLY seam
# LOOM_CI_DAEMON_PIDFILE_CANDIDATES (`none` = no candidates at all), so no
# assertion depends on whether THIS host happens to be running a real daemon —
# which is exactly the condition under test, and would otherwise make "the
# guard is not always-on" unfalsifiable on a fleet host and "the guard fires"
# unfalsifiable on a CI runner.
#
# Usage:
#   ./defaults/scripts/tests/test-run-ci-suites-daemon-guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$SCRIPT_DIR/run-ci-suites.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} $1"
}
fail() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} $1"
    [[ -n "${2:-}" ]] && echo "$2" | sed 's/^/    /'
}
check() {
    local rc="$1" msg="$2" detail="${3:-}"
    if [[ "$rc" -eq 0 ]]; then pass "$msg"; else fail "$msg" "$detail"; fi
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# The host-mutating suites the guard owns. Kept as explicit literal lists (not
# scraped from the runner) so a silent narrowing of the guard's own
# LIVE_DAEMON_GUARDED_SUITES is a test failure, not an invisible regression.
#
# Split in two by #8086, because guard MEMBERSHIP and CI WIRING stopped being
# the same question. test-loom-daemon-watchdog.sh moved to ci-excluded.txt (it
# drives a thin stub over `loom-daemon daemon-watchdog` and so needs a built
# binary, which run-ci-suites.sh's job has no toolchain for), but it is still
# every bit as host-mutating as before and must stay named in the guard. It
# simply cannot be asserted through `--plan`, which only ever enumerates
# ci-wired.txt.
#
# Dropping it from this file instead would be exactly the silent narrowing the
# literal exists to catch — so it is asserted DIFFERENTLY, not less: named in
# the runner's own literal, and absent from the plan (proving the exclusion is
# real rather than a half-applied move).
#
# #8087 moved test-loom-daemon-start.sh and test-loom-daemon-update.sh across
# the same line, for the same reason: cli/loom-daemon-start.sh is now a thin
# stub over `loom-daemon daemon-start` (the update suite reaches it through
# lib/daemon-update-fixtures.sh, which copies the script into every fixture).
# Both remain exactly as host-mutating as they were — they still `kill`, `rm
# -f` pid files and `launchctl bootout` / `systemctl --user disable` whatever
# they resolve — so both stay named in the runner's guard and simply move to
# the unwired half of this assertion.
GUARDED_WIRED_SUITES=(
    test-loom-daemon-stop.sh
    test-loom-daemon-quiesce.sh
)
GUARDED_UNWIRED_SUITES=(
    test-loom-daemon-watchdog.sh
    test-loom-daemon-start.sh
    test-loom-daemon-update.sh
)

plan_line() { # <plan output> <suite>
    printf '%s\n' "$1" | awk -v s="$2" '$2 == s { print $1; exit }'
}

# Every --plan invocation below pins the candidate list, so nothing about the
# host running this suite can influence the outcome.
run_plan() { # <pidfile-candidates|none> [stderr-path]
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES="$1" bash "$RUNNER" --plan 2>"${2:-/dev/null}"
}

# ---------- 1. no pid file anywhere -> every daemon suite is planned to RUN ----
ABSENT_PLAN="$( run_plan "$WORKDIR/absent-a.pid:$WORKDIR/absent-b.pid" )"
absent_rc=$?
check "$absent_rc" "no daemon pid file: --plan exits 0"

missing=""
for suite in "${GUARDED_WIRED_SUITES[@]}"; do
    [[ "$(plan_line "$ABSENT_PLAN" "$suite")" == "RUN" ]] || missing="$missing $suite"
done
check "$([[ -z "$missing" ]] && echo 0 || echo 1)" \
    "no daemon pid file: every WIRED daemon-lifecycle suite is planned to RUN (guard is not always-on)" \
    "not planned RUN:$missing"

# The guarded-but-unwired members (#8086): still named in the runner's own
# LIVE_DAEMON_GUARDED_SUITES literal, and genuinely absent from the plan.
# Both halves matter — the first catches a silent narrowing of the guard, the
# second catches a half-applied exclusion that left the suite wired anyway.
unnamed="" still_planned=""
for suite in "${GUARDED_UNWIRED_SUITES[@]}"; do
    grep -q "LIVE_DAEMON_GUARDED_SUITES=.*$suite" "$RUNNER" || unnamed="$unnamed $suite"
    [[ -z "$(plan_line "$ABSENT_PLAN" "$suite")" ]] || still_planned="$still_planned $suite"
done
check "$([[ -z "$unnamed" ]] && echo 0 || echo 1)" \
    "guarded-but-unwired suites are still named in LIVE_DAEMON_GUARDED_SUITES (no silent narrowing)" \
    "not named in the runner:$unnamed"
check "$([[ -z "$still_planned" ]] && echo 0 || echo 1)" \
    "guarded-but-unwired suites are absent from --plan (the ci-excluded move is real)" \
    "still planned:$still_planned"

# A non-daemon suite is a control: it must RUN in both directions below.
check "$([[ "$(plan_line "$ABSENT_PLAN" "test-live-state-sandbox.sh")" == "RUN" ]] && echo 0 || echo 1)" \
    "no daemon pid file: an unrelated suite is planned to RUN (control)"

# ---------- 2. a live-looking pid file -> the daemon suites are SKIPPED --------
# "Live-looking" without ever naming a real daemon: the pid recorded is this
# test process's own, which is unambiguously alive and unambiguously not a
# daemon. Nothing in this suite ever signals it.
LIVE_PID_FILE="$WORKDIR/live/.loom/.daemon.pid"
mkdir -p "$(dirname "$LIVE_PID_FILE")"
echo "$$" > "$LIVE_PID_FILE"

LIVE_PLAN_ERR="$WORKDIR/live-plan.err"
LIVE_PLAN="$( run_plan "$LIVE_PID_FILE" "$LIVE_PLAN_ERR" )"
live_rc=$?
check "$live_rc" "live pid file: --plan still exits 0 (a skip is not a failure)"

not_skipped=""
for suite in "${GUARDED_WIRED_SUITES[@]}"; do
    [[ "$(plan_line "$LIVE_PLAN" "$suite")" == "SKIP" ]] || not_skipped="$not_skipped $suite"
done
check "$([[ -z "$not_skipped" ]] && echo 0 || echo 1)" \
    "live pid file: every WIRED daemon-lifecycle suite is SKIPPED (#6386 hazard is unreachable)" \
    "not skipped:$not_skipped"

check "$([[ "$(plan_line "$LIVE_PLAN" "test-live-state-sandbox.sh")" == "RUN" ]] && echo 0 || echo 1)" \
    "live pid file: unrelated suites still RUN (the guard is scoped, not a blanket refusal)"

# The skip must be LOUD — an invisible skip reads as "the suites passed".
guard_err="$(cat "$LIVE_PLAN_ERR" 2>/dev/null)"
check "$([[ "$guard_err" == *"LIVE DAEMON DETECTED"* ]] && echo 0 || echo 1)" \
    "live pid file: the guard announces itself loudly on stderr" "$guard_err"
check "$([[ "$guard_err" == *"$LIVE_PID_FILE"* ]] && echo 0 || echo 1)" \
    "live pid file: the guard names the exact pid file it found" "$guard_err"
check "$([[ "$guard_err" == *"LOOM_CI_ALLOW_DAEMON_SUITES"* ]] && echo 0 || echo 1)" \
    "live pid file: the guard names the override that re-enables the suites" "$guard_err"

# ---------- 3. a STALE pid file also trips the guard --------------------------
# The lifecycle suites `rm -f` whichever pid file they resolve, so even a pid
# file naming a dead process is host state they must not silently delete (and
# it is the operator's evidence of how the daemon last exited).
STALE_PID_FILE="$WORKDIR/stale/.loom/.daemon.pid"
mkdir -p "$(dirname "$STALE_PID_FILE")"
echo "2147483646" > "$STALE_PID_FILE"   # far above any live pid on a real host
STALE_PLAN="$( run_plan "$STALE_PID_FILE" )"
check "$([[ "$(plan_line "$STALE_PLAN" "test-loom-daemon-stop.sh")" == "SKIP" ]] && echo 0 || echo 1)" \
    "stale pid file: still skipped (the suites rm -f whatever they resolve)"

# ---------- 4. LOOM_CI_ALLOW_DAEMON_SUITES=1 is an explicit override ----------
OVERRIDE_ERR="$WORKDIR/override.err"
OVERRIDE_PLAN="$( LOOM_CI_ALLOW_DAEMON_SUITES=1 run_plan "$LIVE_PID_FILE" "$OVERRIDE_ERR" )"
check "$([[ "$(plan_line "$OVERRIDE_PLAN" "test-loom-daemon-stop.sh")" == "RUN" ]] && echo 0 || echo 1)" \
    "LOOM_CI_ALLOW_DAEMON_SUITES=1: the daemon suites are planned to RUN again"
check "$([[ "$(cat "$OVERRIDE_ERR")" == *"LOOM_CI_ALLOW_DAEMON_SUITES is set"* ]] && echo 0 || echo 1)" \
    "LOOM_CI_ALLOW_DAEMON_SUITES=1: the override is still announced (never silent)" \
    "$(cat "$OVERRIDE_ERR")"

# ---------- 5. --plan executes nothing ----------------------------------------
# Proven by a canary: --plan must not create the log file a real run writes for
# the very first suite it would execute.
CANARY_LOG="/tmp/ci-suite-test-live-state-sandbox.sh.log"
canary_mtime() { # portable: GNU stat first, then BSD/macOS; "absent" when missing
    [[ -f "$CANARY_LOG" ]] || { echo absent; return 0; }
    stat -c %Y "$CANARY_LOG" 2>/dev/null || stat -f %m "$CANARY_LOG" 2>/dev/null || echo "?"
}
canary_before="$(canary_mtime)"
run_plan "$WORKDIR/absent-c.pid" >/dev/null
canary_after="$(canary_mtime)"
check "$([[ "$canary_before" == "$canary_after" ]] && echo 0 || echo 1)" \
    "--plan runs no suite at all (per-suite log untouched)"

# ---------- 6. an unknown option is rejected ----------------------------------
bad_out="$(bash "$RUNNER" --nope 2>&1)"
bad_rc=$?
check "$([[ "$bad_rc" -eq 1 && "$bad_out" == *"unknown option"* ]] && echo 0 || echo 1)" \
    "an unrecognized option is rejected before anything runs (rc=$bad_rc)" "$bad_out"

# ---------- 7. `none` means no candidates at all ------------------------------
# The seam's empty setting has to be distinguishable from "seam not set", or a
# case that means "this host has nothing" would silently fall back to the
# derived tiers and assert against the REAL host — vacuous on CI, wrong on a
# fleet host.
NONE_PLAN="$( run_plan none )"
check "$([[ "$(plan_line "$NONE_PLAN" "test-loom-daemon-stop.sh")" == "RUN" ]] && echo 0 || echo 1)" \
    "candidates=none: nothing is detected, so the daemon suites RUN (even on a host with a live daemon)"

# ---------- 8. the DERIVED candidate list mirrors find_repo_root's `.git`-file
#               walk (#6420) ------------------------------------------------
# The lifecycle scripts resolve their state home with find_repo_root(), whose
# walk has TWO branches: a `.loom` directory, and a `.git` FILE (a linked
# worktree) which resolves to the MAIN checkout's root. The guard only ever
# considered $SCRIPT_DIR/../../.., $HOME/.loom and the env tiers — so in a
# consumer repo whose worktrees carry no tracked `.loom/`, a suite launched
# from a worktree would resolve the MAIN checkout's live pid file while the
# guard's candidate list did not include it, and the guard would plan RUN for
# all five host-mutating suites.
#
# Driven through `--print-candidates` (the derived list, printed and nothing
# else) rather than --plan, so these cases assert the RESOLUTION and stay
# independent of whether this host happens to run a daemon.
WT_MAIN="$WORKDIR/consumer/main"
WT_LINKED="$WORKDIR/consumer/wt/issue-1"
mkdir -p "$WT_MAIN/.loom" "$WT_MAIN/.git/worktrees/issue-1" "$WT_LINKED" "$WORKDIR/consumer/home"
# A worktree with NO tracked `.loom/` — the consumer-repo shape. Its `.git` is a
# FILE naming the main checkout's per-worktree gitdir, exactly as git writes it.
echo "gitdir: $WT_MAIN/.git/worktrees/issue-1" > "$WT_LINKED/.git"

print_candidates_from() { # <cwd> [home]
    ( cd "$1" && env HOME="${2:-$WORKDIR/consumer/home}" \
        LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
        bash "$RUNNER" --print-candidates 2>/dev/null )
}

WT_CANDIDATES="$( print_candidates_from "$WT_LINKED" )"
check "$([[ "$WT_CANDIDATES" == *"$WT_MAIN/.loom/.daemon.pid"* ]] && echo 0 || echo 1)" \
    "worktree cwd: the main checkout's pid file is a candidate via the .git-file walk (#6420)" \
    "$WT_CANDIDATES"

# Control 1: the existing tiers are untouched — the machine-level state home is
# still enumerated alongside the new one.
check "$([[ "$WT_CANDIDATES" == *"$WORKDIR/consumer/home/.loom/.daemon.pid"* ]] && echo 0 || echo 1)" \
    "worktree cwd: the machine-level \$HOME/.loom tier is still enumerated" "$WT_CANDIDATES"

# Control 2: the branch is gated on the resolved main checkout actually being a
# Loom workspace, exactly as find_repo_root() gates it. A `.git` file pointing
# at a repo with no `.loom/` contributes nothing.
NL_MAIN="$WORKDIR/nonloom/main"
NL_LINKED="$WORKDIR/nonloom/wt/issue-1"
mkdir -p "$NL_MAIN/.git/worktrees/issue-1" "$NL_LINKED"
echo "gitdir: $NL_MAIN/.git/worktrees/issue-1" > "$NL_LINKED/.git"
NL_CANDIDATES="$( print_candidates_from "$NL_LINKED" )"
check "$([[ "$NL_CANDIDATES" != *"$NL_MAIN/.loom/.daemon.pid"* ]] && echo 0 || echo 1)" \
    "worktree cwd: a .git file resolving to a non-Loom checkout adds no candidate (#6420)" \
    "$NL_CANDIDATES"

# Control 3: --print-candidates executes no suite and honors the seam, so it is
# usable as a diagnostic on a live host.
SEAM_CANDIDATES="$( LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none bash "$RUNNER" --print-candidates 2>/dev/null )"
check "$([[ -z "$SEAM_CANDIDATES" ]] && echo 0 || echo 1)" \
    "--print-candidates honors the candidates=none seam (prints nothing)" "$SEAM_CANDIDATES"

# The guard must ACT on the new tier, not merely print it: a live-looking pid
# file in the main checkout skips the daemon suites when the cwd is the
# `.loom`-less worktree.
echo "$$" > "$WT_MAIN/.loom/.daemon.pid"
WT_PLAN="$( cd "$WT_LINKED" && env HOME="$WORKDIR/consumer/home" \
    LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
    bash "$RUNNER" --plan 2>/dev/null )"
check "$([[ "$(plan_line "$WT_PLAN" "test-loom-daemon-stop.sh")" == "SKIP" ]] && echo 0 || echo 1)" \
    "worktree cwd: a live pid file in the MAIN checkout skips the daemon suites (#6420)" \
    "$WT_PLAN"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
