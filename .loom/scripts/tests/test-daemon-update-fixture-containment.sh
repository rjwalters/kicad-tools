#!/usr/bin/env bash
# test-daemon-update-fixture-containment.sh — the harness is the subject
# (#8712).
#
# WHY THIS SUITE EXISTS
#
# The incident behind #8712 was not a bug in any script under test: it was a
# 472-byte bash fake FROM THIS HARNESS — lib/daemon-update-fixtures.sh's
# `fake loom-daemon: unsupported subcommand` stub — sitting at a fleet host's
# real `loom-daemon/target/release/loom-daemon`. `loom-daemon-update.sh
# --fetch` resolves the binary that IMPLEMENTS `release-fetch` by preferring a
# repo-local build, got the fake, and every `auto_update` fetch on that host
# failed for hours while the machine-level install it was trying to update sat
# there working. Moving the fake aside fixed it instantly.
#
# So the fixtures' containment guard is load-bearing, and load-bearing code
# gets assertions. It is tested HERE rather than in the suites that use it
# (test-loom-daemon-update.sh and its -fetch / -resolve-json siblings) because
# it belongs to lib/daemon-update-fixtures.sh, not to any one flow, and must
# hold for ANY invocation that reaches it.
#
# Hermetic: no network, no live forge, no tokens, and — the whole point —
# nothing written outside its own `mktemp -d`.
#
# Needs a built loom-daemon only because sourcing the fixtures library pins
# $LOOM_DAEMON_BIN through require-daemon-bin.sh; it is therefore wired in the
# "Native Port Suites" CI job alongside its -fetch sibling.
#
# Usage:
#   ./.loom/scripts/tests/test-daemon-update-fixture-containment.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
LOOM_REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CLI_DIR="$(cd "$SCRIPT_DIR/../cli" && pwd)"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
START_SCRIPT="$CLI_DIR/loom-daemon-start.sh"

# shellcheck source=lib/daemon-update-fixtures.sh
source "$SCRIPT_DIR/lib/daemon-update-fixtures.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_true() {
    local cond_rc="$1" msg="$2" detail="${3:-}"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$cond_rc" -eq 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        [[ -n "$detail" ]] && echo "    detail: $detail"
    fi
}

BASE_WORKDIR="$(mktemp -d)"
# The declaration under test. Everything this suite writes must land here.
loom_fixture_scratch_root "$BASE_WORKDIR" || {
    echo "FATAL: could not arm fixture containment — the subject of this suite is unavailable." >&2
    rm -rf "$BASE_WORKDIR"
    exit 1
}
# A directory that is deliberately OUTSIDE the scratch root, standing in for
# the real checkout. Simulated rather than real, on purpose: a test that has to
# write into the real checkout to prove it must not is the bug it is testing.
OUTSIDE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-fixture-outside.XXXXXX")"
cleanup() { rm -rf "$BASE_WORKDIR" "$OUTSIDE_ROOT"; }
trap cleanup EXIT

echo "=== daemon-update fixture containment (#8712) ==="
echo

# ------------------------------------------------------------
# A. The DIRECT writers. write_fake_daemon / write_fake_artifact_daemon /
#    write_fake_cargo are each handed an explicit destination; a suite that
#    computes one wrongly (or a future fixture that derives one from an
#    ambient variable) must be stopped at the write, not discovered on a host.
#
#    Each runs in a SUBSHELL because the guard's answer to "you are about to
#    write outside the scratch root" is `exit 1` — a fixture that cannot write
#    where it was told has no correct fallback.
# ------------------------------------------------------------
echo "A. direct fixture writers"

out=$( ( write_fake_daemon "$BASE_WORKDIR/ok-daemon" "c0mm1t" "$BASE_WORKDIR/marker" ) 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=0" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "write_fake_daemon inside the scratch root still succeeds"
assert_true "$([[ -s "$BASE_WORKDIR/ok-daemon" ]] && echo 0 || echo 1)" \
    "…and still writes the fake it was asked for"

out=$( ( write_fake_daemon "$OUTSIDE_ROOT/loom-daemon" "c0mm1t" "$OUTSIDE_ROOT/marker" ) 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=1" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "write_fake_daemon OUTSIDE the scratch root aborts instead of writing"
assert_true "$(grep -q 'FIXTURE CONTAINMENT VIOLATION' <<<"$out" && echo 0 || echo 1)" \
    "…and the refusal names the violation" "$out"
assert_true "$([[ ! -e "$OUTSIDE_ROOT/loom-daemon" ]] && echo 0 || echo 1)" \
    "…and nothing was created at the refused path"

out=$( ( write_fake_artifact_daemon "$OUTSIDE_ROOT/artifact-daemon" "9.9.9" "c0mm1t" ) 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=1" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "write_fake_artifact_daemon is contained by the same guard"

out=$( ( write_fake_cargo "$OUTSIDE_ROOT/cargo" ) 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=1" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "write_fake_cargo is contained by the same guard"

# A path whose parent does not exist yet must still be judged by where it WOULD
# land — `target/release/` never exists before the build that creates it, which
# is precisely the shape of the path that was poisoned.
out=$( ( write_fake_daemon "$OUTSIDE_ROOT/not/created/yet/loom-daemon" "c0mm1t" "$OUTSIDE_ROOT/m2" ) 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=1" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "a not-yet-existing destination is judged by its deepest existing ancestor"

echo

# ------------------------------------------------------------
# B. The fake `cargo` — the one fixture whose destination is computed at RUN
#    time (`${CARGO_TARGET_DIR:-target}/release/loom-daemon`, relative to the
#    cwd `loom-daemon-update.sh` chose). It is therefore the one that can reach
#    a real build-output path with no test bug at all: only a host condition
#    under which the script's `$PWD` stops resolving to the fixture and one of
#    its documented checkout fallbacks (#5140 / #4229) picks a real checkout
#    while a fake `cargo` is still first on `$PATH`.
#
#    It re-checks the scratch root itself, in the child process, because by
#    then it IS a separate process — the parent's guard has long since run.
# ------------------------------------------------------------
echo "B. the fake cargo (run-time destination)"

WB="$BASE_WORKDIR/b"
mkdir -p "$WB/loom-daemon"
write_fake_daemon "$WB/new-fake-bin" "b0mm1t0" "$WB/b-marker"
write_fake_cargo "$WB/cargo"

out=$( cd "$WB/loom-daemon" && NEW_FAKE_BIN_SRC="$WB/new-fake-bin" \
    bash "$WB/cargo" build --release 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=0" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "a build inside the scratch root still succeeds"
assert_true "$([[ -x "$WB/loom-daemon/target/release/loom-daemon" ]] && echo 0 || echo 1)" \
    "…and still writes the fake build output where the fixture expects it" "$out"

mkdir -p "$OUTSIDE_ROOT/checkout/loom-daemon"
out=$( cd "$OUTSIDE_ROOT/checkout/loom-daemon" && NEW_FAKE_BIN_SRC="$WB/new-fake-bin" \
    bash "$WB/cargo" build --release 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=1" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "a build OUTSIDE the scratch root fails instead of writing (the poisoning path)"
assert_true "$([[ ! -e "$OUTSIDE_ROOT/checkout/loom-daemon/target" ]] && echo 0 || echo 1)" \
    "…and not even an empty target/ dir is created there" "$out"
assert_true "$(grep -q 'FIXTURE CONTAINMENT VIOLATION' <<<"$out" && echo 0 || echo 1)" \
    "…and the refusal says why, naming the issue" "$out"

# `cargo metadata` writes too (it mkdir -p's the target dir to resolve it).
out=$( cd "$OUTSIDE_ROOT/checkout/loom-daemon" && \
    bash "$WB/cargo" metadata --format-version 1 --no-deps 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=1" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "the metadata path is contained too, not just build"

echo

# ------------------------------------------------------------
# C. OPT-IN. A suite that never declares a scratch root behaves exactly as it
#    did before #8712 — the guard is a discipline a suite adopts, not a
#    condition sourcing this library imposes on every unrelated consumer.
# ------------------------------------------------------------
echo "C. undeclared scratch root ⇒ unchanged behaviour"

out=$( ( unset LOOM_FIXTURE_SCRATCH_ROOT
         write_fake_daemon "$OUTSIDE_ROOT/undeclared-daemon" "c0mm1t" "$OUTSIDE_ROOT/m3" ) 2>&1
       echo "EXIT=$?" )
assert_eq "EXIT=0" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "with no scratch root declared, a write outside it is permitted as before"
rm -f "$OUTSIDE_ROOT/undeclared-daemon"

echo

# ------------------------------------------------------------
# D. THE SENTINEL. Prevention (above) and detection (here) are separate for the
#    reason #5179's live-state sandbox states: isolation alone is
#    unfalsifiable. The sentinel watches the REAL checkout's canonical build
#    output and reports it if the suite CREATED it — never if it was merely
#    already there, which is a developer's own `cargo build --release`.
# ------------------------------------------------------------
echo "D. the real-build-output sentinel"

# Clean run: this suite creates nothing at the real path, so the sentinel is
# satisfied. (If a developer HAS a real build there, the sentinel is satisfied
# for the other reason — it existed before the run. Both are pass.)
loom_fixture_assert_build_output_untouched
assert_true "$?" \
    "loom_fixture_assert_build_output_untouched passes for a run that created nothing"

# Its two branches, driven against a stand-in path rather than the real one —
# the whole point being that no test may write to the real build output.
_LOOM_FIXTURE_BUILD_OUTPUT="$OUTSIDE_ROOT/sentinel-target/release/loom-daemon"
_LOOM_FIXTURE_BUILD_OUTPUT_EXISTED=false
mkdir -p "$(dirname "$_LOOM_FIXTURE_BUILD_OUTPUT")"
printf '#!/usr/bin/env bash\necho "fake loom-daemon: unsupported subcommand: $*" >&2; exit 1\n' \
    > "$_LOOM_FIXTURE_BUILD_OUTPUT"
out=$( loom_fixture_assert_build_output_untouched 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=1" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "…and FAILS when the run created a build output that was not there before"
assert_true "$([[ ! -e "$_LOOM_FIXTURE_BUILD_OUTPUT" ]] && echo 0 || echo 1)" \
    "…removing the offender, because it is one of our own shell fakes" "$out"

# A non-shell file is left alone: it may be a real build, and deleting a
# developer's binary to make a test tidy is worse than reporting it.
printf '\177ELF not really, but not a shell script either\n' > "$_LOOM_FIXTURE_BUILD_OUTPUT"
out=$( loom_fixture_assert_build_output_untouched 2>&1; echo "EXIT=$?" )
assert_eq "EXIT=1" "$(grep -o 'EXIT=[0-9]*' <<<"$out")" \
    "a non-shell file at the build output still FAILS the sentinel"
assert_true "$([[ -e "$_LOOM_FIXTURE_BUILD_OUTPUT" ]] && echo 0 || echo 1)" \
    "…but is left in place, since it may be a real build" "$out"

# Pre-existing (not created by this run) is never reported at all.
_LOOM_FIXTURE_BUILD_OUTPUT_EXISTED=true
loom_fixture_assert_build_output_untouched
assert_true "$?" \
    "a build output that existed BEFORE the run is never reported (no noise on a developer's build)"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
