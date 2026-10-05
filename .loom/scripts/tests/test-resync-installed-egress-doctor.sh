#!/usr/bin/env bash
# test-resync-installed-egress-doctor.sh - resync-installed.sh's `forge egress
# doctor` step (#9996), hermetic with a fake LOOM_DAEMON_BIN. The routing
# verdict is the daemon's (Rust-tested); this pins the shell half: output is
# shown, work completes first, the doctor's exit code is carried through,
# --dry-run and an older daemon never fail.
#
# Usage: ./.loom/scripts/tests/test-resync-installed-egress-doctor.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESYNC="$(cd "$SCRIPT_DIR/.." && pwd)/resync-installed.sh"

TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-egress.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# Fake daemon: `forge egress --help` succeeds unless FAKE_NO_EGRESS=1; a real
# `forge egress doctor` logs argv to FAKE_LOG, prints FAKE_STDOUT, exits FAKE_RC.
FAKE="$WORKDIR/fake-loom-daemon"
cat > "$FAKE" <<'FAKE_EOF'
#!/usr/bin/env bash
if [[ "$1 $2" == "forge egress" ]]; then
    if [[ " $* " == *" --help "* ]]; then
        [[ "${FAKE_NO_EGRESS:-0}" == "1" ]] && { echo "error: unrecognized subcommand 'egress'" >&2; exit 2; }
        exit 0
    fi
    printf '%s\n' "$*" >> "${FAKE_LOG:-/dev/null}"
    [[ -n "${FAKE_STDOUT:-}" ]] && printf '%s\n' "$FAKE_STDOUT"
    exit "${FAKE_RC:-0}"
fi
exit 0
FAKE_EOF
chmod +x "$FAKE"

make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/scripts/lib" "$repo/.loom/scripts/lib"
    git -C "$repo" init -q
    printf 'S\n' > "$repo/defaults/scripts/foo.sh"
    chmod +x "$repo/defaults/scripts/foo.sh"
    printf 'S\n' > "$repo/.loom/scripts/foo.sh"
    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
    echo "$repo"
}

run() { # run <args...>; env FAKE_* inherited. Sets OUT, RC, LOG.
    local repo; repo="$(make_fixture)"
    LOG="$WORKDIR/argv.log"; : > "$LOG"
    OUT="$(cd "$repo" && FAKE_LOG="$LOG" LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" "$@" 2>&1)"; RC=$?
}

echo "Case 1: no policy (doctor silent, exit 0)"
run
if [[ $RC -eq 0 && "$OUT" != *"Forge egress"* ]] && grep -q "forge egress doctor" "$LOG"; then
    pass "doctor runs, no egress output, exit 0"
else fail "rc=$RC out=$OUT"; fi

echo "Case 2: required + finding (deferred failure)"
FAKE_RC=1 FAKE_STDOUT="FINDING [ROUTE_MISMATCH] x" run
if [[ $RC -eq 1 && "$OUT" == *"ROUTE_MISMATCH"* && "$OUT" == *"[resync]"*"in sync"* ]]; then
    pass "codes printed, summary still shown, doctor's exit code carried"
else fail "rc=$RC out=$OUT"; fi
FAKE_RC=2 FAKE_STDOUT="x" run
[[ $RC -eq 2 ]] && pass "exit 2 passed through unchanged" || fail "rc=$RC"

echo "Case 3: observe (findings, exit 0)"
FAKE_RC=0 FAKE_STDOUT="FINDING [ROUTE_MISMATCH] x" run
if [[ $RC -eq 0 && "$OUT" == *"ROUTE_MISMATCH"* ]]; then pass "printed, exit 0"; else fail "rc=$RC out=$OUT"; fi

echo "Case 4: older daemon"
FAKE_NO_EGRESS=1 FAKE_RC=1 run
if [[ $RC -eq 0 && "$OUT" == *"forge egress check unavailable"* ]] && ! grep -q "forge egress doctor" "$LOG"; then
    pass "warns, exit 0, doctor never called"
else fail "rc=$RC out=$OUT log=$(cat "$LOG")"; fi

echo "Case 5: --dry-run"
FAKE_RC=1 FAKE_STDOUT="x" run --dry-run
if [[ $RC -eq 0 ]] && ! grep -q "forge egress doctor" "$LOG"; then
    pass "doctor not run, exit 0"
else fail "rc=$RC out=$OUT"; fi

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
