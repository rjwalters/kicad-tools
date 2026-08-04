#!/usr/bin/env bash
# run-ci-suites.sh — run the CI-wired shell test suites for
# defaults/scripts/tests/ (issue #4455).
#
# Runs every suite listed in ci-wired.txt, one at a time, capturing pass/fail
# and per-suite wall-clock time. Prints a summary and exits non-zero if any
# wired suite fails. The wired/excluded partition invariant is enforced first
# via check-ci-suite-manifest.sh, so a fresh unlisted suite is a hard failure
# here (it cannot silently slip into an unwired pool).
#
# Usage:
#   run-ci-suites.sh                 # run the whole wired set
#   LOOM_CI_SUITE_TIMEOUT=180 …      # per-suite timeout in seconds (default 1200)
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

# 1) Fail fast if the manifest partition invariant is broken.
if ! bash "$SCRIPT_DIR/check-ci-suite-manifest.sh"; then
    echo "::error::CI-suite manifest invariant failed — fix ci-wired.txt / ci-excluded.txt" >&2
    exit 1
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
failed_names=()
total_start=$(date +%s)

printf '\n=== Running %d CI-wired shell suites (timeout %ss each) ===\n\n' \
    "${#suites[@]}" "$PER_SUITE_TIMEOUT"

for suite in "${suites[@]}"; do
    if [[ "$suite" == */* ]]; then
        path="$REPO_ROOT/$suite"
    else
        path="$SCRIPT_DIR/$suite"
    fi
    log_name="${suite//\//_}"
    if [[ ! -f "$path" ]]; then
        echo "FAIL  $suite (missing file)"
        failed=$((failed + 1)); failed_names+=("$suite"); continue
    fi
    start=$(date +%s)
    if [[ -n "$timeout_cmd" ]]; then
        "$timeout_cmd" "$PER_SUITE_TIMEOUT" bash "$path" >"/tmp/ci-suite-$log_name.log" 2>&1
    else
        bash "$path" >"/tmp/ci-suite-$log_name.log" 2>&1
    fi
    rc=$?
    dur=$(( $(date +%s) - start ))
    if [[ "$rc" -eq 0 ]]; then
        printf 'PASS  %-52s %3ss\n' "$suite" "$dur"
        passed=$((passed + 1))
    else
        printf 'FAIL  %-52s %3ss (exit %s)\n' "$suite" "$dur" "$rc"
        failed=$((failed + 1)); failed_names+=("$suite")
        echo "----- last 40 lines of $suite -----"
        tail -40 "/tmp/ci-suite-$log_name.log"
        echo "----- end $suite -----"
    fi
done

total_dur=$(( $(date +%s) - total_start ))
printf '\n=== Summary: %d passed, %d failed of %d wired suites in %ss ===\n' \
    "$passed" "$failed" "${#suites[@]}" "$total_dur"

if [[ "$failed" -ne 0 ]]; then
    printf 'Failed suites: %s\n' "${failed_names[*]}" >&2
    exit 1
fi
exit 0
