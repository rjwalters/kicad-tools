#!/usr/bin/env bash
# test-resync-installed-retired-migrate.sh - resync-installed.sh's retired
# forge label migration step (#11511), hermetic with a fake LOOM_DAEMON_BIN.
#
# Deleting a forge label cannot be undone, and resync's forge writes are
# opt-in (#10770), so resync may only PREVIEW the migration. This pins:
#   1. Every resync (real and --dry-run) calls `labels migrate` WITHOUT
#      --apply -- never with it.
#   2. A pending report (exit 3) is printed with the exact apply command.
#   3. Nothing pending (exit 0) prints nothing.
#   4. An older daemon without the verb is skipped, and resync still exits 0.
#   5. A repo without .github/labels.yml is not migrated.
#
# Usage: ./.loom/scripts/tests/test-resync-installed-retired-migrate.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESYNC="$(cd "$SCRIPT_DIR/.." && pwd)/resync-installed.sh"

TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-migrate.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# Fake daemon: `labels migrate --help` succeeds unless FAKE_NO_VERB=1; a real
# `labels migrate` logs argv to FAKE_LOG, prints FAKE_STDOUT, exits FAKE_RC.
# Everything else (update-gitignore, forge egress, ...) succeeds silently.
FAKE="$WORKDIR/fake-loom-daemon"
cat > "$FAKE" <<'FAKE_EOF'
#!/usr/bin/env bash
if [[ "$1 $2" == "labels migrate" ]]; then
    if [[ " $* " == *" --help "* ]]; then
        [[ "${FAKE_NO_VERB:-0}" == "1" ]] && { echo "error: unrecognized subcommand 'migrate'" >&2; exit 2; }
        exit 0
    fi
    printf '%s\n' "$*" >> "${FAKE_LOG:-/dev/null}"
    [[ -n "${FAKE_STDOUT:-}" ]] && printf '%s\n' "$FAKE_STDOUT"
    exit "${FAKE_RC:-0}"
fi
exit 0
FAKE_EOF
chmod +x "$FAKE"

make_fixture() { # make_fixture [no-labels]
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/scripts/lib" "$repo/.loom/scripts/lib" "$repo/.github"
    git -C "$repo" init -q
    printf 'S\n' > "$repo/defaults/scripts/foo.sh"
    chmod +x "$repo/defaults/scripts/foo.sh"
    printf 'S\n' > "$repo/.loom/scripts/foo.sh"
    [[ "${1:-}" == "no-labels" ]] || printf '# BEGIN LOOM LABELS\n# END LOOM LABELS\n' > "$repo/.github/labels.yml"
    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
    echo "$repo"
}

FIXTURE_KIND=""
run() { # run <args...>; env FAKE_* inherited. Sets OUT, RC, LOG.
    local repo; repo="$(make_fixture "$FIXTURE_KIND")"
    LOG="$WORKDIR/argv.log"; : > "$LOG"
    OUT="$(cd "$repo" && FAKE_LOG="$LOG" LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" "$@" 2>&1)"; RC=$?
}

PENDING="RELABEL #7 tier:maintenance -> none"

echo "Case 1: real resync with pending work previews, never applies"
FAKE_RC=3 FAKE_STDOUT="$PENDING" run
if [[ $RC -eq 0 ]] && grep -qx "labels migrate" "$LOG" && ! grep -q -- "--apply" "$LOG"; then
    pass "migrate called without --apply, resync exits 0"
else fail "rc=$RC log=$(cat "$LOG") out=$OUT"; fi
if [[ "$OUT" == *"$PENDING"* && "$OUT" == *"loom-daemon labels migrate --apply"* ]]; then
    pass "pending report and the apply command are printed"
else fail "out=$OUT"; fi

echo "Case 2: --dry-run resync previews too, never applies"
FAKE_RC=3 FAKE_STDOUT="$PENDING" run --dry-run
if [[ $RC -eq 0 ]] && grep -qx "labels migrate" "$LOG" && ! grep -q -- "--apply" "$LOG"; then
    pass "dry-run resync calls migrate without --apply"
else fail "rc=$RC log=$(cat "$LOG")"; fi

echo "Case 3: nothing pending prints nothing"
FAKE_RC=0 FAKE_STDOUT="ABSENT tier:maintenance" run
if [[ $RC -eq 0 && "$OUT" != *"Retired forge labels"* && "$OUT" != *"--apply"* ]]; then
    pass "silent when nothing is pending"
else fail "rc=$RC out=$OUT"; fi

echo "Case 4: older daemon without the verb"
FAKE_NO_VERB=1 FAKE_RC=3 run
if [[ $RC -eq 0 && ! -s "$LOG" ]]; then
    pass "skipped quietly, exit 0"
else fail "rc=$RC log=$(cat "$LOG")"; fi

echo "Case 5: a repo without .github/labels.yml"
FIXTURE_KIND="no-labels" FAKE_RC=3 run
if [[ $RC -eq 0 && ! -s "$LOG" ]]; then
    pass "not migrated"
else fail "rc=$RC log=$(cat "$LOG")"; fi

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
