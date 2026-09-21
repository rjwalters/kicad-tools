#!/usr/bin/env bash
# test-require-daemon-bin.sh — Tests for tests/lib/require-daemon-bin.sh's
# loom_test_require_daemon_bin (#8176).
#
# The regression under test is a SHELL SUITE SILENTLY TESTING THE WRONG BINARY.
# On a host whose $CARGO_TARGET_DIR (or ~/.cargo/config.toml build.target-dir)
# is redirected to one directory shared by every checkout and worktree, the
# harness used to pin whatever the shared candidate order happened to name
# first, which produced two plausible-looking false regressions — both observed
# for real while verifying #8119/PR #8174:
#
#   1. RELEASE-OVER-DEBUG: a months-old release/loom-daemon outranks the debug
#      build a developer just made, so assertions run against a binary that
#      predates the change under test (T1-T3).
#   2. CROSS-WORKTREE CLOBBER: the resolved path is shared, so a concurrent
#      `cargo build` elsewhere can replace that file after resolution and
#      before the assertions run (T4-T6).
#
# Everything here drives the harness through fake, tagged "binaries" — shell
# scripts that echo an identifying tag for any argument — so the suite is
# hermetic and needs no Rust toolchain. `loom_test_require_daemon_bin` exits
# rather than returning on failure, so every case runs it in a subprocess
# (runner.sh below) and asserts on that process's stdout/stderr/exit code.
#
# Style matches test-locate-daemon-bin.sh, its sibling one layer down — plain
# bash, hand-rolled assertions, no Bats.
#
# Usage:
#   ./defaults/scripts/tests/test-require-daemon-bin.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$SCRIPT_DIR/lib/require-daemon-bin.sh"
LOCATE_LIB="$SCRIPT_DIR/../lib/locate-daemon-bin.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "${GREEN}✓${NC} $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "${RED}✗${NC} $1"; }

assert_eq() { # <expected> <actual> <msg>
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$1', got '$2')"; fi
}

assert_contains() { # <needle> <haystack> <msg>
    if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3 (expected to find '$1' in [$2])"; fi
}

assert_not_contains() { # <needle> <haystack> <msg>
    if [[ "$2" != *"$1"* ]]; then pass "$3"; else fail "$3 (did NOT expect '$1' in [$2])"; fi
}

for required in "$HARNESS" "$LOCATE_LIB"; do
    if [[ ! -r "$required" ]]; then
        echo -e "${RED}FATAL${NC}: $required not found" >&2
        exit 1
    fi
done

MINIMAL_PATH="/usr/bin:/bin"

# Normalized through `cd`+`pwd`: $TMPDIR carries a trailing slash on macOS, and
# the harness reports a repo_root it resolved the same way, so an un-normalized
# fixture path would differ from it by a stray `//` and fail comparisons.
WORKDIR="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/test-require-daemon-bin.XXXXXX")" && pwd)"
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# Snapshots land under $TMPDIR, so pointing every child at a TMPDIR inside
# $WORKDIR makes them observable here and disposable with the rest of the
# fixture.
TMPROOT="$WORKDIR/tmp"
SNAPSHOT_ROOT="$TMPROOT/loom-test-daemon-bin"
mkdir -p "$TMPROOT"

NOHOME="$WORKDIR/nohome"
mkdir -p "$NOHOME"

# A fake loom-daemon: echoes its tag for ANY argument, so the harness's
# `<bin> <sub> --help` preflight passes and the tag identifies which file the
# suite ended up pinned to.
make_tagged_bin() { # <path> <tag>
    mkdir -p "$(dirname "$1")"
    cat > "$1" <<EOF
#!/usr/bin/env bash
echo "$2"
EOF
    chmod +x "$1"
}

# A fake loom-daemon that knows no subcommands (exit 2 for everything) — what a
# binary predating a port behaves like.
make_portless_bin() { # <path>
    mkdir -p "$(dirname "$1")"
    cat > "$1" <<'EOF'
#!/usr/bin/env bash
echo "unknown subcommand" >&2
exit 2
EOF
    chmod +x "$1"
}

# A fixture "repo": <root>/defaults/scripts is the scripts_dir the harness is
# handed, so the repo_root it derives (scripts_dir/../..) is <root>.
make_fixture_repo() { # <root>
    mkdir -p "$1/defaults/scripts/lib"
    cp "$LOCATE_LIB" "$1/defaults/scripts/lib/locate-daemon-bin.sh"
}

RUNNER="$WORKDIR/runner.sh"
cat > "$RUNNER" <<'EOF'
#!/usr/bin/env bash
# Source the harness under test, run it with "$@", then report what the two
# pins ended up meaning and what the pinned binary actually answers. The `set`
# flags are deliberately the STRICTEST any consumer suite uses (`set -euo
# pipefail`), so a helper in the harness that leaks a non-zero intermediate
# status — the classic way a sourced library kills its caller — takes this
# runner down here rather than in CI.
set -euo pipefail
# shellcheck disable=SC1090
source "$LOOM_TEST_HARNESS"
loom_test_require_daemon_bin "$@"
# A post hook lets a case mutate the filesystem AFTER resolution and BEFORE the
# pinned binary is invoked — the cross-worktree-clobber window.
if [[ -n "${LOOM_TEST_POST_HOOK:-}" ]]; then
    bash "$LOOM_TEST_POST_HOOK"
fi
echo "SELF_BIN=$LOOM_DAEMON_SELF_BIN"
echo "DAEMON_BIN=${LOOM_DAEMON_BIN:-<unset>}"
echo "IDENTITY=$("$LOOM_DAEMON_SELF_BIN" whoami)"
EOF
chmod +x "$RUNNER"

# run_harness <ENV=VAL ...> -- <loom_test_require_daemon_bin args...>
# Runs in a pristine environment; stderr is left for the caller to redirect.
run_harness() {
    local envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift
    env -i PATH="$MINIMAL_PATH" HOME="$NOHOME" TMPDIR="$TMPROOT" \
        LOOM_TEST_HARNESS="$HARNESS" ${envs[@]+"${envs[@]}"} \
        bash "$RUNNER" "$@"
}

# field <KEY> <runner stdout> -- the first `KEY=value` line's value, or "".
# A plain read loop rather than `grep | head`: an early-exit consumer under
# `set -o pipefail` is the SIGPIPE class check-pipefail-early-exit.sh ratchets
# (#7790).
field() {
    local key="$1" line
    while IFS= read -r line; do
        case "$line" in
            "$key="*) printf '%s\n' "${line#"$key"=}"; return 0 ;;
        esac
    done <<< "$2"
    printf '%s\n' ""
}

OLD_STAMP="202601010101"   # `touch -t` format: an unambiguously stale build

# ===========================================================================
# T1-T3: which repo-local build wins (#8176 case 1)
# ===========================================================================

# ---------- T1. A stale release/ build must NOT outrank a fresh debug/ build
#                under a shared $CARGO_TARGET_DIR. This is the exact shape of
#                the false T15g/T15h regression on PR #8174. ----------
ROOT1="$WORKDIR/t1-repo"; make_fixture_repo "$ROOT1"
TARGET1="$WORKDIR/t1-shared-target"
make_tagged_bin "$TARGET1/release/loom-daemon" "STALE-RELEASE"
make_tagged_bin "$TARGET1/debug/loom-daemon" "FRESH-DEBUG"
touch -t "$OLD_STAMP" "$TARGET1/release/loom-daemon"

out="$(run_harness CARGO_TARGET_DIR="$TARGET1" -- "$ROOT1/defaults/scripts" "some-subcommand" 2>"$WORKDIR/t1-stderr")"
rc=$?
assert_eq "0" "$rc" "T1: the harness succeeds with both a stale release and a fresh debug build present"
assert_eq "FRESH-DEBUG" "$(field IDENTITY "$out")" \
    "T1: a stale release/loom-daemon does NOT outrank a fresher debug/loom-daemon under a shared \$CARGO_TARGET_DIR (#8176)"

# ---------- T2. Equal mtimes keep the historical candidate order (release
#                first), so a host that has only ever built one profile sees no
#                behaviour change at all. ----------
ROOT2="$WORKDIR/t2-repo"; make_fixture_repo "$ROOT2"
TARGET2="$WORKDIR/t2-shared-target"
make_tagged_bin "$TARGET2/release/loom-daemon" "RELEASE"
make_tagged_bin "$TARGET2/debug/loom-daemon" "DEBUG"
touch -t "$OLD_STAMP" "$TARGET2/release/loom-daemon" "$TARGET2/debug/loom-daemon"

out="$(run_harness CARGO_TARGET_DIR="$TARGET2" -- "$ROOT2/defaults/scripts" "some-subcommand" 2>/dev/null)"
assert_eq "RELEASE" "$(field IDENTITY "$out")" \
    "T2: equal mtimes keep the shared candidate order (release before debug) — no gratuitous precedence change"

# ---------- T3. The rule is FRESHEST, not "debug always": a fresh release
#                build still wins over a stale debug one. ----------
ROOT3="$WORKDIR/t3-repo"; make_fixture_repo "$ROOT3"
TARGET3="$WORKDIR/t3-shared-target"
make_tagged_bin "$TARGET3/debug/loom-daemon" "STALE-DEBUG"
make_tagged_bin "$TARGET3/release/loom-daemon" "FRESH-RELEASE"
touch -t "$OLD_STAMP" "$TARGET3/debug/loom-daemon"

out="$(run_harness CARGO_TARGET_DIR="$TARGET3" -- "$ROOT3/defaults/scripts" "some-subcommand" 2>/dev/null)"
assert_eq "FRESH-RELEASE" "$(field IDENTITY "$out")" \
    "T3: a fresh release build still wins over a stale debug build (freshest wins, not 'debug always')"

# ===========================================================================
# T4-T6: surviving a clobber of the resolved path (#8176 case 2)
# ===========================================================================

# ---------- T4. A concurrent worktree replacing the resolved binary AFTER
#                resolution must not reach this suite. ----------
ROOT4="$WORKDIR/t4-repo"; make_fixture_repo "$ROOT4"
TARGET4="$WORKDIR/t4-shared-target"
make_tagged_bin "$TARGET4/debug/loom-daemon" "MINE"
CLOBBER4="$WORKDIR/t4-clobber.sh"
cat > "$CLOBBER4" <<EOF
#!/usr/bin/env bash
# Stand in for another worktree's \`cargo build\` landing on the shared path.
cat > "$TARGET4/debug/loom-daemon" <<'INNER'
#!/usr/bin/env bash
echo "FOREIGN"
INNER
chmod +x "$TARGET4/debug/loom-daemon"
EOF
chmod +x "$CLOBBER4"

out="$(run_harness CARGO_TARGET_DIR="$TARGET4" LOOM_TEST_POST_HOOK="$CLOBBER4" \
    -- "$ROOT4/defaults/scripts" "some-subcommand" 2>/dev/null)"
assert_eq "MINE" "$(field IDENTITY "$out")" \
    "T4: a rebuild that replaces the resolved path mid-run cannot swap the binary under this suite (#8176)"
assert_eq "FOREIGN" "$("$TARGET4/debug/loom-daemon" whoami)" \
    "T4: …and the clobber really did land on the shared path (the fixture is exercising the hazard, not a no-op)"

# ---------- T5. The pinned path is a private copy outside the shared target
#                dir — the mechanism T4 depends on, asserted directly. ----------
self_bin="$(field SELF_BIN "$out")"
assert_not_contains "$TARGET4" "$self_bin" "T5: the pinned binary is NOT a path inside the shared \$CARGO_TARGET_DIR"
assert_contains "$SNAPSHOT_ROOT/suite-" "$self_bin" "T5: the pinned binary is a private per-suite snapshot under \$TMPDIR"
assert_eq "DAEMON_BIN=$self_bin" "DAEMON_BIN=$(field DAEMON_BIN "$out")" \
    "T5: without --self-only both pins name the same snapshot (unchanged #8134 contract)"

# ---------- T6. The opt-out is honest about what it costs: with snapshots
#                disabled the harness pins the shared path itself and the same
#                clobber DOES reach the suite. ----------
ROOT6="$WORKDIR/t6-repo"; make_fixture_repo "$ROOT6"
TARGET6="$WORKDIR/t6-shared-target"
make_tagged_bin "$TARGET6/debug/loom-daemon" "MINE"
CLOBBER6="$WORKDIR/t6-clobber.sh"
cat > "$CLOBBER6" <<EOF
#!/usr/bin/env bash
cat > "$TARGET6/debug/loom-daemon" <<'INNER'
#!/usr/bin/env bash
echo "FOREIGN"
INNER
chmod +x "$TARGET6/debug/loom-daemon"
EOF
chmod +x "$CLOBBER6"

out="$(run_harness CARGO_TARGET_DIR="$TARGET6" LOOM_TEST_POST_HOOK="$CLOBBER6" \
    LOOM_TEST_DAEMON_BIN_NO_SNAPSHOT=1 -- "$ROOT6/defaults/scripts" "some-subcommand" 2>/dev/null)"
assert_eq "$TARGET6/debug/loom-daemon" "$(field SELF_BIN "$out")" \
    "T6: LOOM_TEST_DAEMON_BIN_NO_SNAPSHOT=1 pins the resolved path itself (for a subject that depends on its location)"
assert_eq "FOREIGN" "$(field IDENTITY "$out")" \
    "T6: …and that opt-out really does re-expose the clobber, so the snapshot is what closes it"

# ===========================================================================
# T7-T9: precedence the fix must NOT have changed
# ===========================================================================

# ---------- T7. An explicit $LOOM_DAEMON_SELF_BIN still outranks any repo
#                build, however fresh. ----------
ROOT7="$WORKDIR/t7-repo"; make_fixture_repo "$ROOT7"
TARGET7="$WORKDIR/t7-shared-target"
make_tagged_bin "$TARGET7/debug/loom-daemon" "REPO-BUILD"
PINNED7="$WORKDIR/t7-pinned/loom-daemon"
make_tagged_bin "$PINNED7" "EXPLICIT-SELF-PIN"

out="$(run_harness CARGO_TARGET_DIR="$TARGET7" LOOM_DAEMON_SELF_BIN="$PINNED7" \
    -- "$ROOT7/defaults/scripts" "some-subcommand" 2>/dev/null)"
assert_eq "EXPLICIT-SELF-PIN" "$(field IDENTITY "$out")" \
    "T7: an explicit \$LOOM_DAEMON_SELF_BIN still wins over a fresher repo-local build"

# ---------- T8. …and so does an explicit $LOOM_DAEMON_BIN, in the default
#                (not --self-only) mode where it means the implementation. ----------
ROOT8="$WORKDIR/t8-repo"; make_fixture_repo "$ROOT8"
TARGET8="$WORKDIR/t8-shared-target"
make_tagged_bin "$TARGET8/debug/loom-daemon" "REPO-BUILD"
PINNED8="$WORKDIR/t8-pinned/loom-daemon"
make_tagged_bin "$PINNED8" "EXPLICIT-DAEMON-BIN"

out="$(run_harness CARGO_TARGET_DIR="$TARGET8" LOOM_DAEMON_BIN="$PINNED8" \
    -- "$ROOT8/defaults/scripts" "some-subcommand" 2>/dev/null)"
assert_eq "EXPLICIT-DAEMON-BIN" "$(field IDENTITY "$out")" \
    "T8: an explicit \$LOOM_DAEMON_BIN still wins over a repo-local build in default mode (operator pin preserved)"

# ---------- T9. --self-only: $LOOM_DAEMON_BIN is the suite's PROBE mock, so it
#                must neither answer "which binary implements the stub" nor be
#                overwritten by the harness (#8134). ----------
ROOT9="$WORKDIR/t9-repo"; make_fixture_repo "$ROOT9"
TARGET9="$WORKDIR/t9-shared-target"
make_tagged_bin "$TARGET9/debug/loom-daemon" "IMPLEMENTATION"
MOCK9="$WORKDIR/t9-mock/loom-daemon-mock"
make_tagged_bin "$MOCK9" "PROBE-MOCK"

out="$(run_harness CARGO_TARGET_DIR="$TARGET9" LOOM_DAEMON_BIN="$MOCK9" \
    -- --self-only "$ROOT9/defaults/scripts" "some-subcommand" 2>/dev/null)"
assert_eq "IMPLEMENTATION" "$(field IDENTITY "$out")" \
    "T9: --self-only resolves the repo build as the implementation, never the \$LOOM_DAEMON_BIN probe mock (#8134)"
assert_eq "$MOCK9" "$(field DAEMON_BIN "$out")" \
    "T9: …and leaves \$LOOM_DAEMON_BIN pointing at the suite's own mock, untouched"

# ---------- T10. An installed consumer repo has no repo-local build; the
#                 harness still falls through to the ambient resolution. ----------
ROOT10="$WORKDIR/t10-repo"; make_fixture_repo "$ROOT10"
PATHDIR10="$WORKDIR/t10-on-path"
make_tagged_bin "$PATHDIR10/loom-daemon" "ON-PATH"

out="$(env -i PATH="$PATHDIR10:$MINIMAL_PATH" HOME="$NOHOME" TMPDIR="$TMPROOT" \
    LOOM_TEST_HARNESS="$HARNESS" bash "$RUNNER" "$ROOT10/defaults/scripts" "some-subcommand" 2>/dev/null)"
assert_eq "ON-PATH" "$(field IDENTITY "$out")" \
    "T10: with no repo-local build the harness still resolves loom-daemon on \$PATH (installed consumer repo unaffected)"

# ===========================================================================
# T11-T12: the fatal contracts the harness exists to keep
# ===========================================================================

# ---------- T11. Nothing resolves -> still FATAL, not a skip, and still names
#                 both ways out. ----------
ROOT11="$WORKDIR/t11-repo"; make_fixture_repo "$ROOT11"
out="$(run_harness -- "$ROOT11/defaults/scripts" "some-subcommand" 2>"$WORKDIR/t11-stderr")"
rc=$?
stderr_out="$(cat "$WORKDIR/t11-stderr")"
assert_eq "1" "$rc" "T11: no binary anywhere is still FATAL (exit 1), never a silent skip"
assert_contains "cargo build --package loom-daemon" "$stderr_out" "T11: the fatal message still names the rebuild command"
assert_contains "LOOM_DAEMON_SELF_BIN=" "$stderr_out" "T11: …and still names the explicit-pin escape hatch"

# ---------- T12. A binary that does not know the subcommand -> still FATAL,
#                 with the "predates the port" explanation. ----------
ROOT12="$WORKDIR/t12-repo"; make_fixture_repo "$ROOT12"
TARGET12="$WORKDIR/t12-shared-target"
make_portless_bin "$TARGET12/debug/loom-daemon"
out="$(run_harness CARGO_TARGET_DIR="$TARGET12" -- "$ROOT12/defaults/scripts" "detect-dependency-cycle" 2>"$WORKDIR/t12-stderr")"
rc=$?
stderr_out="$(cat "$WORKDIR/t12-stderr")"
assert_eq "1" "$rc" "T12: a binary predating the port is still FATAL (exit 1)"
assert_contains "does not know the 'detect-dependency-cycle' subcommand" "$stderr_out" \
    "T12: …and the message still names the missing subcommand"

# ===========================================================================
# T13-T15: the stale-binary report (the hazard no resolution rule can fix)
# ===========================================================================

# ---------- T13. A binary older than this checkout's loom-daemon/src warns
#                 loudly, names the newer source file, and still runs — an
#                 mtime cannot distinguish "stale build" from "freshly checked
#                 out worktree", so a hard failure by default would be wrong. ----------
ROOT13="$WORKDIR/t13-repo"; make_fixture_repo "$ROOT13"
TARGET13="$WORKDIR/t13-shared-target"
make_tagged_bin "$TARGET13/debug/loom-daemon" "OLD-BUILD"
touch -t "$OLD_STAMP" "$TARGET13/debug/loom-daemon"
mkdir -p "$ROOT13/loom-daemon/src/dep_recheck"
touch "$ROOT13/loom-daemon/src/dep_recheck/mod.rs"

out="$(run_harness CARGO_TARGET_DIR="$TARGET13" -- "$ROOT13/defaults/scripts" "some-subcommand" 2>"$WORKDIR/t13-stderr")"
rc=$?
stderr_out="$(cat "$WORKDIR/t13-stderr")"
assert_eq "0" "$rc" "T13: a binary older than loom-daemon/src warns but does not fail by default"
assert_eq "OLD-BUILD" "$(field IDENTITY "$out")" "T13: …and the suite still runs against it"
assert_contains "OLDER than this checkout's Rust sources" "$stderr_out" "T13: the stale-binary warning fires"
assert_contains "$ROOT13/loom-daemon/src/dep_recheck/mod.rs" "$stderr_out" \
    "T13: …names the newer source file that triggered it"
assert_contains "cargo build --package loom-daemon" "$stderr_out" "T13: …and names the rebuild that fixes it"

# ---------- T14. LOOM_TEST_DAEMON_BIN_STRICT_FRESHNESS=1 promotes that warning
#                 to fatal, for a caller that knows a build just ran. ----------
out="$(run_harness CARGO_TARGET_DIR="$TARGET13" LOOM_TEST_DAEMON_BIN_STRICT_FRESHNESS=1 \
    -- "$ROOT13/defaults/scripts" "some-subcommand" 2>"$WORKDIR/t14-stderr")"
rc=$?
stderr_out="$(cat "$WORKDIR/t14-stderr")"
assert_eq "1" "$rc" "T14: LOOM_TEST_DAEMON_BIN_STRICT_FRESHNESS=1 makes a stale binary fatal"
assert_contains "STRICT_FRESHNESS" "$stderr_out" "T14: …and the fatal message names the knob that caused it"

# ---------- T15. A binary NEWER than the sources produces no warning at all
#                 (the check must not cry wolf on every run). ----------
ROOT15="$WORKDIR/t15-repo"; make_fixture_repo "$ROOT15"
mkdir -p "$ROOT15/loom-daemon/src"
touch -t "$OLD_STAMP" "$ROOT15/loom-daemon/src/main.rs"
TARGET15="$WORKDIR/t15-shared-target"
make_tagged_bin "$TARGET15/debug/loom-daemon" "FRESH-BUILD"

out="$(run_harness CARGO_TARGET_DIR="$TARGET15" -- "$ROOT15/defaults/scripts" "some-subcommand" 2>"$WORKDIR/t15-stderr")"
stderr_out="$(cat "$WORKDIR/t15-stderr")"
assert_eq "FRESH-BUILD" "$(field IDENTITY "$out")" "T15: a build newer than its sources resolves normally"
assert_not_contains "OLDER than this checkout" "$stderr_out" "T15: …and produces no stale-binary warning"

# ===========================================================================
# T16-T17: the resolution trace, and not leaking snapshots
# ===========================================================================

# ---------- T16. Every resolution reports the path, mtime and a content
#                 fingerprint on stderr — so "which binary did that run test?"
#                 is answerable from a sweep log after the fact — without
#                 contaminating stdout. LOOM_TEST_DAEMON_BIN_QUIET=1 silences
#                 it. ----------
ROOT16="$WORKDIR/t16-repo"; make_fixture_repo "$ROOT16"
TARGET16="$WORKDIR/t16-shared-target"
make_tagged_bin "$TARGET16/debug/loom-daemon" "TRACED"

out="$(run_harness CARGO_TARGET_DIR="$TARGET16" -- "$ROOT16/defaults/scripts" "some-subcommand" 2>"$WORKDIR/t16-stderr")"
stderr_out="$(cat "$WORKDIR/t16-stderr")"
assert_contains "$TARGET16/debug/loom-daemon" "$stderr_out" "T16: the trace names the resolved binary path"
assert_contains "mtime:" "$stderr_out" "T16: …its mtime"
assert_contains "sha256:" "$stderr_out" "T16: …and a content fingerprint (#8176)"
assert_contains "snapshot:" "$stderr_out" "T16: …and where it was snapshotted to"
assert_not_contains "sha256:" "$out" "T16: the trace does not contaminate stdout"

stderr_out="$(run_harness CARGO_TARGET_DIR="$TARGET16" LOOM_TEST_DAEMON_BIN_QUIET=1 \
    -- "$ROOT16/defaults/scripts" "some-subcommand" 2>&1 >/dev/null)"
assert_eq "" "$stderr_out" "T16: LOOM_TEST_DAEMON_BIN_QUIET=1 suppresses the trace"

# ---------- T17. Snapshots are reaped by owning-PID liveness: a dead suite's
#                 snapshot is cleaned up, a LIVE one is never touched (the
#                 reaper must not be able to delete a running suite's binary
#                 out from under it). ----------
sleep 0 &
DEAD_PID=$!
wait "$DEAD_PID" 2>/dev/null
DEAD_DIR="$SNAPSHOT_ROOT/suite-$DEAD_PID-deadxx"
LIVE_DIR="$SNAPSHOT_ROOT/suite-$$-livexx"
mkdir -p "$DEAD_DIR" "$LIVE_DIR"

ROOT17="$WORKDIR/t17-repo"; make_fixture_repo "$ROOT17"
TARGET17="$WORKDIR/t17-shared-target"
make_tagged_bin "$TARGET17/debug/loom-daemon" "REAPER"
run_harness CARGO_TARGET_DIR="$TARGET17" -- "$ROOT17/defaults/scripts" "some-subcommand" >/dev/null 2>&1

if [[ -d "$DEAD_DIR" ]]; then
    fail "T17: a snapshot whose owning shell has exited is reaped"
else
    pass "T17: a snapshot whose owning shell has exited is reaped"
fi
if [[ -d "$LIVE_DIR" ]]; then
    pass "T17: a LIVE suite's snapshot is never reaped"
else
    fail "T17: a LIVE suite's snapshot is never reaped (it was deleted)"
fi

# ---------- summary ----------
echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
