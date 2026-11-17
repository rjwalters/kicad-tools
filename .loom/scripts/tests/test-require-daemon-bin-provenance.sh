#!/usr/bin/env bash
# test-require-daemon-bin-provenance.sh — tests/lib/require-daemon-bin.sh's
# "is this binary built from THIS checkout?" rule (#10662).
#
# The regressions under test: a shell suite passing against the INSTALLED
# loom-daemon on $PATH because the checkout had no build of its own, and a
# suite picking another checkout's build out of a shared target dir because it
# was the freshest file there. Both hid real failures that only CI caught.
#
# Hermetic: the fixture is a throwaway git repo carrying a stub
# `loom-daemon/Cargo.toml` (which is what makes the harness enforce the rule),
# and every "binary" is a shell script that prints a `--version` line naming
# whatever source commit the case wants. No Rust toolchain is needed.
#
# Usage:
#   ./defaults/scripts/tests/test-require-daemon-bin-provenance.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$SCRIPT_DIR/lib/require-daemon-bin.sh"
LOCATE_LIB="$SCRIPT_DIR/../lib/locate-daemon-bin.sh"

TESTS_RUN=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }
assert_eq() { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$1', got '$2')"; fi; }
assert_contains() { if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3 (expected '$1' in [$2])"; fi; }
assert_not_contains() { if [[ "$2" != *"$1"* ]]; then pass "$3"; else fail "$3 (did NOT expect '$1' in [$2])"; fi; }

WORKDIR="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/test-rdb-provenance.XXXXXX")" && pwd)"
trap 'rm -rf "$WORKDIR"' EXIT
mkdir -p "$WORKDIR/tmp" "$WORKDIR/home"

# The fixture checkout: <repo>/defaults/scripts is the harness's scripts_dir.
REPO="$WORKDIR/repo"
mkdir -p "$REPO/defaults/scripts/lib" "$REPO/loom-daemon"
cp "$LOCATE_LIB" "$REPO/defaults/scripts/lib/locate-daemon-bin.sh"
printf '[package]\nname = "loom-daemon"\n' > "$REPO/loom-daemon/Cargo.toml"
git -C "$REPO" init -q
git -C "$REPO" add -A
git -C "$REPO" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm fixture
HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"
OTHER_SHA="0123456789abcdef0123456789abcdef01234567"

# fake_bin <path> <version-line>: answers --version with <version-line> and any
# `<sub> --help` preflight with exit 0.
fake_bin() {
    mkdir -p "$(dirname "$1")"
    printf '#!/usr/bin/env bash\n[[ "${1:-}" == --version ]] && { echo "%s"; exit 0; }\nexit 0\n' "$2" > "$1"
    chmod +x "$1"
}
ver() { echo "loom-daemon 0.19.0 (commit ${1:0:9}, built 2026-10-06T00:00:00Z, source $1 $2)"; }

RUNNER="$WORKDIR/runner.sh"
cat > "$RUNNER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1090
source "$LOOM_TEST_HARNESS"
# shellcheck disable=SC1090
[[ -z "${RACE_HOOK:-}" ]] || source "$RACE_HOOK"
loom_test_require_daemon_bin "$@"
loom_test_require_daemon_bin "$@"
loom_test_skip "fixture skip"
echo "SKIPPED=$TESTS_SKIPPED"
echo "ON_PATH=$(command -v loom-daemon || true)"
echo "SELF_BIN=$LOOM_DAEMON_SELF_BIN"
EOF

# run <ENV=VAL ...> -- <harness args...>: sets $out (stdout+stderr) and $rc.
run() {
    local envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift
    rc=0
    out="$(env -i PATH="$WORKDIR/installed:/usr/bin:/bin" HOME="$WORKDIR/home" TMPDIR="$WORKDIR/tmp" \
        LOOM_TEST_HARNESS="$HARNESS" ${envs[@]+"${envs[@]}"} bash "$RUNNER" "$@" 2>&1)" || rc=$?
}

# An installed release on $PATH, built from some other commit.
fake_bin "$WORKDIR/installed/loom-daemon" "$(ver "$OTHER_SHA" clean)"
SCRIPTS="$REPO/defaults/scripts"

echo "P1: an installed loom-daemon on PATH and no build for the checkout"
run CARGO_TARGET_DIR="$WORKDIR/none" -- "$SCRIPTS" sub
assert_eq "1" "$rc" "the suite fails instead of testing the installed binary"
assert_contains "no loom-daemon build for this checkout" "$out" "…saying there is no build for the checkout"
assert_contains "NOT used: the installed $WORKDIR/installed/loom-daemon" "$out" "…and naming the installed binary it refused"
assert_not_contains "daemon under test:" "$out" "…without ever reporting a daemon under test"

echo "P2: a stale build from another checkout in a shared target dir"
SHARED="$WORKDIR/shared"
fake_bin "$SHARED/release/loom-daemon" "$(ver "$OTHER_SHA" clean)"
run CARGO_TARGET_DIR="$SHARED" -- "$SCRIPTS" sub
assert_eq "1" "$rc" "the stale build is rejected"
assert_contains "not this checkout's: $SHARED/release/loom-daemon" "$out" "…and named with its own commit"
assert_contains "$OTHER_SHA" "$out" "…which the message prints"

echo "P3: a matching build beside a FRESHER stale one"
fake_bin "$SHARED/debug/loom-daemon" "$(ver "$HEAD_SHA" clean)"
touch -t 202601010101 "$SHARED/debug/loom-daemon"
run CARGO_TARGET_DIR="$SHARED" -- --path "$SCRIPTS" sub
assert_eq "0" "$rc" "the matching build is accepted"
assert_contains "daemon under test: $SHARED/debug/loom-daemon ($(ver "$HEAD_SHA" clean)) — source matches HEAD" "$out" \
    "…the matching build wins over the fresher stale one, and the line names it"
lines="$(printf '%s\n' "$out" | grep -c '^daemon under test:' || true)"
assert_eq "1" "$lines" "the line is printed once, though the harness ran twice"
assert_contains "ON_PATH=$(dirname "$(printf '%s\n' "$out" | sed -n 's/^SELF_BIN=//p')")/loom-daemon" "$out" \
    "--path puts the pinned binary first on PATH, ahead of the installed one"

echo "P4: a dirty-tree build at the same HEAD"
fake_bin "$SHARED/debug/loom-daemon" "$(ver "$HEAD_SHA" dirty)"
run CARGO_TARGET_DIR="$SHARED" -- "$SCRIPTS" sub
assert_eq "0" "$rc" "a dirty build at HEAD is accepted"
assert_contains "source matches HEAD (built from a dirty tree)" "$out" "…and the line says dirty"

echo "P5: an explicit LOOM_DAEMON_SELF_BIN is checked too"
run CARGO_TARGET_DIR="$SHARED" LOOM_DAEMON_SELF_BIN="$WORKDIR/installed/loom-daemon" -- --self-only "$SCRIPTS" sub
assert_eq "1" "$rc" "a pin built from another commit fails"
assert_contains "source $OTHER_SHA is NOT this checkout's HEAD $HEAD_SHA" "$out" "…naming both commits"

echo "P6: a binary that reports no source commit"
fake_bin "$WORKDIR/old/loom-daemon" "loom-daemon 0.10.0"
run CARGO_TARGET_DIR="$SHARED" LOOM_DAEMON_SELF_BIN="$WORKDIR/old/loom-daemon" -- "$SCRIPTS" sub
assert_eq "1" "$rc" "it counts as a mismatch"
assert_contains "reports NO source commit; this checkout's HEAD is $HEAD_SHA" "$out" "…and says so"

echo "P7: LOOM_TEST_ALLOW_DAEMON_MISMATCH=1"
rm -f "$SHARED/debug/loom-daemon"
run CARGO_TARGET_DIR="$SHARED" LOOM_TEST_ALLOW_DAEMON_MISMATCH=1 -- "$SCRIPTS" sub
assert_eq "0" "$rc" "the override accepts the other checkout's build"
assert_contains "allowed by LOOM_TEST_ALLOW_DAEMON_MISMATCH=1" "$out" "…and the line says it was allowed"
run CARGO_TARGET_DIR="$WORKDIR/none" LOOM_TEST_ALLOW_DAEMON_MISMATCH=1 -- "$SCRIPTS" sub
assert_eq "1" "$rc" "…but never re-enables the installed binary on PATH"

echo "P10: the shared file is replaced between the check and the snapshot"
# The #8176 clobber, aimed at the provenance check: the hook wraps a step the
# harness runs AFTER choosing the candidate and BEFORE copying it, and uses it
# to overwrite the shared file with another commit's build — what a concurrent
# `cargo build` in another worktree does. The verdict must be the copy's.
RACED="$WORKDIR/raced"
fake_bin "$RACED/debug/loom-daemon" "$(ver "$HEAD_SHA" clean)"
fake_bin "$WORKDIR/foreign/loom-daemon" "$(ver "$OTHER_SHA" clean)"
cat > "$WORKDIR/race-hook.sh" <<EOF
_loom_test_daemon_bin_fingerprint() { cp "$WORKDIR/foreign/loom-daemon" "\$1"; echo raced; }
EOF
run CARGO_TARGET_DIR="$RACED" RACE_HOOK="$WORKDIR/race-hook.sh" -- "$SCRIPTS" sub
assert_eq "1" "$rc" "the harness fails when the file it chose was replaced before the copy"
assert_contains "daemon under test: $RACED/debug/loom-daemon ($(ver "$OTHER_SHA" clean)) — source $OTHER_SHA is NOT this checkout's HEAD $HEAD_SHA" "$out" \
    "…reporting the commit the pinned copy actually carries"
assert_contains "private copy of candidate $RACED/debug/loom-daemon" "$out" "…and that it checked the private copy of that candidate"
assert_not_contains "source matches HEAD" "$out" "…never certifying the file it checked before the swap"

echo "P8: skips are announced and counted"
run CARGO_TARGET_DIR="$SHARED" LOOM_TEST_ALLOW_DAEMON_MISMATCH=1 -- "$SCRIPTS" sub
assert_contains "  SKIP: fixture skip" "$out" "loom_test_skip prints SKIP: <reason>"
assert_contains "SKIPPED=1" "$out" "…and counts it in TESTS_SKIPPED"

echo "P9: a checkout with no daemon source (an installed consumer repo)"
CONSUMER="$WORKDIR/consumer"
mkdir -p "$CONSUMER/defaults/scripts/lib"
cp "$LOCATE_LIB" "$CONSUMER/defaults/scripts/lib/locate-daemon-bin.sh"
run CARGO_TARGET_DIR="$WORKDIR/none" -- "$CONSUMER/defaults/scripts" sub
assert_eq "0" "$rc" "still resolves the installed binary, as before"
assert_contains "not checked: this checkout carries no loom-daemon source" "$out" "…and the line says it was not checked"

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
