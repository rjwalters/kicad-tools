#!/usr/bin/env bash
# test-resync-installed-agents-hooks.sh - resync-installed.sh covers
# .claude/agents/, reports hooks/tests drift and warns about unwired hooks
# (#11033), hermetic with a fake LOOM_DAEMON_BIN.
#
# This pins:
#   1. A stale .claude/agents/loom-champion.md is updated; a second run is a no-op.
#   2. A symlinked .claude/agents (the dogfood link) is left alone.
#   3. An existing .loom/hooks/tests/ is synced; without one, a run prints an
#      "un-synced drift" line naming the upstream test files and writes nothing.
#   4. An upstream hook the repo's .claude/settings.json does not wire is
#      named in a warning; settings.json is byte-identical afterwards, the exit
#      status is unchanged, and a fully wired repo prints no such warning.
#
# Usage: ./.loom/scripts/tests/test-resync-installed-agents-hooks.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESYNC="$(cd "$SCRIPT_DIR/.." && pwd)/resync-installed.sh"

TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-agents.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

FAKE="$WORKDIR/fake-loom-daemon"
printf '#!/usr/bin/env bash\nexit 0\n' > "$FAKE"
chmod +x "$FAKE"

REPO="$WORKDIR/repo"

# make_fixture: a consumer-shaped repo whose defaults/ sits beside it.
make_fixture() {
    rm -rf "$REPO"
    mkdir -p "$REPO/defaults/scripts" "$REPO/defaults/hooks/tests" "$REPO/defaults/.claude/agents" \
        "$REPO/.loom/scripts" "$REPO/.loom/hooks" "$REPO/.claude/agents" "$REPO/scripts/install"
    git -C "$REPO" init -q
    printf 'S\n' > "$REPO/defaults/scripts/foo.sh"; chmod +x "$REPO/defaults/scripts/foo.sh"
    printf 'S\n' > "$REPO/.loom/scripts/foo.sh"
    printf 'new champion\n' > "$REPO/defaults/.claude/agents/loom-champion.md"
    printf 'old champion\n' > "$REPO/.claude/agents/loom-champion.md"
    printf '#!/usr/bin/env bash\necho v2\n' > "$REPO/defaults/hooks/tests/test-hook-wiring.sh"
    printf '#!/usr/bin/env bash\necho t\n' > "$REPO/defaults/hooks/tests/test-roll-pause.sh"
    chmod +x "$REPO"/defaults/hooks/tests/*.sh
    # The upstream wiring table: two hooks.
    cat > "$REPO/scripts/install/provision-hooks.sh" <<'LIB'
_PHOOK_TYPES=(PreToolUse PreToolUse)
_PHOOK_MATCHERS=(Bash "*")
_PHOOK_NAMES=(guard-destructive.sh roll-pause.sh)
LIB
    cat > "$REPO/.claude/settings.json" <<'JSON'
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash -c 'x hook-wiring.sh PreToolUse guard-destructive.sh'"}]}]}}
JSON
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$REPO" > "$REPO/.loom/install-metadata.json"
    git -C "$REPO" add -A >/dev/null 2>&1
    git -C "$REPO" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
}

run() { # run resync with no args; sets OUT, RC
    OUT="$(cd "$REPO" && LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" 2>&1)"; RC=$?
}

echo "Case 1: a stale .claude/agents file is updated, then stable"
make_fixture
run
if [[ $RC -eq 0 && "$(cat "$REPO/.claude/agents/loom-champion.md")" == "new champion" ]]; then
    pass "loom-champion.md refreshed from defaults"
else fail "rc=$RC out=$OUT"; fi
if [[ "$OUT" == *"updated"*"agents/loom-champion.md"* ]]; then pass "the update is reported"
else fail "out=$OUT"; fi
git -C "$REPO" add -A >/dev/null 2>&1; git -C "$REPO" commit -qm "chore: resync" >/dev/null 2>&1
run
if [[ $RC -eq 0 && "$OUT" != *"agents/loom-champion.md"*updated* && "$(cat "$REPO/.claude/agents/loom-champion.md")" == "new champion" ]]; then
    pass "second run changes nothing"
else fail "rc=$RC out=$OUT"; fi

echo "Case 2: a symlinked .claude/agents is left alone"
make_fixture
rm -rf "$REPO/.claude/agents"
mkdir -p "$WORKDIR/elsewhere"; printf 'mine\n' > "$WORKDIR/elsewhere/loom-champion.md"
ln -s "$WORKDIR/elsewhere" "$REPO/.claude/agents"
run
if [[ "$(cat "$WORKDIR/elsewhere/loom-champion.md")" == "mine" && -L "$REPO/.claude/agents" && "$OUT" == *"skipped"*"agents"* ]]; then
    pass "symlink target untouched and the skip reported"
else fail "rc=$RC out=$OUT"; fi

echo "Case 3: hooks/tests is synced when present, reported when absent"
make_fixture
run
if [[ "$OUT" == *"un-synced drift"*"test-hook-wiring.sh"*"test-roll-pause.sh"* && ! -e "$REPO/.loom/hooks/tests" ]]; then
    pass "absent directory: files named, nothing written"
else fail "rc=$RC out=$OUT"; fi
make_fixture
mkdir -p "$REPO/.loom/hooks/tests"; printf 'stale\n' > "$REPO/.loom/hooks/tests/test-hook-wiring.sh"
run
if [[ "$(sed -n 2p "$REPO/.loom/hooks/tests/test-hook-wiring.sh")" == "echo v2" \
      && -x "$REPO/.loom/hooks/tests/test-roll-pause.sh" && "$OUT" != *"un-synced drift"* ]]; then
    pass "present directory: stale test updated, missing one created executable"
else fail "rc=$RC out=$OUT"; fi

echo "Case 4: an upstream hook missing from settings.json is reported, not added"
make_fixture
before="$(cat "$REPO/.claude/settings.json")"
run
if [[ "$OUT" == *"does not"*"PreToolUse [*] roll-pause.sh"* && "$OUT" != *"guard-destructive.sh"*"[Bash]"* ]]; then
    pass "roll-pause.sh named; the wired guard is not"
else fail "out=$OUT"; fi
if [[ "$(cat "$REPO/.claude/settings.json")" == "$before" && $RC -eq 0 ]]; then
    pass "settings.json unchanged and exit status 0"
else fail "rc=$RC"; fi
make_fixture
cat > "$REPO/.claude/settings.json" <<'JSON'
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"x guard-destructive.sh"}]},{"matcher":"*","hooks":[{"type":"command","command":"x roll-pause.sh"}]}]}}
JSON
run
if [[ "$OUT" != *"wires hook"* ]]; then pass "fully wired repo: no warning"
else fail "out=$OUT"; fi
make_fixture
printf '.claude/settings.json\n' > "$REPO/.loom/resync-ignore"
run
if [[ "$OUT" != *"wires hook"* ]]; then pass "a pinned settings.json is not reported on"
else fail "out=$OUT"; fi

echo
echo "Ran $TESTS_RUN checks, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
