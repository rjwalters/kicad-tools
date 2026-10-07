#!/usr/bin/env bash
# test-resync-installed-host-optout.sh - the `loom-daemon host check` guard in
# resync-installed.sh (#10179)
#
# Split out of test-resync-installed.sh (frozen by the file-size ratchet,
# .loom/docs/file-size-policy.md) rather than grown in place.
#
# resync-installed.sh asks the PATH `loom-daemon` whether this host opted out
# (`loom-daemon host disable`). It must refuse ONLY on the affirmative disabled
# signal (exit 10); an older binary that predates `host` exits 1 (or clap's 2)
# for the unknown subcommand and must fall through to normal resolver handling
# -- the stale-binary fixtures in test-resync-installed.sh depend on that.
# The fakes here are scriptable stand-ins, not the real binary; the real
# binary's exit 10 is pinned by loom-daemon/tests/host_optout_paths.rs.
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-host-optout.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/resync-installed.sh"

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-optout.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder (trimmed copy of test-resync-installed.sh's) -----------
# .loom/hooks/guard.sh drifts (OLD -> A): a run that proceeds rewrites it, a
# refused run must leave it untouched.
make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/hooks" "$repo/defaults/scripts/lib" \
             "$repo/.loom/hooks" "$repo/.loom/scripts/lib"
    git -C "$repo" init -q

    printf 'A\n' > "$repo/defaults/hooks/guard.sh"
    printf 'S\n' > "$repo/defaults/scripts/foo.sh"
    chmod +x "$repo/defaults/hooks/guard.sh" "$repo/defaults/scripts/foo.sh"
    printf 'OLD\n' > "$repo/.loom/hooks/guard.sh"
    printf 'S\n'   > "$repo/.loom/scripts/foo.sh"

    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"

    # Subject matches resync's safe-lineage pattern so the guard.sh drift
    # resyncs instead of being held as a local fix (#7864).
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1

    echo "$repo"
}

# make_fake_daemon <dir> <host-rc>: a PATH `loom-daemon` whose `host ...`
# exits <host-rc> (logging the call) and that no-ops `update-gitignore`.
make_fake_daemon() {
    local dir="$1" host_rc="$2"
    mkdir -p "$dir"
    cat > "$dir/loom-daemon" <<FAKE_EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "host" ]]; then
    printf '%s\n' "\$*" >> "$dir/host.log"
    if [[ $host_rc -eq 10 ]]; then
        echo "loom-daemon: \${4:-host check} refused: host disabled by operator (reason: cost freeze; who: alice; when: 2026-10-04T00:00:00Z). Re-enable with: loom-daemon host enable" >&2
    elif [[ $host_rc -ne 0 ]]; then
        echo "error: unrecognized subcommand 'host'" >&2
    fi
    exit $host_rc
fi
[[ "\${1:-}" == "update-gitignore" ]] && exit 0
exit 1
FAKE_EOF
    chmod +x "$dir/loom-daemon"
}

# run_with_fake <host-rc>: sets REPO, OUT, RC, FAKE.
run_with_fake() {
    REPO="$(make_fixture)"
    FAKE="$WORKDIR/fake-$1"
    rm -rf "$FAKE"
    make_fake_daemon "$FAKE" "$1"
    local home="$WORKDIR/home-$1"
    mkdir -p "$home"
    OUT="$(cd "$REPO" && env -u LOOM_DAEMON_BIN PATH="$FAKE:/usr/bin:/bin" HOME="$home" \
        LOOM_DAEMON_BIN_DIR="/nonexistent" bash "$SCRIPT" 2>&1)"
    RC=$?
}

echo "Test group 1: supported binary, host enabled (exit 0) -> resync proceeds"
run_with_fake 0
if grep -q "^host check --entry-point resync-installed.sh" "$FAKE/host.log" 2>/dev/null; then
    pass "(#10179) resync asks the PATH binary \`host check\`"
else
    fail "(#10179) \`host check\` was never invoked; log=$(cat "$FAKE/host.log" 2>/dev/null)"
fi
if [[ $RC -eq 0 && "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(#10179) an enabled host resyncs normally"
else
    fail "(#10179) enabled host did not resync (rc=$RC); out=$OUT"
fi

echo "Test group 2: supported binary, host disabled (exit 10) -> resync refuses with no side effects"
run_with_fake 10
if [[ $RC -eq 1 ]] && grep -q "cost freeze" <<<"$OUT" && grep -q "loom-daemon host enable" <<<"$OUT"; then
    pass "(#10179) a disabled host refuses (exit 1) naming the reason and the enable command"
else
    fail "(#10179) disabled host did not refuse properly (rc=$RC); out=$OUT"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]]; then
    pass "(#10179) a refused resync wrote nothing"
else
    fail "(#10179) a refused resync still rewrote .loom/hooks/guard.sh"
fi

for rc in 1 2; do
    echo "Test group 3.$rc: unsupported (pre-#10179) binary, \`host\` exits $rc -> resync proceeds"
    run_with_fake "$rc"
    if [[ $RC -eq 0 && "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
        pass "(#10179) exit $rc from an older binary is not read as an opt-out"
    else
        fail "(#10179) exit $rc from an older binary blocked the resync (rc=$RC); out=$OUT"
    fi
    if ! grep -q "unrecognized subcommand" <<<"$OUT"; then
        pass "(#10179) the older binary's unknown-subcommand noise is suppressed"
    else
        fail "(#10179) the older binary's error leaked into resync output"
    fi
done

# --- summary -----------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
echo "========================================"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
