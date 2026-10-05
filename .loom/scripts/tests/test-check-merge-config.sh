#!/usr/bin/env bash
# test-check-merge-config.sh - check-merge-config.sh's stub contract and its
# resync-installed.sh wiring (#9287).
#
# The decision logic (effective merge-method set, the three findings, ruleset
# intersection, "could not determine" on a 403) is `loom-daemon forge
# merge-config` and is tested in Rust (loom-daemon/src/forge_merge_config_tests.rs).
# This suite pins the SHELL half, hermetically, with a fake LOOM_DAEMON_BIN —
# no real binary, no gh, no network:
#
#   (a) the stub always exits 0: no binary, a binary that predates the
#       subcommand, and a binary that crashes all skip rather than fail;
#   (b) the stub forwards its arguments to `forge merge-config` verbatim;
#   (c) resync-installed.sh shows the check's stdout (findings), stays silent
#       when it prints nothing, never lets it change the resync exit code, and
#       drops its stderr skip notes.
#
# Usage:
#   ./.loom/scripts/tests/test-check-merge-config.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STUB="$HELPERS_DIR/check-merge-config.sh"
RESYNC="$HELPERS_DIR/resync-installed.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-config.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# A fake loom-daemon. `--help` succeeds unless FAKE_NO_SUBCOMMAND=1; a real
# call logs its argv to FAKE_LOG, prints FAKE_STDOUT, and exits FAKE_RC.
FAKE="$WORKDIR/fake-loom-daemon"
cat > "$FAKE" <<'FAKE_EOF'
#!/usr/bin/env bash
[[ "$1 $2" == "forge egress" ]] && exit 0   # #9996: resync's egress doctor is not under test here
if [[ " $* " == *" --help "* ]]; then
    [[ "${FAKE_NO_SUBCOMMAND:-0}" == "1" ]] && { echo "error: unrecognized subcommand 'merge-config'" >&2; exit 2; }
    exit 0
fi
printf '%s\n' "$*" >> "${FAKE_LOG:-/dev/null}"
[[ -n "${FAKE_STDOUT:-}" ]] && printf '%s\n' "$FAKE_STDOUT"
exit "${FAKE_RC:-0}"
FAKE_EOF
chmod +x "$FAKE"

echo "Test group 1: stub always exits 0"

OUT="$(LOOM_DAEMON_BIN="$WORKDIR/does-not-exist" bash "$STUB" 2>&1)"; RC=$?
if [[ $RC -eq 0 && "$OUT" == *"loom-daemon not found"* ]]; then
    pass "missing binary -> skipped, exit 0"
else
    fail "missing binary: rc=$RC out=$OUT"
fi

OUT="$(LOOM_DAEMON_BIN="$WORKDIR/does-not-exist" bash "$STUB" 2>/dev/null)"
if [[ -z "$OUT" ]]; then
    pass "the skip note is on stderr, not stdout"
else
    fail "skip note leaked to stdout: $OUT"
fi

OUT="$(FAKE_NO_SUBCOMMAND=1 LOOM_DAEMON_BIN="$FAKE" bash "$STUB" 2>&1)"; RC=$?
if [[ $RC -eq 0 && "$OUT" == *"predates"* ]]; then
    pass "a binary without 'forge merge-config' -> skipped, exit 0"
else
    fail "older binary: rc=$RC out=$OUT"
fi

echo "Test group 2: stub forwards to 'forge merge-config'"

LOG="$WORKDIR/argv.log"; : > "$LOG"
OUT="$(FAKE_LOG="$LOG" FAKE_STDOUT="merge-config: WARNING [EMPTY_EFFECTIVE_SET] x" LOOM_DAEMON_BIN="$FAKE" \
    bash "$STUB" --verbose --repo acme/widgets --branch main 2>&1)"; RC=$?
if [[ $RC -eq 0 && "$(cat "$LOG")" == "forge merge-config --verbose --repo acme/widgets --branch main" ]]; then
    pass "arguments forwarded verbatim"
else
    fail "forwarding: rc=$RC argv=$(cat "$LOG")"
fi
if [[ "$OUT" == *"EMPTY_EFFECTIVE_SET"* ]]; then
    pass "the subcommand's findings reach stdout"
else
    fail "findings not shown: $OUT"
fi

# --- resync wiring ----------------------------------------------------------
# Trimmed fixture (same shape as test-resync-installed-guard-check.sh's):
# just enough of a defaults/ + .loom/ tree for resync-installed.sh to finish.
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
    # The real stub, driven by the fake binary via LOOM_DAEMON_BIN.
    cp "$STUB" "$repo/defaults/scripts/check-merge-config.sh"
    chmod +x "$repo/defaults/scripts/check-merge-config.sh"
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
    echo "$repo"
}

echo "Test group 3: resync-installed.sh runs the check, advisory only"

REPO="$(make_fixture)"
LOG="$WORKDIR/resync-argv.log"; : > "$LOG"
OUT="$(cd "$REPO" && FAKE_LOG="$LOG" FAKE_STDOUT="merge-config: WARNING [EMPTY_EFFECTIVE_SET] no merge method is permitted" \
    LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" 2>&1)"; RC=$?
if grep -q "forge merge-config" "$LOG"; then
    pass "resync invokes the merge-config check"
else
    fail "resync never invoked the check (argv log: $(cat "$LOG"))"
fi
if [[ "$OUT" == *"Forge merge configuration"* && "$OUT" == *"EMPTY_EFFECTIVE_SET"* ]]; then
    pass "a finding is shown under its own heading"
else
    fail "finding not shown; out=$OUT"
fi
if [[ $RC -eq 0 ]]; then
    pass "a finding does not fail the resync"
else
    fail "resync exited $RC on a merge-config finding"
fi

REPO="$(make_fixture)"
OUT="$(cd "$REPO" && LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" 2>&1)"; RC=$?
if [[ $RC -eq 0 && "$OUT" != *"Forge merge configuration"* && "$OUT" != *"merge-config: "* ]]; then
    pass "nothing to report -> no new resync output"
else
    fail "silent check produced output or failed (rc=$RC); out=$OUT"
fi

REPO="$(make_fixture)"
OUT="$(cd "$REPO" && FAKE_RC=7 LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" 2>&1)"; RC=$?
if [[ $RC -eq 0 ]]; then
    pass "a crashing check never changes the resync exit code"
else
    fail "resync exited $RC when the check crashed; out=$OUT"
fi

REPO="$(make_fixture)"
OUT="$(cd "$REPO" && LOOM_DAEMON_BIN="$WORKDIR/does-not-exist" bash "$RESYNC" 2>&1)"; RC=$?
if [[ $RC -eq 0 && "$OUT" != *"Forge merge configuration"* && "$OUT" != *"merge-config: "* ]]; then
    pass "no loom-daemon -> the stderr skip note is dropped, resync unaffected"
else
    fail "missing binary leaked into resync (rc=$RC); out=$OUT"
fi

echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
