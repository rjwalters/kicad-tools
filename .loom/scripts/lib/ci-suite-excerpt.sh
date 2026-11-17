#!/usr/bin/env bash
# ci-suite-excerpt.sh — the failure-excerpt printer for run-ci-suites.sh
# (issue #6662).
#
# ## Why this exists
#
# run-ci-suites.sh captures each suite's stdout+stderr to /tmp and, on
# failure, prints an excerpt into the CI job log. That excerpt used to be a
# bare `tail -40`: a FIXED TRAILING WINDOW that says nothing about where the
# failure actually is.
#
# For any suite that keeps running after its first failed assertion — which is
# every suite in this repo, they all count PASS/FAIL and report a total at the
# end — the failing assertion is wherever it happens to be, and the last 40
# lines are whatever ran LAST. Those are unrelated facts. #6662 is the
# concrete bill for that: `tests/install/test-provision-daemon.sh` failed on
# the self-hosted runner with `Total: 99  Passed: 96  Failed: 3`, and all
# three `FAIL:` lines sat ~60 lines above the window, so five consecutive CI
# runs emitted a diagnostic excerpt in which every single visible line said
# PASS. Nobody could name the assertion from CI artifacts at all.
#
# So the excerpt is now anchored on the FAILURES, not on the end of the file:
# the first N matching lines (with a few lines of trailing context, because
# this repo's assert helpers print `expected:` / `actual:` on the lines
# following the FAIL line) are printed FIRST, then the tail is printed as
# before. The tail is deliberately kept — it carries the suite's own
# pass/fail totals and any late crash output — this is additive, nothing that
# used to be visible stops being visible.
#
# Sourced by run-ci-suites.sh rather than inlined there so it can be tested
# directly against synthetic logs (see
# defaults/scripts/tests/test-run-ci-suites-failure-excerpt.sh) without
# executing a full CI suite run — the same "extract for testability" move
# #6528 made for live-daemon-guard.sh.
#
# ## Knobs (env, all optional)
#
#   LOOM_CI_FAIL_EXCERPT_MAX   max failure lines to print   (default 20)
#   LOOM_CI_FAIL_CONTEXT_LINES trailing context per match   (default 3)
#   LOOM_CI_FAIL_TAIL_LINES    trailing-window size         (default 40)

# Pattern for "this line reports a failure". Covers the conventions in use
# across this repo's suites plus the ones a suite can plausibly adopt:
#
#   FAIL      the `FAIL:` / `FAILED:` prefix (assert_eq-style suites,
#             e.g. tests/install/*)
#   ✗         U+2717 BALLOT X — the bullet used by this repo's
#             check/pass/fail-style suites (e.g.
#             defaults/scripts/tests/test-run-ci-suites-daemon-guard.sh)
#   ✘         U+2718 HEAVY BALLOT X
#   ✖         U+2716 HEAVY MULTIPLICATION X
#   ^not ok   TAP-style result lines
#
# The last three are proactive (#6745): as of 2026-08-23 no wired suite emits
# them, so this closes the gap the moment one adopts a different convention
# rather than after another blind CI run. They come from the inline excerpt in
# PR #6639, which matched `✗|✘|✖|FAIL:|^not ok`; that block was reverted in
# favor of this library (#6662) and the wider marker set was lost with it.
#
# Deliberately substring-loose on FAIL so a color-wrapped `\033[0;31mFAIL\033[0m:`
# still matches — the excerpt is a diagnostic, so a false positive costs a few
# extra printed lines while a false negative costs another blind CI run. The
# same latitude is NOT extended to `not ok`: unlike `FAIL` and the cross-mark
# bullets it is ordinary lowercase English, so an unanchored match would fire on
# any prose containing the phrase and bury the real failure. It is anchored to
# line start, where TAP puts it. `-m "$max"` caps whatever does match.
LOOM_CI_FAIL_PATTERN='FAIL|✗|✘|✖|^not ok'

# print_suite_failure_excerpt <suite-label> <log-path>
#
# Prints, to stdout:
#   1. the first LOOM_CI_FAIL_EXCERPT_MAX lines matching LOOM_CI_FAIL_PATTERN,
#      each with LOOM_CI_FAIL_CONTEXT_LINES lines of trailing context and its
#      line number in the captured log, and
#   2. the last LOOM_CI_FAIL_TAIL_LINES lines of the log.
#
# Never fails: a missing log file, or a failing suite that emitted no
# recognizable failure line at all (a crash, a timeout kill, a non-zero exit
# with no assertions), both print an explicit note instead of silently
# emitting nothing. Always returns 0 — this is a reporter, and it must never
# be the thing that changes a run's exit status.
#
# On a SHORT log the two sections can overlap (the first failure is also
# inside the trailing window). That duplication is intentional and left
# unsuppressed: the alternative is a length-dependent excerpt shape that is
# harder to read and harder to test than a few repeated lines.
print_suite_failure_excerpt() {
    local suite="$1" log="$2"
    local max="${LOOM_CI_FAIL_EXCERPT_MAX:-20}"
    local ctx="${LOOM_CI_FAIL_CONTEXT_LINES:-3}"
    local tail_lines="${LOOM_CI_FAIL_TAIL_LINES:-40}"

    if [[ ! -f "$log" ]]; then
        printf -- '----- no captured log for %s (expected at %s) -----\n' "$suite" "$log"
        return 0
    fi

    printf -- '----- first %s failure line(s) of %s -----\n' "$max" "$suite"
    # -a: never treat a log with stray binary bytes as binary and collapse it
    # to "Binary file matches" (a suite that cats a fixture can do this).
    # -n: line numbers, so an operator can find the spot in the full artifact.
    # -m/-A: cap the excerpt and carry the expected/actual diagnostic lines.
    if ! grep -a -n -m "$max" -A "$ctx" -E "$LOOM_CI_FAIL_PATTERN" "$log"; then
        printf -- '(no line matched /%s/ — this suite failed without emitting a recognizable\n' "$LOOM_CI_FAIL_PATTERN"
        printf -- ' failure line: a crash, a timeout kill, or a non-zero exit before any\n'
        printf -- ' assertion ran. The trailing window below is the whole diagnostic.)\n'
    fi

    printf -- '----- last %s lines of %s -----\n' "$tail_lines" "$suite"
    tail -n "$tail_lines" "$log"
    printf -- '----- end %s -----\n' "$suite"
    return 0
}
