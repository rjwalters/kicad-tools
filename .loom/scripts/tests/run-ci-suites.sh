#!/usr/bin/env bash
# run-ci-suites.sh — run the CI-wired shell test suites for
# defaults/scripts/tests/ (issue #4455).
#
# Runs every suite listed in ci-wired.txt CONCURRENTLY (issue #6622), each
# suite's stdout/stderr captured to its own log file exactly as before, and
# prints the pass/fail/skip report in MANIFEST order after every suite has
# finished — identical ordering and totals format to the old sequential run,
# so log diffs and the wired/excluded manifest invariant check (#4455) are
# unaffected by which suite happened to finish first. Exits non-zero if any
# wired suite fails. The wired/excluded partition invariant is enforced first
# via check-ci-suite-manifest.sh, so a fresh unlisted suite is a hard failure
# here (it cannot silently slip into an unwired pool).
#
# ## Concurrency (#6622)
#
# Suites are hermetic by construction (this job's own name) — each already
# gets its own isolated log file and its own per-suite timeout, which is what
# makes concurrent dispatch tractable without touching that machinery. Only
# the scheduling loop and the final report changed: suites are dispatched to
# a bounded worker pool (default: one worker per logical core, see
# LOOM_CI_PARALLELISM below), each writes its outcome (exit code + duration)
# to its own result file, and once every dispatched suite has completed the
# report walks the manifest array — NOT completion order — printing each
# suite's recorded outcome. The live-daemon guard (#6386) still runs
# per-suite BEFORE that suite is dispatched (not batched after the fact), so
# a guarded suite is never launched even speculatively.
#
# ## Serial lane (#6622 AC5)
#
# "Hermetic by construction" is an invariant to enforce, not an assumption to
# rely on: a suite that turns out NOT to be hermetic under concurrency is
# pinned to SERIAL_LANE_SUITES below, which runs it alone after the parallel
# pool has fully drained. See that list for the current occupants, the
# evidence that put them there, and the cost of adding one.
#
# Usage:
#   run-ci-suites.sh                 # run the whole wired set (concurrently)
#   run-ci-suites.sh --plan          # print the RUN/SKIP plan and exit (runs nothing)
#   run-ci-suites.sh --print-candidates
#                                    # print the live-daemon guard's derived pid-file
#                                    # candidate list and exit (runs nothing)
#   LOOM_CI_SUITE_TIMEOUT=180 …      # per-suite timeout in seconds (default 1200)
#   LOOM_CI_FAIL_EXCERPT_MAX=20 …    # failure-excerpt knobs — see
#   LOOM_CI_FAIL_CONTEXT_LINES=3 …     defaults/scripts/lib/ci-suite-excerpt.sh
#   LOOM_CI_FAIL_TAIL_LINES=40 …       (#6662)
#   LOOM_CI_PARALLELISM=4 …          # concurrent suites (default: logical core count)
#   LOOM_CI_SERIAL_SUITES='a.sh b.sh'
#                                    # override the serial lane (empty disables it)
#   LOOM_CI_RETRY_LOG=<path>         # durable retry record location (default:
#                                      /tmp/ci-suite-retry-log.tsv, see below)
#   LOOM_CI_LIVE_LEAK_GUARD=warn     # downgrade the #8077 live-host leak guard
#                                      from a hard failure to a warning
#   LOOM_TEST_ALLOW_SYSTEMD=1        # (read by individual suites, not by this
#                                      script) opt in to blocks that drive the
#                                      LIVE `systemctl --user` manager — safe
#                                      only on a host with no production daemon
#
# ## Live-host leak guard (#8077)
#
# The #6386 guard below decides whether a host-mutating suite RUNS. This one
# checks, after the fact, whether the run that did happen moved live host
# state: the boot-block count of every reachable `daemon.log`, the supervised
# unit's restart identity, the systemd --user unit-file name set, the shared
# token pool. It exists because #6386's per-suite skip list is a whitelist of
# KNOWN-dangerous suites, and #8077 was an unlisted one — the sweep environment
# inherits the production daemon's own `LOOM_SOCKET_PATH`, so ANY suite that
# spawns a real daemon without an explicit override resolves the live
# `~/.loom/daemon.log`, whatever else it sandboxes. A whitelist cannot see
# that; a before/after fingerprint of the damage surface can.
#
# ## Retry-once-and-record (#7791)
#
# A single flaky assertion anywhere among 223 suites fails the whole job,
# which blocks every concurrent PR — not just the one that happened to run.
# #7287 and its 2026-09-16 recurrence (#7789/#7791) established this as a
# fleet-wide tax, not a one-off. So: a suite that fails its first attempt is
# re-run exactly ONCE (never the whole job — that would re-roll every other
# suite's dice for no reason). A suite that fails BOTH attempts still fails
# the job exactly as before.
#
# The retry is never silent. A quiet retry would turn a visible flake into an
# invisible one — the same fail-open pattern #7745/#7761/#7755/#7743 already
# burned this repo on — so every retry is recorded twice:
#   1. In the human-readable report below, as "PASS (retried once, first
#      exit N)" — visibly distinct from a plain "PASS" so a zero-retry run
#      can never be confused with one that stepped over a flake.
#   2. In a durable, machine-readable record (LOOM_CI_RETRY_LOG, default
#      /tmp/ci-suite-retry-log.tsv) naming every retried suite, its first and
#      retry exit codes, and its outcome — plus a $GITHUB_STEP_SUMMARY entry
#      when running under Actions. This is the data #7789 needs to decide
#      which suites are worth quarantining, rather than arguing from a
#      hand-categorization of issue titles. Cross-run quarantine tracking
#      (a rolling per-suite counter) is an explicit fast-follow, not this
#      record's job — it needs state that outlives one CI run.
#
# ## Live-daemon guard (#6386)
#
# The daemon-lifecycle suites (LIVE_DAEMON_GUARDED_SUITES below) execute the
# REAL loom-daemon-{start,stop,update,quiesce}.sh and loom-daemon-watchdog.sh —
# they `kill`, `rm -f` pid files, and `launchctl bootout` / `systemctl --user
# disable` whatever their invocations resolve. Every one of them sandboxes
# itself, but that is a per-case property: it takes exactly ONE case that
# forgets one pin (or one script that ignores one of them) to reach a live
# daemon. That is not hypothetical — #6386 was an Auditor running THIS script
# from a fleet host's live checkout, whose stop suite SIGTERM'd the fleet's
# authoritative dispatcher and left it down for 11 hours.
#
# So: when a daemon pid file exists on this host, those suites are SKIPPED
# (loudly, and reported in the summary), not run. CI runners have no daemon and
# no pid file, so the wired set is unaffected there — this only fires on the
# hosts where the blast radius is real. A skip is NOT a failure: the run still
# exits 0 if everything else passes, and the summary names what went
# unvalidated so the gap is never invisible.
#
# The trigger is EXISTENCE, not liveness: the lifecycle suites `rm -f`
# whichever pid file they resolve, so even a stale one is host state they must
# not silently delete (it is also the operator's evidence of how the daemon
# last exited).
#
#   LOOM_CI_ALLOW_DAEMON_SUITES=1    run them anyway (deliberate operator override)
#   LOOM_CI_DAEMON_PIDFILE_CANDIDATES=<path>[:<path>…]|none
#                                    TEST-ONLY seam: replace the derived
#                                    candidate list (`none` = no candidates at
#                                    all), so the guard's own regression suite
#                                    can assert BOTH directions regardless of
#                                    whether its host runs a real daemon. See
#                                    test-run-ci-suites-daemon-guard.sh.
#
# A manifest entry containing a `/` is a suite outside this directory,
# resolved relative to the repo root (tests/hooks/…, #4769; defaults/hooks/
# tests/…, #4451) rather than SCRIPT_DIR; its log file name has the `/`
# replaced with `_` so it stays a flat /tmp path. The default per-suite
# timeout was raised from 120s to 1200s in #4769 to cover
# tests/hooks/test-guard-destructive.sh (531 assertions, observed up to ~14
# min / 850s wall-clock on a loaded dev machine — still hermetic, just large
# — so the ceiling keeps real headroom above that peak).
#
# Exit 0 = all wired suites passed; 1 = one or more failed / manifest invalid.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WIRED_MANIFEST="$SCRIPT_DIR/ci-wired.txt"
PER_SUITE_TIMEOUT="${LOOM_CI_SUITE_TIMEOUT:-1200}"

PLAN_ONLY=false
PRINT_CANDIDATES=false
for arg in "$@"; do
    case "$arg" in
        --plan) PLAN_ONLY=true ;;
        --print-candidates) PRINT_CANDIDATES=true ;;
        *) echo "run-ci-suites.sh: unknown option '$arg'" >&2; exit 1 ;;
    esac
done

# ---------- live-daemon guard (#6386) ----------
# Host-mutating suites: each one drives the real daemon lifecycle scripts.
#
# test-loom-daemon-watchdog.sh is deliberately still listed although #8086 moved
# it to ci-excluded.txt (it needs a built loom-daemon; see that file). The entry
# is inert while the suite is unwired -- this is a name filter over the wired
# set -- and it must stay: the suite is no less host-mutating than before, so a
# future re-wiring, or a run against a hand-edited manifest, has to land inside
# the guard rather than outside it. test-run-ci-suites-daemon-guard.sh asserts
# this entry's continued presence directly.
LIVE_DAEMON_GUARDED_SUITES="test-loom-daemon-start.sh test-loom-daemon-stop.sh test-loom-daemon-update.sh test-loom-daemon-quiesce.sh test-loom-daemon-watchdog.sh"

# ---------- serial lane (#6622 AC5, evidence in #6639) ----------
# Suites that are demonstrably NOT hermetic under concurrency run alone, after
# the parallel pool has fully drained. This is the escape hatch #6622's AC5
# names explicitly ("any suite that turns out not to be hermetic under
# concurrency gets fixed or explicitly pinned to a serial lane") — it is a
# quarantine list, not a general-purpose knob: adding a suite here costs its
# full wall-clock time on the critical path, so it must be justified by an
# observed concurrent-only failure, and removed once the suite is fixed.
#
# Current occupants:
#   test-loom-daemon-update.sh — failed on 2 of the 4 concurrent CI runs of
#     PR #6639 while passing every sequential run on main, and passing locally
#     both standalone and pinned to 2 oversubscribed cores. Observed failures
#     were a different assertion each time (once 2 unnamed, once test 64's
#     `--help documents --drain / --timeout / --force-after-timeout /
#     --restart-now` — an assertion with no timing component at all, which
#     rules out simple CPU-contention slowness and points at cross-suite
#     interference not yet root-caused). Tracked for a real fix rather than
#     left as a permanent pin.
#   test-loom-daemon-start.sh (#7391) — the "autonomy downgrade (plist): marker
#     present + no readable prior value still warns" case (AD7) failed
#     intermittently on main and on unrelated PRs (a docs-only commit, a
#     Rust-only PR touching no shell scripts), always with the SAME truncated
#     captured output: the invocation's stderr ends right after the
#     unconditional "Reliability daemon: work_finder=off main_health_gate=off"
#     line, with NEITHER `check_autonomy_downgrade_key()` WARNING ever printed
#     for either autonomy key. Investigation ruled out every candidate IN this
#     script:
#       - guard_session_context_start() (#6568) does not fire here at all —
#         AD7 sets LOOM_LAUNCHD_LABEL explicitly, which trips that guard's own
#         `[[ -n "${LOOM_LAUNCHD_LABEL:-}" ]] && return 0` exemption before it
#         reaches any warning/refusal branch (confirmed by reading the guard's
#         mechanism-detection + exemption checks directly).
#       - check_autonomy_downgrade_key()'s own logic is fully deterministic
#         and side-effect-free for this case: the marker path is set via an
#         explicit `LOOM_AUTONOMY_MARKER=` override (never the SOCKET_PATH-
#         derived fallback), the prior-plist path never exists (a fresh
#         mktemp HOME), and neither `--from-config` nor a pre-exported
#         WORK_FINDER/HEALTH_GATE value is in play — every branch that could
#         suppress the warning requires a flag/env this case does not set.
#       - Reproduction attempts failed to trip it under real stress: 480+
#         concurrent invocations of the isolated check (8-way parallel on this
#         host), 150 further invocations inside a CPU-throttled
#         (`--cpus=1`) Ubuntu container, 8 consecutive full-suite runs inside a
#         `--cpus=2` Ubuntu container (which DID reproduce two OTHER,
#         environment-specific flakes every single time — a missing
#         `systemd-inhibit` binary and a background-fork timing case — but
#         never AD7), and 20 consecutive full standalone suite runs on this
#         host (AC2). This is the same "passes locally both standalone and
#         pinned to oversubscribed cores" signature the update.sh entry above
#         documents.
#     Conclusion: not a logic defect in this suite's own autonomy-downgrade
#     detection or in the #6568 guard — the failure requires the real CI
#     job's full concurrent-suite load (this suite is itself one of five
#     LIVE_DAEMON_GUARDED_SUITES, several of which fork many short-lived
#     subprocesses of their own) to manifest, consistent with the same
#     unresolved cross-suite interference class as test-loom-daemon-update.sh
#     above rather than a second, independent bug. Tracked for a real fix
#     (most likely: capture and assert the exit status of AD7's `$(...)`
#     invocation, so a subprocess that is killed/starved under load fails
#     loudly as "subprocess did not complete" instead of silently as a
#     content mismatch) rather than left as a permanent pin.
#
# BOTH occupants are currently UNWIRED (#8087): cli/loom-daemon-start.sh is now
# a thin stub over `loom-daemon daemon-start`, so test-loom-daemon-start.sh and
# test-loom-daemon-update.sh (which reaches the same script through
# lib/daemon-update-fixtures.sh) need a built binary and moved to
# ci-excluded.txt, wired in ci.yml's "Native Port Suites" job instead. That
# job's steps are sequential, so they get the isolation this lane was giving
# them by construction rather than by quarantine list.
#
# They stay NAMED here on purpose. The lane filter simply never matches a suite
# that is not in ci-wired.txt, so the two names cost nothing today — and if
# either suite is ever re-wired into this runner's concurrent pool, it lands
# already pinned rather than silently rejoining the pool the #6639/#7391 flakes
# were observed in. test-run-ci-suites-serial-lane.sh asserts that retention
# directly (named here, absent from --plan) and exercises the lane MECHANISM
# through the LOOM_CI_SERIAL_SUITES seam below, the same
# "asserted-differently-not-less" split #8086 introduced for the live-daemon
# guard's own literal.
#
# LOOM_CI_SERIAL_SUITES overrides the list (space-separated basenames); an
# empty value disables the lane entirely. It exists as a test seam for
# test-run-ci-suites-serial-lane.sh and as an operator escape hatch.
SERIAL_LANE_SUITES="${LOOM_CI_SERIAL_SUITES-test-loom-daemon-update.sh test-loom-daemon-start.sh}"

# guard_repo_root_from / live_daemon_pidfile_candidates / live_daemon_pidfiles_present
# — extracted to a shared lib (#6528) so nextest-daemon-guard.sh (the
# equivalent guard for the Rust `daemon-integration` nextest group) reuses the
# exact same pid-file detection instead of a second, driftable copy. See
# defaults/scripts/lib/live-daemon-guard.sh for the full function docs.
# shellcheck source=../lib/live-daemon-guard.sh
source "$REPO_ROOT/defaults/scripts/lib/live-daemon-guard.sh"

# live_host_leak_snapshot / live_host_leak_assert_unchanged (#8077) — the
# whole-run guard that this script wraps around every suite it dispatches. The
# #6386 guard above answers "should this suite run here?"; this one answers
# "did the run that DID happen touch the live host?", which is the question
# #8077 went unanswered on: the sweep environment inherits the production
# daemon's own LOOM_SOCKET_PATH, so a suite that merely OMITS an override gets
# the live `~/.loom/daemon.log` as its "default" rather than a neutral one.
# Only the #8077 pair is used, NOT live_state_sandbox_snapshot: the state-path
# pair fingerprints `.daemon.pid`, which a legitimate auto_update daemon roll
# rewrites — fine inside one short suite, a guaranteed flake across a full
# 20-minute run. See that file's "#8077 live-host leak guard" header section.
#
# Sourced fail-CLOSED. This script runs `set -uo pipefail` WITHOUT `-e`, so an
# unreadable `source` only prints and continues — which would leave the guard's
# functions undefined and its verdict indistinguishable from a real leak. A
# missing guard must stop the run, not be silently absent (#7745/#7761's
# fail-open pattern).
if [[ ! -r "$SCRIPT_DIR/lib/live-state-sandbox.sh" ]]; then
    echo "::error::run-ci-suites.sh: lib/live-state-sandbox.sh is missing — the #8077 live-host leak guard cannot run" >&2
    exit 1
fi
# shellcheck source=lib/live-state-sandbox.sh
source "$SCRIPT_DIR/lib/live-state-sandbox.sh"

# print_suite_failure_excerpt — the failure excerpt printed for every failing
# suite below. Extracted (#6662) so the excerpt's shape is testable against a
# synthetic log without executing a real CI suite run; see that file for why
# a bare trailing window was not enough.
# shellcheck source=../lib/ci-suite-excerpt.sh
source "$REPO_ROOT/defaults/scripts/lib/ci-suite-excerpt.sh"
# loom_cpu_total_cores() — portable logical-core detection (nproc ->
# getconf -> sysctl -> 1), already shared with spawn-claude.sh's CPU-quota
# math (#5111/#5979). Reused here for the default suite-concurrency budget
# rather than a second nproc/sysctl fallback ladder.
# shellcheck source=../lib/cpu-budget.sh
source "$REPO_ROOT/defaults/scripts/lib/cpu-budget.sh"

# --print-candidates: the derived candidate list and nothing else. The guard's
# resolution is otherwise only observable through its RUN/SKIP decision, which
# depends on whether the host running the test happens to have a daemon — this
# seam lets the guard's own regression suite (and an operator debugging a
# surprising skip) assert the resolution directly. Runs no suite.
if [[ "$PRINT_CANDIDATES" == "true" ]]; then
    live_daemon_pidfile_candidates | awk 'NF' | sort -u
    exit 0
fi

LIVE_DAEMON_EVIDENCE="$(live_daemon_pidfiles_present)"
SKIP_DAEMON_SUITES=false
if [[ -n "$LIVE_DAEMON_EVIDENCE" ]]; then
    if [[ "${LOOM_CI_ALLOW_DAEMON_SUITES:-}" =~ ^(1|true|yes|on)$ ]]; then
        echo "!!! LOOM_CI_ALLOW_DAEMON_SUITES is set — running the daemon-lifecycle suites anyway" >&2
        echo "$LIVE_DAEMON_EVIDENCE" | sed 's/^/      /' >&2
    else
        SKIP_DAEMON_SUITES=true
    fi
fi

# 1) Fail fast if the manifest partition invariant is broken.
if ! bash "$SCRIPT_DIR/check-ci-suite-manifest.sh"; then
    echo "::error::CI-suite manifest invariant failed — fix ci-wired.txt / ci-excluded.txt" >&2
    exit 1
fi

# Returns 0 when <suite> must be skipped because this host has a daemon pid file.
suite_is_daemon_guarded() {
    local candidate name="${1##*/}"
    [[ "$SKIP_DAEMON_SUITES" == "true" ]] || return 1
    for candidate in $LIVE_DAEMON_GUARDED_SUITES; do
        [[ "$name" == "$candidate" ]] && return 0
    done
    return 1
}

# Returns 0 when <suite> is pinned to the serial lane (#6622 AC5). Independent
# of the live-daemon guard above: a suite can be both, and the guard wins (a
# guarded suite is never run at all, serial lane or not).
suite_is_serial_lane() {
    local candidate name="${1##*/}"
    [[ -n "$SERIAL_LANE_SUITES" ]] || return 1
    for candidate in $SERIAL_LANE_SUITES; do
        [[ "$name" == "$candidate" ]] && return 0
    done
    return 1
}

if [[ "$SKIP_DAEMON_SUITES" == "true" ]]; then
    {
        echo
        echo "############################################################"
        echo "!!! LIVE DAEMON DETECTED ON THIS HOST — daemon-lifecycle suites SKIPPED (#6386)"
        echo "$LIVE_DAEMON_EVIDENCE" | sed 's/^/      /'
        echo "    Skipping: $LIVE_DAEMON_GUARDED_SUITES"
        echo "    These suites run the REAL loom-daemon-{start,stop,update,quiesce}.sh and can"
        echo "    kill/bootout a live daemon if any single case's sandbox is incomplete — that is"
        echo "    #6386 (an 11h fleet-dispatcher outage caused by exactly this script's Auditor run)."
        echo "    Run them on a host with no daemon, or override with LOOM_CI_ALLOW_DAEMON_SUITES=1."
        echo "############################################################"
        echo
    } >&2
fi

# Resolve a GNU/BSD-agnostic timeout wrapper (optional — plain bash if absent).
timeout_cmd=""
if command -v timeout >/dev/null 2>&1; then
    timeout_cmd="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
    timeout_cmd="gtimeout"
fi

# `mapfile` is bash 4+; macOS ships 3.2 and this runs on developer machines
# too (#7751). Empty lines are already filtered by the awk NF test.
suites=()
while IFS= read -r _suite; do
    suites+=("$_suite")
done < <(sed -E 's/#.*$//' "$WIRED_MANIFEST" | awk 'NF { print $1 }')

passed=0
failed=0
skipped=0
failed_names=()
skipped_names=()
total_start=$(date +%s)

# --plan: report what WOULD run (and what the live-daemon guard removed), then
# exit without executing a single suite. This is the seam the guard's own
# regression test drives — asserting the skip decision without paying for (or
# risking) a full suite run.
if [[ "$PLAN_ONLY" == "true" ]]; then
    for suite in "${suites[@]}"; do
        if suite_is_daemon_guarded "$suite"; then
            printf 'SKIP  %s (live-daemon guard, #6386)\n' "$suite"
        elif suite_is_serial_lane "$suite"; then
            # Still RUN in field 1 — the serial lane changes WHEN a suite runs,
            # never WHETHER it runs, and the guard's own regression suite reads
            # that field as the verdict.
            printf 'RUN   %s (serial lane, #6622)\n' "$suite"
        else
            printf 'RUN   %s\n' "$suite"
        fi
    done
    exit 0
fi

# Live-host leak guard (#8077): fingerprint the surfaces a test run must never
# move — every reachable `daemon.log`'s boot-block count, the supervised unit's
# restart identity, the systemd --user unit-file name set, the shared token
# pool — BEFORE the first suite is dispatched. Taken here, after the --plan /
# --print-candidates early exits, so a dry run pays nothing.
live_host_leak_snapshot

# PARALLELISM: how many suites run at once. Default is the host's logical
# core count (nproc — the issue's own default) via the shared cpu-budget.sh
# helper; LOOM_CI_PARALLELISM overrides it for local tuning (e.g. throttling
# on a laptop, or forcing 1 to reproduce the old fully-sequential behavior).
PARALLELISM="${LOOM_CI_PARALLELISM:-}"
if ! [[ "$PARALLELISM" =~ ^[0-9]+$ ]] || [[ "$PARALLELISM" -lt 1 ]]; then
    PARALLELISM="$(loom_cpu_total_cores)"
fi

# Per-suite results are written here (one <log_name>.result file per
# dispatched suite: "<rc> <duration_seconds>", or "MISSING 0" for a manifest
# entry whose file does not exist) so the background workers below can hand
# their outcome back to this process — a subshell's local vars vanish when it
# exits, but a file survives. Suite LOGS keep their existing /tmp/ci-suite-*
# path (unaffected — those are already inspected after a CI failure); only
# this small bookkeeping directory is new, and it is removed on exit.
RESULTS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ci-suite-results.XXXXXX")"
trap 'rm -rf "$RESULTS_DIR"' EXIT

# run_suite <suite> — executes one suite (log file + per-suite timeout
# unchanged from the old sequential loop) and records its outcome. Runs in a
# background subshell (see the dispatch loop below), so it must not rely on
# anything surviving past its own exit other than the result file it writes.
#
# Retry-once-and-record (#7791): a non-zero first attempt is re-run exactly
# once, under the SAME per-suite timeout (a suite that times out is not a
# different case — it is just another non-zero exit, retried the same way).
# The result file now carries 5 space-separated fields instead of 2:
#   <final_rc> <duration_seconds> <retried:0|1> <first_rc> <retry_rc>
# where <final_rc> is the retry's exit code when retried=1, else the first
# attempt's; <retry_rc> is "-" when retried=0 (no retry happened). This is
# the cross-process handoff the parallel-pool dispatch loop below relies on
# (a background subshell's locals vanish when it exits), and the serial
# lane's foreground loop and the daemon-guard's skip path both funnel through
# this same function, so the retry logic applies uniformly across all three
# dispatch paths without separate handling.
run_suite() {
    local suite="$1" path log_name start dur rc first_rc retried retry_rc
    if [[ "$suite" == */* ]]; then
        path="$REPO_ROOT/$suite"
    else
        path="$SCRIPT_DIR/$suite"
    fi
    log_name="${suite//\//_}"
    if [[ ! -f "$path" ]]; then
        printf 'MISSING 0 0 - -\n' >"$RESULTS_DIR/$log_name.result"
        return 0
    fi
    start=$(date +%s)
    if [[ -n "$timeout_cmd" ]]; then
        "$timeout_cmd" "$PER_SUITE_TIMEOUT" bash "$path" >"/tmp/ci-suite-$log_name.log" 2>&1
    else
        bash "$path" >"/tmp/ci-suite-$log_name.log" 2>&1
    fi
    rc=$?
    first_rc="$rc"
    retried=0
    retry_rc="-"
    if [[ "$rc" -ne 0 ]]; then
        retried=1
        {
            echo
            echo "=== RETRY (#7791): attempt 1 failed with exit $first_rc — re-running once ==="
        } >>"/tmp/ci-suite-$log_name.log"
        if [[ -n "$timeout_cmd" ]]; then
            "$timeout_cmd" "$PER_SUITE_TIMEOUT" bash "$path" >>"/tmp/ci-suite-$log_name.log" 2>&1
        else
            bash "$path" >>"/tmp/ci-suite-$log_name.log" 2>&1
        fi
        retry_rc=$?
        rc="$retry_rc"
    fi
    dur=$(( $(date +%s) - start ))
    printf '%s %s %s %s %s\n' "$rc" "$dur" "$retried" "$first_rc" "$retry_rc" >"$RESULTS_DIR/$log_name.result"
}

printf '\n=== Running %d CI-wired shell suites (parallelism %d, timeout %ss each) ===\n\n' \
    "${#suites[@]}" "$PARALLELISM" "$PER_SUITE_TIMEOUT"
if [[ -n "$SERIAL_LANE_SUITES" ]]; then
    printf 'Serial lane (run alone after the pool drains, #6622 AC5): %s\n\n' \
        "$SERIAL_LANE_SUITES"
fi

# Dispatch pass: launch every non-guarded, non-serial-lane suite in the
# background, bounded to $PARALLELISM concurrent jobs via `wait -n`. The
# live-daemon guard decision is made HERE, synchronously, per suite, before
# that suite is ever dispatched — never batched or revisited after the fact
# (#6386's hazard is a suite actually starting, not how the report is
# printed).
_RUN_PIDS=()
running=0
for suite in "${suites[@]}"; do
    if suite_is_daemon_guarded "$suite"; then
        continue
    fi
    if suite_is_serial_lane "$suite"; then
        continue
    fi
    run_suite "$suite" &
    # `wait -n` is bash 4.3+. Stock macOS ships 3.2, where it fails with
    # "wait: -n: invalid option" -- and this script is `set -uo pipefail`
    # WITHOUT `-e`, so it did not abort: `running` decremented anyway and the
    # parallelism bound silently stopped bounding (#7802 Class 1).
    #
    # Waiting on the OLDEST pid instead of any pid needs no new machinery and
    # preserves the bound exactly, which is the contract that matters here. A
    # slow oldest job can hold a slot a little longer than `wait -n` would; that
    # is a scheduling nuance, not a correctness one, and it is a far better
    # trade than hand-rolling job-polling in shell.
    _RUN_PIDS+=($!)
    running=$((running + 1))
    if [[ "$running" -ge "$PARALLELISM" ]]; then
        wait "${_RUN_PIDS[0]}" 2>/dev/null || true
        # Quoted: SC2206. Safe under `set -u` on bash 3.2 because an array
        # SLICE of an empty/exhausted array expands to nothing rather than
        # tripping the unbound-variable error that bare "${arr[@]}" does there.
        _RUN_PIDS=("${_RUN_PIDS[@]:1}")
        running=$((running - 1))
    fi
done
wait

# Serial lane: the pool has fully drained (the `wait` above is unconditional),
# so these suites run one at a time with nothing else executing — the
# concurrency-quarantine escape hatch #6622's AC5 calls for. Run in the
# FOREGROUND, not backgrounded-then-waited, so two serial-lane suites can
# never overlap each other either.
for suite in "${suites[@]}"; do
    suite_is_serial_lane "$suite" || continue
    suite_is_daemon_guarded "$suite" && continue
    run_suite "$suite"
done

# Report pass: walk the manifest IN ORDER (not completion order) so the
# printed report — and its ordering/totals format — is identical to the old
# sequential run regardless of which suite happened to finish first.
#
# retried_records accumulates one tab-separated "<suite>\t<first_rc>\t<retry_rc>\t<outcome>"
# entry per RETRIED suite (#7791), consumed once below — after every suite's
# outcome is known — to emit the durable record. It is built here (in-memory,
# during the loop) but WRITTEN only once, after the loop, so the artifact is a
# single well-formed record rather than N interleaved fragments.
retried_records=()
for suite in "${suites[@]}"; do
    if suite_is_daemon_guarded "$suite"; then
        printf 'SKIP  %-52s     (live-daemon guard, #6386)\n' "$suite"
        skipped=$((skipped + 1)); skipped_names+=("$suite"); continue
    fi
    log_name="${suite//\//_}"
    result_file="$RESULTS_DIR/$log_name.result"
    if [[ ! -f "$result_file" ]]; then
        # Should not happen (every dispatched suite writes its result before
        # `wait` returns) — treated as a loud failure rather than silently
        # dropped from the report.
        printf 'FAIL  %-52s     (no result recorded)\n' "$suite"
        failed=$((failed + 1)); failed_names+=("$suite"); continue
    fi
    read -r rc dur retried first_rc retry_rc <"$result_file"
    if [[ "$rc" == "MISSING" ]]; then
        echo "FAIL  $suite (missing file)"
        failed=$((failed + 1)); failed_names+=("$suite"); continue
    fi
    if [[ "$rc" -eq 0 ]]; then
        if [[ "$retried" == "1" ]]; then
            printf 'PASS  %-52s %3ss (retried once — first exit %s, #7791)\n' \
                "$suite" "$dur" "$first_rc"
            retried_records+=("$suite"$'\t'"$first_rc"$'\t'"$retry_rc"$'\t'"PASS")
        else
            printf 'PASS  %-52s %3ss\n' "$suite" "$dur"
        fi
        passed=$((passed + 1))
    else
        if [[ "$retried" == "1" ]]; then
            printf 'FAIL  %-52s %3ss (failed both attempts — first exit %s, retry exit %s)\n' \
                "$suite" "$dur" "$first_rc" "$retry_rc"
            retried_records+=("$suite"$'\t'"$first_rc"$'\t'"$retry_rc"$'\t'"FAIL")
        else
            printf 'FAIL  %-52s %3ss (exit %s)\n' "$suite" "$dur" "$rc"
        fi
        failed=$((failed + 1)); failed_names+=("$suite")
        print_suite_failure_excerpt "$suite" "/tmp/ci-suite-$log_name.log"
    fi
done

total_dur=$(( $(date +%s) - total_start ))
printf '\n=== Summary: %d passed, %d failed, %d skipped of %d wired suites in %ss ===\n' \
    "$passed" "$failed" "$skipped" "${#suites[@]}" "$total_dur"

if [[ "$skipped" -ne 0 ]]; then
    printf 'Skipped (live-daemon guard, #6386 — NOT validated on this host): %s\n' \
        "${skipped_names[*]}" >&2
fi

# ---------- durable retry record (#7791) ----------
# Emitted ONCE, here, after every suite's outcome is known — never per-suite
# mid-run, so this is one well-formed record rather than N interleaved lines
# from concurrent workers. A silent retry would turn a visible flake into an
# invisible one (the same fail-open pattern #7745/#7761/#7755/#7743 already
# cost this repo), so a run with zero retries must be distinguishable — in
# both the printed report above AND this file — from one with several.
#
# This is a SINGLE-RUN record. Cross-run quarantine tracking ("retried more
# than N times across a rolling window") needs state that outlives one CI
# run and is an explicit fast-follow (#7789), not this record's job.
RETRY_LOG_FILE="${LOOM_CI_RETRY_LOG:-/tmp/ci-suite-retry-log.tsv}"
RETRY_RUN_ID="${GITHUB_RUN_ID:-local}"
RETRY_BRANCH="${GITHUB_REF_NAME:-$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}"
{
    printf '# CI suite retry log (#7791) — run_id=%s branch=%s retried_count=%d\n' \
        "$RETRY_RUN_ID" "$RETRY_BRANCH" "${#retried_records[@]}"
    printf '# suite\tfirst_exit\tretry_exit\toutcome\n'
    for rec in ${retried_records[@]+"${retried_records[@]}"}; do
        printf '%s\n' "$rec"
    done
} >"$RETRY_LOG_FILE"

if [[ "${#retried_records[@]}" -gt 0 ]]; then
    printf '\nRetried suites (%d, see %s):\n' "${#retried_records[@]}" "$RETRY_LOG_FILE"
    for rec in "${retried_records[@]}"; do
        printf '  %s\n' "$rec" | tr '\t' ' '
    done
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
        echo "### CI suite retries (#7791)"
        echo
        if [[ "${#retried_records[@]}" -eq 0 ]]; then
            echo "No suite required a retry this run."
        else
            echo "| suite | first exit | retry exit | outcome |"
            echo "|---|---|---|---|"
            for rec in "${retried_records[@]}"; do
                IFS=$'\t' read -r rec_suite rec_first rec_retry rec_outcome <<<"$rec"
                echo "| $rec_suite | $rec_first | $rec_retry | $rec_outcome |"
            done
        fi
    } >>"$GITHUB_STEP_SUMMARY"
fi

# ---------- live-host leak guard, second half (#8077) ----------
# Re-fingerprint what live_host_leak_snapshot recorded. A clean run says so
# explicitly (naming the surface count), because a guard whose only output is
# silence is indistinguishable from one that never ran — the same reasoning the
# retry record above is built on. A leak is a HARD failure by default: #8077
# was found by an operator reading a fleet-check digest 15 hours later, which
# is exactly the discovery latency a gate exists to remove.
if live_host_leak_assert_unchanged; then
    printf '\nLive-host leak guard: clean (%d surface(s) checked, #8077)\n' \
        "$(live_host_leak_snapshot_size)"
else
    {
        echo
        echo "############################################################"
        echo "!!! A SUITE IN THIS RUN TOUCHED LIVE HOST STATE (#8077)"
        echo "    The offending surface(s) are named above. This is the class of leak that"
        echo "    wrote 17 daemon boot blocks into a fleet worker's PRODUCTION ~/.loom/daemon.log"
        echo "    and reloaded the user manager supervising its live daemon."
        echo "    Set LOOM_CI_LIVE_LEAK_GUARD=warn to downgrade this to a warning."
        echo "############################################################"
    } >&2
    if [[ "${LOOM_CI_LIVE_LEAK_GUARD:-fail}" == "warn" ]]; then
        echo "    (LOOM_CI_LIVE_LEAK_GUARD=warn — not failing the run)" >&2
    else
        failed=$((failed + 1))
        failed_names+=("<live-host leak guard, #8077>")
    fi
fi

if [[ "$failed" -ne 0 ]]; then
    printf 'Failed suites: %s\n' "${failed_names[*]}" >&2
    exit 1
fi
exit 0
