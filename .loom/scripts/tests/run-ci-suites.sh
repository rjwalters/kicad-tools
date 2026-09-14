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

mapfile -t suites < <(sed -E 's/#.*$//' "$WIRED_MANIFEST" | awk 'NF { print $1 }')

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
run_suite() {
    local suite="$1" path log_name start dur rc
    if [[ "$suite" == */* ]]; then
        path="$REPO_ROOT/$suite"
    else
        path="$SCRIPT_DIR/$suite"
    fi
    log_name="${suite//\//_}"
    if [[ ! -f "$path" ]]; then
        printf 'MISSING 0\n' >"$RESULTS_DIR/$log_name.result"
        return 0
    fi
    start=$(date +%s)
    if [[ -n "$timeout_cmd" ]]; then
        "$timeout_cmd" "$PER_SUITE_TIMEOUT" bash "$path" >"/tmp/ci-suite-$log_name.log" 2>&1
    else
        bash "$path" >"/tmp/ci-suite-$log_name.log" 2>&1
    fi
    rc=$?
    dur=$(( $(date +%s) - start ))
    printf '%s %s\n' "$rc" "$dur" >"$RESULTS_DIR/$log_name.result"
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
running=0
for suite in "${suites[@]}"; do
    if suite_is_daemon_guarded "$suite"; then
        continue
    fi
    if suite_is_serial_lane "$suite"; then
        continue
    fi
    run_suite "$suite" &
    running=$((running + 1))
    if [[ "$running" -ge "$PARALLELISM" ]]; then
        wait -n
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
    read -r rc dur <"$result_file"
    if [[ "$rc" == "MISSING" ]]; then
        echo "FAIL  $suite (missing file)"
        failed=$((failed + 1)); failed_names+=("$suite"); continue
    fi
    if [[ "$rc" -eq 0 ]]; then
        printf 'PASS  %-52s %3ss\n' "$suite" "$dur"
        passed=$((passed + 1))
    else
        printf 'FAIL  %-52s %3ss (exit %s)\n' "$suite" "$dur" "$rc"
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

if [[ "$failed" -ne 0 ]]; then
    printf 'Failed suites: %s\n' "${failed_names[*]}" >&2
    exit 1
fi
exit 0
