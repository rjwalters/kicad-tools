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
#   4. (#13787) A Loom hook installed in .loom/hooks/ but not wired (the repo
#      only has the Repo Skills guard) is wired additively by resync: the
#      existing entry stays first, a second run is a no-op. --dry-run only
#      reports; non-JSON and pinned settings.json are left untouched.
#
# Usage: ./.loom/scripts/tests/test-resync-installed-agents-hooks.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESYNC="$(cd "$SCRIPT_DIR/.." && pwd)/resync-installed.sh"
PROVISION_LIB="$(cd "$SCRIPT_DIR/../../.." && pwd)/scripts/install/provision-hooks.sh"
[[ -r "$PROVISION_LIB" ]] || { echo "SKIP: $PROVISION_LIB not found (not a Loom source checkout)"; exit 0; }

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
    # The real upstream wiring table + ensure_project_hook_wiring (#13787).
    cp "$PROVISION_LIB" "$REPO/scripts/install/provision-hooks.sh"
    # Two installed hook scripts; settings.json starts with only the Repo Skills guard.
    printf '#!/usr/bin/env bash\n' > "$REPO/.loom/hooks/guard-destructive.sh"
    printf '#!/usr/bin/env bash\n' > "$REPO/.loom/hooks/roll-pause.sh"
    chmod +x "$REPO"/.loom/hooks/*.sh
    cat > "$REPO/.claude/settings.json" <<'JSON'
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"\"${CLAUDE_PROJECT_DIR}/.claude/skills/repo/hooks/guard-destructive.sh\""}]}]}}
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

echo "Case 4: resync wires missing Loom hooks additively (#13787)"
make_fixture
run
cmds() { jq -r '.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[].command' "$REPO/.claude/settings.json"; }
if [[ "$(cmds | sed -n 1p)" == *".claude/skills/repo/hooks/guard-destructive.sh"* \
      && "$(cmds | sed -n 2p)" == *".loom/hooks/guard-destructive.sh"* && "$(cmds | wc -l)" -eq 2 ]]; then
    pass "Repo Skills guard kept first, Loom guard appended under Bash"
else fail "rc=$RC cmds=$(cmds) out=$OUT"; fi
if jq -e '.hooks.PreToolUse[] | select(.matcher=="*") | .hooks[].command | contains(".loom/hooks/roll-pause.sh")' \
      "$REPO/.claude/settings.json" >/dev/null; then pass "roll-pause.sh wired too"
else fail "roll-pause.sh not wired"; fi
if [[ $RC -eq 0 ]]; then pass "exit status 0"; else fail "rc=$RC"; fi
after="$(cat "$REPO/.claude/settings.json")"
run
if [[ "$(cat "$REPO/.claude/settings.json")" == "$after" && "$OUT" != *"not wired"* ]]; then
    pass "second resync is a no-op"
else fail "out=$OUT"; fi

echo "Case 5: --dry-run reports the Repo-Skills-only repo and writes nothing"
make_fixture
before="$(cat "$REPO/.claude/settings.json")"
OUT="$(cd "$REPO" && LOOM_DAEMON_BIN="$FAKE" bash "$RESYNC" --dry-run 2>&1)"; RC=$?
if [[ "$OUT" == *"not wired"*"PreToolUse [Bash] guard-destructive.sh"* && "$(cat "$REPO/.claude/settings.json")" == "$before" ]]; then
    pass "guard named as installed-but-not-wired; file untouched"
else fail "out=$OUT"; fi

echo "Case 6: invalid-JSON and pinned settings.json are left untouched"
make_fixture
printf 'not json\n' > "$REPO/.claude/settings.json"
run
if [[ "$(cat "$REPO/.claude/settings.json")" == "not json" ]]; then pass "non-JSON file untouched"
else fail "out=$OUT"; fi
make_fixture
before="$(cat "$REPO/.claude/settings.json")"
printf '.claude/settings.json\n' > "$REPO/.loom/resync-ignore"
run
if [[ "$(cat "$REPO/.claude/settings.json")" == "$before" && "$OUT" != *"not wired"* ]]; then
    pass "a pinned settings.json is neither wired nor reported on"
else fail "out=$OUT"; fi

echo
echo "Ran $TESTS_RUN checks, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
