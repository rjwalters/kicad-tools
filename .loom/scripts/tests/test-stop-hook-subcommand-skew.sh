#!/usr/bin/env bash
# test-stop-hook-subcommand-skew.sh - Regression tests for the `Stop` /
# `SubagentStop` hook wrapper in .claude/settings.json that invokes
# `loom-daemon worktree-state stop-hook` (issues #8267, #8377).
#
# The bug (#8377): the wrapper ended in a bare
#   exec "$B" worktree-state stop-hook
# so when the resolved loom-daemon binary predated the `worktree-state`
# subcommand (#8267), clap's usage-error exit code 2 became the HOOK's exit
# code — and Claude Code reads exit 2 from a Stop hook as "block this turn
# from ending". Every turn on such a host wedged, including unattended
# headless sweeps with nobody to nudge them.
#
# That violates the guard's own documented contract
# (loom-daemon/src/worktree_state/stop_hook.rs, "# Failure mode: always
# allow"): a BLOCK is signalled by printing {"decision":"block",...} on
# stdout and exiting 0, never by a non-zero exit. So a non-zero exit from the
# subcommand can only ever mean "the guard failed", which must fail open.
#
# The fix: drop the bare `exec` and swallow a non-zero exit —
#   "$B" worktree-state stop-hook || exit 0
# "binary too old to know this subcommand" is now treated exactly like
# "binary absent": both exit 0.
#
# These tests assert BOTH halves, since a fix that only fails open is
# indistinguishable from disabling the guard entirely:
#   1. Fail open — an old binary (clap exit 2), a panicking binary, an
#      unresolvable binary and a non-executable binary all yield hook exit 0.
#   2. Still guards — a current binary that prints a block verdict still has
#      that verdict reach Claude Code verbatim on stdout, with exit 0.
#
# Source-tree-only by design (#6194): this hook wiring lives in Loom's own
# project-scope .claude/settings.json (it is NOT part of defaults/.claude/
# settings.json nor of the user-scope provision-hooks.sh set), so this suite
# SKIPs (exit 0) rather than errors when run outside Loom's own checkout.
#
# Since #8372 it also covers the CONSUMER entry point,
# defaults/hooks/guard-uncommitted-work.sh, against the same fake binaries:
# the same fail-open contract, plus `--consumer-canary` reaching the daemon and
# wrapper failures being recorded (opted-in workspaces only).
#
# Usage:
#   bash defaults/scripts/tests/test-stop-hook-subcommand-skew.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SETTINGS="$REPO_ROOT/.claude/settings.json"

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
    [[ -n "${2:-}" ]] && echo "    $2"
    return 0
}

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then
        pass "$msg"
    else
        fail "$msg" "expected '$expected', got '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg" "expected to find '$needle' in: $haystack"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg" "did NOT expect to find '$needle' in: $haystack"
    fi
}

if [[ ! -f "$SETTINGS" ]]; then
    echo "SKIP: source-tree-only test, $SETTINGS not found (not shipped into an installed repo)" >&2
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available" >&2
    exit 0
fi

# --- Extract the wrapper commands under test -------------------------------
# One per hook event, selected by the subcommand they invoke so the test does
# not depend on the position of the entry in the array.
extract_cmd() {
    local event="$1"
    jq -r --arg ev "$event" '
        .hooks[$ev][]?.hooks[]?.command // empty
        | select(contains("worktree-state stop-hook"))
    ' "$SETTINGS"
}

STOP_CMD="$(extract_cmd Stop)"
SUBAGENT_STOP_CMD="$(extract_cmd SubagentStop)"

if [[ -z "$STOP_CMD" || -z "$SUBAGENT_STOP_CMD" ]]; then
    echo "ERROR: could not locate the worktree-state stop-hook wrapper for both Stop and SubagentStop in $SETTINGS" >&2
    exit 1
fi

# --- Fake workspace + stub binaries ----------------------------------------
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-stop-hook-skew.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

WS="$TMP_ROOT/ws"
mkdir -p "$WS/.loom/scripts/lib"
# A real git repo so the wrapper's `git rev-parse --git-common-dir` resolves
# to THIS workspace and not to whatever repo the test runner is sitting in.
git init -q "$WS" >/dev/null 2>&1 || true

# Stand-in for lib/locate-daemon-bin.sh: resolves whatever stub the current
# case exports, and fails (like the real one) when there is nothing to find.
cat >"$WS/.loom/scripts/lib/locate-daemon-bin.sh" <<'LIB'
loom_resolve_self_daemon_bin() {
    [ -n "${LOOM_TEST_STUB_BIN:-}" ] || return 1
    printf '%s\n' "$LOOM_TEST_STUB_BIN"
}
LIB

BIN_DIR="$TMP_ROOT/bin"
mkdir -p "$BIN_DIR"

# (a) A loom-daemon that predates #8267: clap rejects the unknown subcommand
#     with a usage error on stderr and exit code 2.
cat >"$BIN_DIR/old-daemon" <<'OLD'
#!/usr/bin/env bash
echo "error: unrecognized subcommand '$1'" >&2
echo "Usage: loom-daemon <COMMAND>" >&2
exit 2
OLD

# (b) A current loom-daemon whose guard decided to BLOCK: verdict on stdout,
#     exit 0. This is the contract in stop_hook.rs.
cat >"$BIN_DIR/blocking-daemon" <<'BLOCKING'
#!/usr/bin/env bash
printf '%s\n' '{"decision":"block","reason":"STOP BLOCKED: 3 uncommitted files"}'
exit 0
BLOCKING

# (c) A current loom-daemon whose guard allowed the stop with an advisory.
cat >"$BIN_DIR/advisory-daemon" <<'ADVISORY'
#!/usr/bin/env bash
printf '%s\n' '{"systemMessage":"2 commits, pushed"}'
exit 0
ADVISORY

# (d) A loom-daemon that crashes (Rust panic -> exit 101).
cat >"$BIN_DIR/panicking-daemon" <<'PANIC'
#!/usr/bin/env bash
echo "thread 'main' panicked at src/worktree_state.rs:1:1" >&2
exit 101
PANIC

# (e) A binary that exists but is not executable.
printf '#!/usr/bin/env bash\nexit 2\n' >"$BIN_DIR/not-executable"

chmod +x "$BIN_DIR/old-daemon" "$BIN_DIR/blocking-daemon" \
    "$BIN_DIR/advisory-daemon" "$BIN_DIR/panicking-daemon"
chmod -x "$BIN_DIR/not-executable"

PAYLOAD='{"session_id":"t","transcript_path":"/nonexistent","cwd":"'"$WS"'","stop_hook_active":false,"hook_event_name":"Stop"}'

HOOK_STDOUT=""
HOOK_STATUS=0

# run_hook <command-string> <stub-binary-or-empty>
# Executes the wrapper exactly as Claude Code would (shell-evaluated command
# string, hook payload on stdin), capturing stdout and the exit status.
run_hook() {
    local cmd="$1" stub="$2" out_file="$TMP_ROOT/out.$$"
    (
        cd "$WS" || exit 1
        export CLAUDE_PROJECT_DIR="$WS"
        # shellcheck disable=SC2030  # subshell-local on purpose
        export LOOM_TEST_STUB_BIN="$stub"
        eval "$cmd" <<<"$PAYLOAD" 2>/dev/null
    ) >"$out_file"
    HOOK_STATUS=$?
    HOOK_STDOUT="$(cat "$out_file")"
    rm -f "$out_file"
}

echo "=== Stop-hook wrapper: subcommand-skew fail-open (#8377) ==="
echo ""

# ---------------------------------------------------------------------------
# Static: the bare `exec` is gone from BOTH wired entries.
# ---------------------------------------------------------------------------
echo "Static: wrapper text"
for pair in "Stop:$STOP_CMD" "SubagentStop:$SUBAGENT_STOP_CMD"; do
    event="${pair%%:*}"
    cmd="${pair#*:}"
    assert_contains "$cmd" 'worktree-state stop-hook || exit 0' \
        "$event: the subcommand invocation falls back to exit 0"
    # shellcheck disable=SC2016  # the literal `$B` IS the needle: this asserts the wrapper's own text
    assert_not_contains "$cmd" 'exec "$B" worktree-state' \
        "$event: no bare exec into the subcommand (a usage error would become the hook's exit code)"
done
echo ""

# ---------------------------------------------------------------------------
# Functional: both hook events, every failure and success mode.
# ---------------------------------------------------------------------------
for pair in "Stop:$STOP_CMD" "SubagentStop:$SUBAGENT_STOP_CMD"; do
    event="${pair%%:*}"
    cmd="${pair#*:}"

    echo "$event: fail-open cases"

    # The regression this issue is about: binary predates `worktree-state`.
    run_hook "$cmd" "$BIN_DIR/old-daemon"
    assert_eq "$HOOK_STATUS" "0" "$event: binary too old for the subcommand -> hook exits 0"
    assert_eq "$HOOK_STDOUT" "" "$event: binary too old -> no verdict printed"

    # A crashing binary is the same class of failure.
    run_hook "$cmd" "$BIN_DIR/panicking-daemon"
    assert_eq "$HOOK_STATUS" "0" "$event: panicking binary -> hook exits 0"

    # Pre-existing fail-open paths must keep working.
    run_hook "$cmd" ""
    assert_eq "$HOOK_STATUS" "0" "$event: binary unresolvable -> hook exits 0"

    run_hook "$cmd" "$BIN_DIR/not-executable"
    assert_eq "$HOOK_STATUS" "0" "$event: binary present but not executable -> hook exits 0"

    run_hook "$cmd" "$TMP_ROOT/bin/does-not-exist"
    assert_eq "$HOOK_STATUS" "0" "$event: binary path missing -> hook exits 0"

    echo ""
    echo "$event: the guard still guards"

    # The other half: a supported binary's BLOCK verdict must survive intact.
    run_hook "$cmd" "$BIN_DIR/blocking-daemon"
    assert_eq "$HOOK_STATUS" "0" "$event: block verdict -> hook exits 0 (block is stdout JSON, not an exit code)"
    assert_contains "$HOOK_STDOUT" '"decision":"block"' \
        "$event: block verdict reaches Claude Code on stdout"
    assert_contains "$HOOK_STDOUT" 'STOP BLOCKED' \
        "$event: block reason reaches Claude Code on stdout"

    run_hook "$cmd" "$BIN_DIR/advisory-daemon"
    assert_eq "$HOOK_STATUS" "0" "$event: advisory verdict -> hook exits 0"
    assert_contains "$HOOK_STDOUT" '"systemMessage"' \
        "$event: advisory reaches Claude Code on stdout"

    echo ""
done

# ---------------------------------------------------------------------------
# The consumer canary stub (#8372): defaults/hooks/guard-uncommitted-work.sh.
#
# Same fail-open contract as the dogfood wrapper above, exercised against the
# same fake binaries, plus the two things only the stub does: it passes
# `--consumer-canary`, and when the daemon cannot run it records the wrapper
# failure itself (only for an opted-in workspace), so a version-skewed host
# shows up as a coverage gap instead of a silent zero.
# ---------------------------------------------------------------------------
STUB_SRC="$REPO_ROOT/defaults/hooks/guard-uncommitted-work.sh"
if [[ ! -f "$STUB_SRC" ]]; then
    fail "consumer stub exists" "missing $STUB_SRC"
else
    CHK="$TMP_ROOT/chk"
    mkdir -p "$CHK/hooks" "$CHK/scripts/lib"
    cp "$STUB_SRC" "$CHK/hooks/guard-uncommitted-work.sh"
    chmod +x "$CHK/hooks/guard-uncommitted-work.sh"
    cp "$WS/.loom/scripts/lib/locate-daemon-bin.sh" "$CHK/scripts/lib/locate-daemon-bin.sh"
    STUB="$CHK/hooks/guard-uncommitted-work.sh"

    # An argv/stdin-recording daemon: proves the flag and the payload arrive.
    cat >"$BIN_DIR/recording-daemon" <<'RECORDING'
#!/usr/bin/env bash
{ printf 'argv=%s\n' "$*"; printf 'inv=%s\n' "${LOOM_HOOK_INVOCATION_ID:-}"; printf 'stdin=%s\n' "$(cat)"; } >"$RECORD_FILE"
exit 0
RECORDING
    # A daemon that prints a (would-be) block and then fails: the partial
    # stdout must NOT reach Claude Code.
    cat >"$BIN_DIR/partial-daemon" <<'PARTIAL'
#!/usr/bin/env bash
printf '%s\n' '{"decision":"block","reason":"half-written"}'
exit 2
PARTIAL
    chmod +x "$BIN_DIR/recording-daemon" "$BIN_DIR/partial-daemon"

    OPTED="$TMP_ROOT/opted"
    OPTED_OUT="$TMP_ROOT/opted-out"
    for d in "$OPTED" "$OPTED_OUT"; do
        mkdir -p "$d/.loom"
        git init -q "$d" >/dev/null 2>&1 || true
    done
    printf '{"guards": {"uncommittedWorkConsumerCanary": true}}\n' >"$OPTED/.loom/config.json"
    printf '{"guards": {"uncommittedWorkConsumerCanary": false}}\n' >"$OPTED_OUT/.loom/config.json"
    CANARY_LOG="$TMP_ROOT/logs/canary.jsonl"
    STUB_OUT="$TMP_ROOT/stub-out.txt"

    # run_stub <workspace> <event> <stub-binary-or-empty> [stub-path]
    run_stub() {
        local ws="$1" ev="$2" stub="$3" hook="${4:-$STUB}"
        local payload='{"session_id":"t","transcript_path":"/nonexistent","cwd":"'"$ws"'","stop_hook_active":false,"hook_event_name":"'"$ev"'"}'
        (
            cd "$ws" || exit 1
            unset LOOM_PROJECT_ROOT
            # shellcheck disable=SC2031  # subshell-local on purpose, like run_hook
            export LOOM_TEST_STUB_BIN="$stub"
            export LOOM_UNCOMMITTED_WORK_CANARY_LOG="$CANARY_LOG"
            export RECORD_FILE="$TMP_ROOT/record.txt"
            bash "$hook" <<<"$payload" 2>/dev/null
        ) >"$STUB_OUT"
        HOOK_STATUS=$?
        HOOK_STDOUT="$(cat "$STUB_OUT")"
    }
    log_lines() { if [[ -f "$CANARY_LOG" ]]; then wc -l <"$CANARY_LOG" | tr -d ' '; else echo 0; fi; }
    last_record() { tail -1 "$CANARY_LOG" 2>/dev/null; }

    echo "Consumer stub: flag + payload reach the daemon"
    run_stub "$OPTED" Stop "$BIN_DIR/recording-daemon"
    rec="$(cat "$TMP_ROOT/record.txt" 2>/dev/null)"
    assert_contains "$rec" 'argv=worktree-state stop-hook --consumer-canary' "stub invokes worktree-state stop-hook --consumer-canary"
    assert_contains "$rec" '"hook_event_name":"Stop"' "stub forwards the hook payload on stdin"
    assert_contains "$rec" 'inv=inv-' "stub exports a per-invocation correlation id"
    echo ""

    for event in Stop SubagentStop; do
        echo "Consumer stub ($event): success paths"
        before="$(log_lines)"
        run_stub "$OPTED" "$event" "$BIN_DIR/blocking-daemon"
        assert_eq "$HOOK_STATUS" "0" "$event: block verdict -> exit 0"
        assert_contains "$HOOK_STDOUT" '"decision":"block"' "$event: block verdict reaches Claude Code"
        run_stub "$OPTED" "$event" "$BIN_DIR/advisory-daemon"
        assert_contains "$HOOK_STDOUT" '"systemMessage"' "$event: advisory reaches Claude Code"
        assert_eq "$(log_lines)" "$before" "$event: a successful daemon run adds no wrapper record (the daemon records its own)"
        echo ""

        echo "Consumer stub ($event): wrapper failures fail open AND are recorded"
        run_stub "$OPTED" "$event" "$BIN_DIR/old-daemon"
        assert_eq "$HOOK_STATUS|$HOOK_STDOUT" "0|" "$event: stale binary (clap exit 2) -> silent exit 0"
        assert_contains "$(last_record)" '"error":"unsupported_subcommand_or_flag"' "$event: stale binary recorded as version skew"
        assert_contains "$(last_record)" '"source":"wrapper"' "$event: recorded with source=wrapper (outside the detection denominator)"
        assert_contains "$(last_record)" "\"event\":\"$event\"" "$event: wrapper record names the event"

        run_stub "$OPTED" "$event" "$BIN_DIR/partial-daemon"
        assert_eq "$HOOK_STATUS|$HOOK_STDOUT" "0|" "$event: partial stdout from a failing daemon is discarded"

        run_stub "$OPTED" "$event" "$BIN_DIR/panicking-daemon"
        assert_eq "$HOOK_STATUS" "0" "$event: panicking binary -> exit 0"
        assert_contains "$(last_record)" '"error":"daemon_exit_101"' "$event: panic recorded with its exit code"

        run_stub "$OPTED" "$event" ""
        assert_eq "$HOOK_STATUS|$HOOK_STDOUT" "0|" "$event: unresolvable binary -> silent exit 0"
        assert_contains "$(last_record)" '"error":"binary_unresolved"' "$event: missing binary recorded"

        run_stub "$OPTED" "$event" "$BIN_DIR/not-executable"
        assert_eq "$HOOK_STATUS" "0" "$event: non-executable binary -> exit 0"
        echo ""
    done

    echo "Consumer stub: missing daemon library"
    NOLIB="$TMP_ROOT/nolib/hooks"
    mkdir -p "$NOLIB"
    cp "$STUB_SRC" "$NOLIB/guard-uncommitted-work.sh"
    run_stub "$OPTED" Stop "$BIN_DIR/blocking-daemon" "$NOLIB/guard-uncommitted-work.sh"
    assert_eq "$HOOK_STATUS|$HOOK_STDOUT" "0|" "no locate-daemon-bin.sh -> silent exit 0"
    assert_contains "$(last_record)" '"error":"lib_missing"' "missing library recorded"
    echo ""

    echo "Consumer stub: an opted-out workspace leaves no trace"
    before="$(log_lines)"
    run_stub "$OPTED_OUT" Stop "$BIN_DIR/old-daemon"
    assert_eq "$HOOK_STATUS|$HOOK_STDOUT" "0|" "opted-out + stale binary -> silent exit 0"
    run_stub "$OPTED_OUT" SubagentStop ""
    assert_eq "$(log_lines)" "$before" "opted-out workspace: no wrapper record written"
    echo ""

    echo "Consumer stub: every wrapper record is valid JSON"
    bad=0
    while IFS= read -r line; do
        printf '%s' "$line" | jq -e . >/dev/null 2>&1 || bad=$((bad + 1))
    done <"$CANARY_LOG"
    assert_eq "$bad" "0" "wrapper records parse as JSON"
    echo ""
fi

echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
