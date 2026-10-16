#!/usr/bin/env bash
# test-resync-installed-payload-fallback.sh - resync-installed.sh's hand-off to
# `loom-daemon resync-payload` when no defaults/ source tree resolves (#8961).
# The resync itself is the daemon's (Rust-tested); this pins the shell half
# against a fake LOOM_DAEMON_BIN: the hand-off happens only when no source
# tree resolves, --dry-run is forwarded, the daemon's exit code is carried
# through, and a missing/older daemon or a refusal still ends in the
# "Could not locate a defaults/ source tree" failure (exit 1).
#
# Usage: ./.loom/scripts/tests/test-resync-installed-payload-fallback.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESYNC="$(cd "$SCRIPT_DIR/.." && pwd)/resync-installed.sh"

TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }

# Physical path: the script hands the daemon the root git reports, which on
# macOS is /private/var/... for a /var/... temp dir.
WORKDIR="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/test-resync-payload.XXXXXX")" && pwd -P)"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# Fake daemon. `resync-payload --help` succeeds unless FAKE_NO_PAYLOAD=1 (an
# older binary: clap's unrecognized-subcommand error, exit 2). A real
# `resync-payload` call logs its argv to FAKE_LOG, prints FAKE_STDOUT /
# FAKE_STDERR and exits FAKE_RC. Every other verb is a silent exit 0.
FAKE="$WORKDIR/bin/loom-daemon"
mkdir -p "$WORKDIR/bin"
cat > "$FAKE" <<'FAKE_EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "resync-payload" ]]; then
    if [[ "${FAKE_NO_PAYLOAD:-0}" == "1" ]]; then
        echo "error: unrecognized subcommand 'resync-payload'" >&2; exit 2
    fi
    [[ " $* " == *" --help "* ]] && exit 0
    printf '%s\n' "$*" >> "${FAKE_LOG:-/dev/null}"
    [[ -n "${FAKE_STDOUT:-}" ]] && printf '%s\n' "$FAKE_STDOUT"
    [[ -n "${FAKE_STDERR:-}" ]] && printf '%s\n' "$FAKE_STDERR" >&2
    exit "${FAKE_RC:-0}"
fi
exit 0
FAKE_EOF
chmod +x "$FAKE"

# A consumer repo exactly as a fresh clone has it: Loom installed, no
# defaults/ tree, no gitignored sidecar, no legacy loom_source in metadata.
make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/.loom/scripts"
    git -C "$repo" init -q
    printf 'STALE\n' > "$repo/.loom/scripts/foo.sh"
    printf '{\n  "loom_version": "0.0.1",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "installed_files": []\n}\n' \
        > "$repo/.loom/install-metadata.json"
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.1" >/dev/null 2>&1
    echo "$repo"
}

LOG="$WORKDIR/argv.log"
run() { # run <args...>; env FAKE_* inherited. Sets REPO, OUT, RC; resets LOG.
    REPO="$(make_fixture)"; : > "$LOG"
    OUT="$(cd "$REPO" && FAKE_LOG="$LOG" LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" "$@" 2>&1)"; RC=$?
}

echo "Case 1: no source tree + capable daemon -> hands off, exit 0"
FAKE_STDOUT="resync-payload: 3 file(s) written" run
if [[ $RC -eq 0 && "$OUT" == *"3 file(s) written"* && "$OUT" != *"Could not locate"* ]]; then
    pass "daemon output shown, exit 0, no source-tree error"
else fail "rc=$RC out=$OUT"; fi
if [[ "$(cat "$LOG")" == "resync-payload --workspace $REPO" ]]; then
    pass "called once with --workspace <repo root> and no --dry-run"
else fail "argv log: $(cat "$LOG")"; fi

echo "Case 2: --dry-run is forwarded and the drift exit code is carried"
FAKE_RC=2 FAKE_STDOUT="  would update .loom/scripts/foo.sh" run --dry-run
if [[ $RC -eq 2 && "$OUT" == *"would update .loom/scripts/foo.sh"* ]]; then
    pass "exit 2 passed through, preview shown"
else fail "rc=$RC out=$OUT"; fi
if [[ "$(cat "$LOG")" == "resync-payload --workspace $REPO --dry-run" ]]; then
    pass "--dry-run forwarded"
else fail "argv log: $(cat "$LOG")"; fi
if [[ -z "$(git -C "$REPO" status --porcelain)" ]]; then
    pass "the script itself wrote nothing on the dry run"
else fail "tree dirty: $(git -C "$REPO" status --porcelain)"; fi
FAKE_RC=0 run --dry-run
if [[ $RC -eq 0 ]]; then pass "--dry-run in sync exits 0"; else fail "rc=$RC out=$OUT"; fi

echo "Case 3: the daemon refuses (never a downgrade) -> exit 1, reason shown"
FAKE_RC=1 FAKE_STDERR="resync-payload: refused, nothing written: repo ahead of daemon" run
if [[ $RC -eq 1 && "$OUT" == *"refused, nothing written: repo ahead of daemon"* ]]; then
    pass "refusal printed, exit 1"
else fail "rc=$RC out=$OUT"; fi
if [[ "$OUT" == *"Could not locate a defaults/ source tree"* && "$OUT" == *"refused or failed"* ]]; then
    pass "source-tree error still printed, and says the fallback refused"
else fail "out=$OUT"; fi
if [[ -z "$(git -C "$REPO" status --porcelain)" ]]; then pass "nothing written"; else fail "tree dirty"; fi

echo "Case 4: older daemon (no resync-payload) -> today's failure, never called"
FAKE_NO_PAYLOAD=1 run
if [[ $RC -eq 1 && "$OUT" == *"Could not locate a defaults/ source tree"* ]]; then
    pass "exit 1 with the source-tree error"
else fail "rc=$RC out=$OUT"; fi
if [[ "$OUT" == *".loom/loom-source-path"* && "$OUT" == *"missing or too old"* && "$OUT" == *"$FAKE"* ]]; then
    pass "names the missing sidecar and the too-old daemon it looked at"
else fail "out=$OUT"; fi
if [[ "$OUT" != *"unrecognized subcommand"* && ! -s "$LOG" ]]; then
    pass "no clap error leaked, resync-payload never run"
else fail "out=$OUT log=$(cat "$LOG")"; fi

echo "Case 5: no loom-daemon anywhere -> today's failure"
REPO="$(make_fixture)"
OUT="$(cd "$REPO" && env -u LOOM_DAEMON_BIN PATH="/usr/bin:/bin" HOME="$WORKDIR/nohome" \
    LOOM_DAEMON_BIN_DIR="/nonexistent" bash "$RESYNC" 2>&1)"; RC=$?
if [[ $RC -eq 1 && "$OUT" == *"Could not locate a defaults/ source tree"* \
    && "$OUT" == *".loom/loom-source-path"* && "$OUT" == *"missing or too old"* ]]; then
    pass "exit 1, names the missing sidecar and the missing daemon"
else fail "rc=$RC out=$OUT"; fi

echo "Case 6: loom-daemon found on PATH (no LOOM_DAEMON_BIN) is used too"
REPO="$(make_fixture)"; : > "$LOG"
OUT="$(cd "$REPO" && env -u LOOM_DAEMON_BIN FAKE_LOG="$LOG" PATH="$WORKDIR/bin:/usr/bin:/bin" \
    bash "$RESYNC" 2>&1)"; RC=$?
if [[ $RC -eq 0 && "$(cat "$LOG")" == "resync-payload --workspace $REPO" ]]; then
    pass "PATH daemon handed the resync"
else fail "rc=$RC out=$OUT log=$(cat "$LOG")"; fi

echo "Case 7: a source tree resolves -> the daemon fallback is never invoked"
REPO="$(make_fixture)"; : > "$LOG"
mkdir -p "$REPO/defaults/scripts"
printf 'FRESH\n' > "$REPO/defaults/scripts/foo.sh"
printf '{\n  "version": "9.9.9"\n}\n' > "$REPO/package.json"
git -C "$REPO" add -A >/dev/null 2>&1 && git -C "$REPO" commit -qm "add source" >/dev/null 2>&1
OUT="$(cd "$REPO" && FAKE_LOG="$LOG" LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" 2>&1)"; RC=$?
if [[ ! -s "$LOG" && "$(cat "$REPO/.loom/scripts/foo.sh")" == "FRESH" ]]; then
    pass "dogfood rung synced from defaults/, resync-payload not called"
else fail "rc=$RC log=$(cat "$LOG") out=$OUT"; fi
SRC="$WORKDIR/loom-src"; rm -rf "$SRC"; mkdir -p "$SRC/defaults/scripts"
printf 'SIDECAR\n' > "$SRC/defaults/scripts/foo.sh"
printf '{\n  "version": "9.9.9"\n}\n' > "$SRC/package.json"
REPO="$(make_fixture)"; : > "$LOG"
printf '%s\n' "$SRC" > "$REPO/.loom/loom-source-path"
OUT="$(cd "$REPO" && FAKE_LOG="$LOG" LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" 2>&1)"; RC=$?
if [[ ! -s "$LOG" && "$(cat "$REPO/.loom/scripts/foo.sh")" == "SIDECAR" ]]; then
    pass "sidecar rung synced from the recorded clone, resync-payload not called"
else fail "rc=$RC log=$(cat "$LOG") out=$OUT"; fi

echo "Case 8: --output staging never falls back (the daemon writes the checkout itself)"
run --output "$WORKDIR/staging"
if [[ $RC -eq 1 && "$OUT" == *"Could not locate a defaults/ source tree"* && ! -s "$LOG" ]]; then
    pass "exit 1, resync-payload not called"
else fail "rc=$RC out=$OUT log=$(cat "$LOG")"; fi

# The real subcommand, when a binary that has it is around. The fixture is
# AHEAD of any daemon, so every build refuses it (a dev build as "not a
# release build", a release build as "repo ahead of daemon") and nothing is
# written. Skipped, not failed, without such a binary: CI runs these suites
# without a built daemon.
echo "Case 9: a real loom-daemon refuses a repo ahead of it and writes nothing"
REAL=""
LOOM_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
for candidate in "${LOOM_DAEMON_BIN:-}" "${CARGO_TARGET_DIR:+$CARGO_TARGET_DIR/debug/loom-daemon}" \
    "$LOOM_ROOT/target/debug/loom-daemon" "$LOOM_ROOT/target/release/loom-daemon" \
    "$(command -v loom-daemon 2>/dev/null || true)"; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if "$candidate" resync-payload --help >/dev/null 2>&1; then REAL="$candidate"; break; fi
done
if [[ -z "$REAL" ]]; then
    echo "  SKIP: no loom-daemon with resync-payload resolved (set LOOM_DAEMON_BIN to run)"
else
    REPO="$(make_fixture)"
    printf '{\n  "loom_version": "999.0.0",\n  "installed_files": []\n}\n' > "$REPO/.loom/install-metadata.json"
    git -C "$REPO" commit -qam "ahead" >/dev/null 2>&1
    for mode in "" "--dry-run"; do
        # shellcheck disable=SC2086  # $mode is deliberately empty or one flag
        OUT="$(cd "$REPO" && LOOM_DAEMON_BIN="$REAL" bash "$RESYNC" $mode 2>&1)"; RC=$?
        if [[ $RC -eq 1 && "$OUT" == *"refused, nothing written"* \
            && "$OUT" == *"Could not locate a defaults/ source tree"* \
            && -z "$(git -C "$REPO" status --porcelain)" ]]; then
            pass "real daemon ($REAL) refused${mode:+ under $mode}, tree unchanged"
        else fail "mode=$mode rc=$RC out=$OUT status=$(git -C "$REPO" status --porcelain)"; fi
    done
fi

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
