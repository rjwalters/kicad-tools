#!/usr/bin/env bash
# Test suite for defaults/hooks/hook-wiring.sh and the settings.json hook
# wirings that route through it (issue #7761).
#
# Usage: ./defaults/hooks/tests/test-hook-wiring.sh
#
# #7761: every PreToolUse hook entry used to end in `|| exit 0` — ALLOW — so a
# missing guard, a guard that had lost its executable bit, and a failed
# `git rev-parse --git-common-dir` all silently allowed the tool call with no
# stderr, no log line, and nothing to distinguish them from a guard that ran and
# decided to allow. This suite pins the replacement ladder:
#
#   rung 1  executable hook                 -> runs it (unchanged fast path)
#   rung 2  not a Loom workspace            -> silent allow (unchanged)
#   rung 3  present but not executable      -> runs it via `bash`, reports
#   rung 4  machine-level copy available    -> runs that (or stands down for an
#                                              already-wired user-scope entry)
#   rung 5  nothing anywhere                -> PreToolUse DENIES; other events
#                                              report loudly and allow
#
# plus the two properties the whole design rests on: the launcher NEVER exits
# non-zero, and no reachable path is silent inside a Loom workspace.
#
# Both subjects are resolved from the INSTALLED copy first (a Loom-installed
# consumer repo has no defaults/ tree at all), falling back to defaults/ for
# Loom's own source tree — the #6496 convention.
#
# Exit 0 = all pass, 1 = one or more failures.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

SRC_LAUNCHER="$REPO_ROOT/.loom/hooks/hook-wiring.sh"
[[ -r "$SRC_LAUNCHER" ]] || SRC_LAUNCHER="$REPO_ROOT/defaults/hooks/hook-wiring.sh"
SETTINGS="$REPO_ROOT/.claude/settings.json"

PASS=0
FAIL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

ok()   { PASS=$((PASS + 1)); printf "  ${GREEN}✓${NC} %s\n" "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf "  ${RED}✗${NC} %s\n" "$1"; [[ -n "${2:-}" ]] && printf "      %s\n" "$2"; }

assert_eq() { # <label> <expected> <actual>
    if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected [$2], got [$3]"; fi
}
assert_contains() { # <label> <needle> <haystack>
    if [[ "$3" == *"$2"* ]]; then ok "$1"; else bad "$1" "expected to contain [$2], got [$3]"; fi
}
assert_not_contains() { # <label> <needle> <haystack>
    if [[ "$3" != *"$2"* ]]; then ok "$1"; else bad "$1" "expected NOT to contain [$2], got [$3]"; fi
}

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# new_workspace <name> [--plain]
#
# A git repo with .loom/hooks/hook-wiring.sh installed. `--plain` omits the
# whole .loom/ tree, i.e. a repo that does NOT use Loom.
new_workspace() {
    local ws="$TMPROOT/$1"; shift
    local plain=0
    [[ "${1:-}" == "--plain" ]] && plain=1
    mkdir -p "$ws"
    git init -q "$ws"
    if [[ "$plain" -eq 0 ]]; then
        mkdir -p "$ws/.loom/hooks" "$ws/.loom/logs"
        cp "$SRC_LAUNCHER" "$ws/.loom/hooks/hook-wiring.sh"
        chmod +x "$ws/.loom/hooks/hook-wiring.sh"
    fi
    printf '%s' "$ws"
}

# install_hook <dir> <name> [--noexec]
#
# A stand-in "guard" that proves it ran by echoing a sentinel plus whatever it
# received on stdin (so stdin passthrough is verifiable).
install_hook() {
    local dir="$1" name="$2" mode="${3:-}"
    mkdir -p "$dir"
    cat > "$dir/$name" <<'HOOK'
#!/usr/bin/env bash
printf 'RAN:%s STDIN:%s\n' "$(basename "$0")" "$(cat)"
exit 0
HOOK
    if [[ "$mode" == "--noexec" ]]; then chmod -x "$dir/$name"; else chmod +x "$dir/$name"; fi
}

# An empty machine checkout, so a fixture never accidentally resolves the real
# operator's ~/.local/share/loom copy of a hook.
EMPTY_HOME="$TMPROOT/empty-home"
EMPTY_LOOM_HOME="$TMPROOT/empty-loom-home"
mkdir -p "$EMPTY_HOME" "$EMPTY_LOOM_HOME"

# run_launcher <workspace> <event> <name> [env assignments...]
#
# Runs the launcher the way a settings.json entry does, from inside the
# workspace. Prints "<exit>|<stdout>|<stderr>".
run_launcher() {
    local ws="$1" event="$2" name="$3"; shift 3
    local out err rc=0
    out="$TMPROOT/out.$$"; err="$TMPROOT/err.$$"
    ( cd "$ws" && env HOME="$EMPTY_HOME" LOOM_HOME="$EMPTY_LOOM_HOME" "$@" \
        bash "$ws/.loom/hooks/hook-wiring.sh" "$event" "$name" ) \
        </dev/null >"$out" 2>"$err" || rc=$?
    printf '%s|%s|%s' "$rc" "$(cat "$out")" "$(cat "$err")"
}

field() { printf '%s' "$1" | cut -d'|' -f"$2"; }

echo "=== hook-wiring.sh ladder (#7761) ==="

# --- rung 1: executable hook runs, stdin passes through --------------------
WS="$(new_workspace ws-ok)"
install_hook "$WS/.loom/hooks" guard-destructive.sh
OUT="$( ( cd "$WS" && echo '{"payload":1}' | env HOME="$EMPTY_HOME" LOOM_HOME="$EMPTY_LOOM_HOME" \
    bash "$WS/.loom/hooks/hook-wiring.sh" PreToolUse guard-destructive.sh 2>/dev/null ) )"
assert_contains "rung 1: an executable hook is exec'd" "RAN:guard-destructive.sh" "$OUT"
assert_contains "rung 1: stdin is passed through untouched" 'STDIN:{"payload":1}' "$OUT"

# --- rung 2: not a Loom workspace -> silent allow ---------------------------
# The launcher is copied in explicitly (a non-Loom repo would never have one);
# what makes this "not a Loom workspace" is the absent .loom/ directory, which
# is what the gate actually tests.
PLAIN="$TMPROOT/ws-plain"
mkdir -p "$PLAIN/bin"
git init -q "$PLAIN"
cp "$SRC_LAUNCHER" "$PLAIN/bin/hook-wiring.sh"
RC=0
OUT="$( ( cd "$PLAIN" && env HOME="$EMPTY_HOME" LOOM_HOME="$EMPTY_LOOM_HOME" \
    bash "$PLAIN/bin/hook-wiring.sh" PreToolUse guard-destructive.sh ) </dev/null 2>"$TMPROOT/plain.err" )" || RC=$?
assert_eq "rung 2: non-Loom workspace exits 0" "0" "$RC"
assert_eq "rung 2: non-Loom workspace emits no decision" "" "$OUT"
assert_eq "rung 2: non-Loom workspace is SILENT (no stderr)" "" "$(cat "$TMPROOT/plain.err")"

# --- rung 3: present but not executable -> still runs, loudly ---------------
WS="$(new_workspace ws-noexec)"
install_hook "$WS/.loom/hooks" guard-destructive.sh --noexec
R="$(run_launcher "$WS" PreToolUse guard-destructive.sh)"
assert_eq "rung 3: exits 0" "0" "$(field "$R" 1)"
assert_contains "rung 3: the hook STILL RUNS (zero lost coverage)" "RAN:guard-destructive.sh" "$(field "$R" 2)"
assert_contains "rung 3: warns on stderr" "BROKEN GUARD INSTALL" "$(field "$R" 3)"
assert_contains "rung 3: names the repair" "chmod +x" "$(field "$R" 3)"
assert_contains "rung 3: writes a hook-errors.log line" "not executable" "$(cat "$WS/.loom/logs/hook-errors.log" 2>/dev/null)"

# --- rung 4a: machine-level fallback runs when no user-scope entry wires it --
WS="$(new_workspace ws-machine)"
MACHINE="$TMPROOT/machine-checkout"
install_hook "$MACHINE/defaults/hooks" guard-destructive.sh
RC=0
OUT="$( ( cd "$WS" && env HOME="$EMPTY_HOME" LOOM_HOME="$MACHINE" \
    bash "$WS/.loom/hooks/hook-wiring.sh" PreToolUse guard-destructive.sh ) </dev/null 2>"$TMPROOT/m.err" )" || RC=0
assert_eq "rung 4a: exits 0" "0" "$RC"
assert_contains "rung 4a: the machine-level copy runs" "RAN:guard-destructive.sh" "$OUT"
assert_contains "rung 4a: reports the incomplete per-repo install" "BROKEN GUARD INSTALL" "$(cat "$TMPROOT/m.err")"

# --- rung 4b: stands down for an already-wired user-scope entry -------------
# A repo migrated to machine-level hooks carries no .loom/hooks/ copies BY
# DESIGN. Denying there would brick a workspace whose guards are in fact
# running fine — and firing here too would double-report every decision.
WIRED_HOME="$TMPROOT/wired-home"
mkdir -p "$WIRED_HOME/.claude"
cat > "$WIRED_HOME/.claude/settings.json" <<'JSON'
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash -c 'exec \"$H/defaults/hooks/guard-destructive.sh\"'"}]}]}}
JSON
RC=0
OUT="$( ( cd "$WS" && env HOME="$WIRED_HOME" LOOM_HOME="$MACHINE" \
    bash "$WS/.loom/hooks/hook-wiring.sh" PreToolUse guard-destructive.sh ) </dev/null 2>"$TMPROOT/m2.err" )" || RC=$?
assert_eq "rung 4b: exits 0" "0" "$RC"
assert_not_contains "rung 4b: does NOT double-fire the hook" "RAN:" "$OUT"
assert_eq "rung 4b: stands down quietly" "" "$(cat "$TMPROOT/m2.err")"

# --- rung 5: nothing anywhere -> PreToolUse DENIES --------------------------
WS="$(new_workspace ws-missing)"
R="$(run_launcher "$WS" PreToolUse guard-destructive.sh)"
assert_eq "rung 5: exits 0 (a deny is a decision, never a non-zero exit)" "0" "$(field "$R" 1)"
DENY="$(field "$R" 2)"
assert_contains "rung 5: emits a deny decision" '"permissionDecision":"deny"' "$DENY"
assert_contains "rung 5: deny is tagged PreToolUse" '"hookEventName":"PreToolUse"' "$DENY"
if printf '%s' "$DENY" | jq empty 2>/dev/null; then
    ok "rung 5: the deny document is valid JSON"
else
    bad "rung 5: the deny document is valid JSON" "$DENY"
fi
assert_contains "rung 5: the deny names the repair" "install-loom.sh" "$DENY"
assert_contains "rung 5: warns on stderr too" "BROKEN GUARD INSTALL" "$(field "$R" 3)"
assert_contains "rung 5: writes a hook-errors.log line" "is not installed" "$(cat "$WS/.loom/logs/hook-errors.log" 2>/dev/null)"

# --- rung 5: the operator escape hatch downgrades the deny -----------------
R="$(run_launcher "$WS" PreToolUse guard-destructive.sh LOOM_GUARD_WIRING_FAILOPEN=1)"
assert_eq "escape hatch: exits 0" "0" "$(field "$R" 1)"
assert_eq "escape hatch: no deny is emitted" "" "$(field "$R" 2)"
assert_contains "escape hatch: STILL reports (never silent)" "BROKEN GUARD INSTALL" "$(field "$R" 3)"

# --- rung 5: a non-PreToolUse event has no deny channel --------------------
R="$(run_launcher "$WS" Stop guard-background-subagents.sh)"
assert_eq "Stop event: exits 0" "0" "$(field "$R" 1)"
assert_eq "Stop event: emits no deny (no such channel)" "" "$(field "$R" 2)"
assert_contains "Stop event: still reports the broken install" "BROKEN GUARD INSTALL" "$(field "$R" 3)"

# --- the empty-ROOT degenerate path (#7761 AC4) ----------------------------
# `git rev-parse --git-common-dir` failing used to leave ROOT empty, so the
# `-x` test ran against the absolute path `/.loom/hooks/<name>` — which can
# never succeed — and fell through to a silent allow. It must now land on the
# SAME ladder as every other failure, not a third silent path.
WS="$(new_workspace ws-nogit)"
FAKE_BIN="$TMPROOT/nogit-bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/git" <<'SH'
#!/usr/bin/env bash
exit 127
SH
chmod +x "$FAKE_BIN/git"
RC=0
OUT="$( ( cd "$WS" && env PATH="$FAKE_BIN:$PATH" HOME="$EMPTY_HOME" LOOM_HOME="$EMPTY_LOOM_HOME" \
    CLAUDE_PROJECT_DIR="$WS" bash "$WS/.loom/hooks/hook-wiring.sh" PreToolUse guard-destructive.sh ) \
    </dev/null 2>"$TMPROOT/nogit.err" )" || RC=$?
assert_eq "empty ROOT: exits 0" "0" "$RC"
assert_contains "empty ROOT: denies instead of silently allowing" '"permissionDecision":"deny"' "$OUT"
assert_contains "empty ROOT: reports on stderr" "BROKEN GUARD INSTALL" "$(cat "$TMPROOT/nogit.err")"

# ...and the same degenerate path in a NON-Loom repo still allows silently.
RC=0
OUT="$( ( cd "$PLAIN" && env PATH="$FAKE_BIN:$PATH" HOME="$EMPTY_HOME" LOOM_HOME="$EMPTY_LOOM_HOME" \
    CLAUDE_PROJECT_DIR="$PLAIN" bash "$PLAIN/bin/hook-wiring.sh" PreToolUse guard-destructive.sh ) \
    </dev/null 2>"$TMPROOT/nogit2.err" )" || RC=$?
assert_eq "empty ROOT + non-Loom repo: exits 0" "0" "$RC"
assert_eq "empty ROOT + non-Loom repo: still a silent allow" "" "$OUT$(cat "$TMPROOT/nogit2.err")"

# --- a garbled invocation must not deny every tool call --------------------
WS="$(new_workspace ws-args)"
RC=0
OUT="$( ( cd "$WS" && env HOME="$EMPTY_HOME" LOOM_HOME="$EMPTY_LOOM_HOME" \
    bash "$WS/.loom/hooks/hook-wiring.sh" ) </dev/null 2>"$TMPROOT/args.err" )" || RC=$?
assert_eq "no arguments: exits 0" "0" "$RC"
assert_eq "no arguments: emits no deny" "" "$OUT"
assert_contains "no arguments: says so on stderr" "hook-wiring.sh" "$(cat "$TMPROOT/args.err")"

# ---------------------------------------------------------------------------
# The real .claude/settings.json wirings
# ---------------------------------------------------------------------------
echo ""
echo "=== .claude/settings.json wirings ==="

if [[ ! -f "$SETTINGS" ]] || ! command -v jq >/dev/null 2>&1; then
    echo "  (skipped: $SETTINGS or jq unavailable)"
else
    # Every wired command, with the event it is wired under.
    COMMANDS="$(jq -r '
        .hooks | to_entries[] | .key as $event
        | .value[]? | .hooks[]? | select((.command // "") | contains(".loom/hooks/"))
        | "\($event)\t\(.command)"' "$SETTINGS")"

    if [[ -z "$COMMANDS" ]]; then
        bad "settings.json wires at least one .loom/hooks/ entry" "found none"
    fi

    # AC1/AC2 as a structural regression assertion: no PreToolUse wiring may
    # end its broken-install path in a bare `exit 0`. This is the shape the
    # issue is about, so it is pinned directly rather than only behaviourally.
    while IFS=$'\t' read -r event command; do
        [[ -n "$event" ]] || continue
        name="$(printf '%s' "$command" | grep -o '\.loom/hooks/[A-Za-z0-9._-]*\.sh' | grep -v hook-wiring | head -1 | sed 's|.*/||')"
        [[ -n "$name" ]] || continue
        assert_contains "$name ($event): routes through hook-wiring.sh" "hook-wiring.sh" "$command"
        assert_contains "$name ($event): signals a broken install on stderr" "BROKEN GUARD INSTALL" "$command"
        # roll-pause.sh is the one fail-open PreToolUse entry; see (g) below.
        if [[ "$event" == "PreToolUse" && "$name" != "roll-pause.sh" ]]; then
            assert_contains "$name: inline fallback denies rather than allowing" '\"permissionDecision\":\"deny\"' "$command"
        fi
    done <<< "$COMMANDS"

    # Behavioural checks, driving ONE real wired command (guard-destructive.sh,
    # the Bash-matcher guard) through each install state.
    CMD="$(printf '%s\n' "$COMMANDS" | grep -m1 'guard-destructive\.sh' | cut -f2-)"
    CMD="${CMD#bash -c }"

    run_wired() { # <workspace> -> "<exit>|<stdout>|<stderr>"
        local ws="$1" rc=0
        ( cd "$ws" && env HOME="$EMPTY_HOME" LOOM_HOME="$EMPTY_LOOM_HOME" \
            bash -c "${CMD:1:${#CMD}-2}" ) </dev/null >"$TMPROOT/w.out" 2>"$TMPROOT/w.err" || rc=$?
        printf '%s|%s|%s' "$rc" "$(cat "$TMPROOT/w.out")" "$(cat "$TMPROOT/w.err")"
    }

    # (a) fully installed -> the guard runs
    WS="$(new_workspace ws-wired-ok)"
    install_hook "$WS/.loom/hooks" guard-destructive.sh
    R="$(run_wired "$WS")"
    assert_eq "wiring: exits 0 when installed" "0" "$(field "$R" 1)"
    assert_contains "wiring: runs the guard when installed" "RAN:guard-destructive.sh" "$(field "$R" 2)"

    # (b) launcher NOT yet resynced, guard present -> guard still runs.
    # This is the rollout-safety property: a host whose .loom/hooks/ lags this
    # change keeps working exactly as before instead of bricking.
    WS="$(new_workspace ws-wired-nolauncher)"
    rm -f "$WS/.loom/hooks/hook-wiring.sh"
    install_hook "$WS/.loom/hooks" guard-destructive.sh
    R="$(run_wired "$WS")"
    assert_eq "wiring: exits 0 with no launcher installed" "0" "$(field "$R" 1)"
    assert_contains "wiring: falls back to exec'ing the guard directly" "RAN:guard-destructive.sh" "$(field "$R" 2)"

    # (c) launcher AND guard both absent, Loom workspace -> deny
    WS="$(new_workspace ws-wired-broken)"
    rm -f "$WS/.loom/hooks/hook-wiring.sh"
    R="$(run_wired "$WS")"
    assert_eq "wiring: exits 0 when nothing is installed" "0" "$(field "$R" 1)"
    assert_contains "wiring: inline fallback denies" '"permissionDecision":"deny"' "$(field "$R" 2)"
    assert_contains "wiring: inline fallback warns on stderr" "BROKEN GUARD INSTALL" "$(field "$R" 3)"

    # (d) same, but NOT a Loom workspace -> silent allow (unchanged)
    R="$(run_wired "$PLAIN")"
    assert_eq "wiring: exits 0 in a non-Loom repo" "0" "$(field "$R" 1)"
    assert_eq "wiring: stays silent in a non-Loom repo" "" "$(field "$R" 2)$(field "$R" 3)"

    # (e) guard present but not executable, launcher absent -> still runs.
    # The inline fallback carries its own copy of rung 3 for exactly this
    # window (settings updated, .loom/hooks/ not yet resynced).
    WS="$(new_workspace ws-wired-noexec)"
    rm -f "$WS/.loom/hooks/hook-wiring.sh"
    install_hook "$WS/.loom/hooks" guard-destructive.sh --noexec
    R="$(run_wired "$WS")"
    assert_eq "wiring: exits 0 for a non-executable guard" "0" "$(field "$R" 1)"
    assert_contains "wiring: runs a non-executable guard via bash" "RAN:guard-destructive.sh" "$(field "$R" 2)"

    # (f) the empty-ROOT degenerate path through the REAL wiring (#7761 AC4).
    # `cd "$(git rev-parse …)/.."` with an empty substitution is `cd /..`, which
    # succeeds and yields `/` — so the pre-#7761 wiring silently allowed here.
    run_wired_nogit() { # <workspace> -> "<exit>|<stdout>|<stderr>"
        local ws="$1" rc=0
        ( cd "$ws" && env PATH="$FAKE_BIN:$PATH" HOME="$EMPTY_HOME" LOOM_HOME="$EMPTY_LOOM_HOME" \
            CLAUDE_PROJECT_DIR="$ws" bash -c "${CMD:1:${#CMD}-2}" ) \
            </dev/null >"$TMPROOT/n.out" 2>"$TMPROOT/n.err" || rc=$?
        printf '%s|%s|%s' "$rc" "$(cat "$TMPROOT/n.out")" "$(cat "$TMPROOT/n.err")"
    }
    WS="$(new_workspace ws-wired-nogit)"
    rm -f "$WS/.loom/hooks/hook-wiring.sh"
    R="$(run_wired_nogit "$WS")"
    assert_eq "wiring + no git: exits 0" "0" "$(field "$R" 1)"
    assert_contains "wiring + no git: denies instead of silently allowing" '"permissionDecision":"deny"' "$(field "$R" 2)"
    R="$(run_wired_nogit "$PLAIN")"
    assert_eq "wiring + no git + non-Loom repo: still a silent allow" "0||" "$R"

    # (g) roll-pause.sh is not a guard (#10830): it parks a daemon-dispatched
    # agent's tool calls for a roll and is a no-op otherwise, so a missing or
    # stale installed copy must ALLOW. This repo's PreToolUse entry for it is
    # match-all; failing closed there would block every tool call in the
    # checkout. Driven with the launcher present (rung 5) and absent (the
    # inline fallback).
    CMD="$(printf '%s\n' "$COMMANDS" | grep -m1 '^PreToolUse.*roll-pause\.sh' | cut -f2-)"
    CMD="${CMD#bash -c }"
    if [[ -z "$CMD" ]]; then
        bad "settings.json wires roll-pause.sh under PreToolUse" "found none"
    else
        WS="$(new_workspace ws-pause-nohook)"
        R="$(run_wired "$WS")"
        assert_eq "roll-pause, hook missing: exits 0 and emits no deny" "0|" "$(field "$R" 1)|$(field "$R" 2)"
        assert_contains "roll-pause, hook missing: still warns on stderr" "roll-pause.sh" "$(field "$R" 3)"
        rm -f "$WS/.loom/hooks/hook-wiring.sh"
        R="$(run_wired "$WS")"
        assert_eq "roll-pause, hook and launcher missing: exits 0 and emits no deny" "0|" "$(field "$R" 1)|$(field "$R" 2)"
        install_hook "$WS/.loom/hooks" roll-pause.sh
        R="$(run_wired "$WS")"
        assert_contains "roll-pause, hook installed: the hook runs" "RAN:roll-pause.sh" "$(field "$R" 2)"
    fi
fi

# ---------------------------------------------------------------------------
# defaults/ vs .loom/ parity for the launcher itself (#7416/#7423 class)
# ---------------------------------------------------------------------------
echo ""
echo "=== installed-copy parity ==="
if [[ -f "$REPO_ROOT/defaults/hooks/hook-wiring.sh" && -f "$REPO_ROOT/.loom/hooks/hook-wiring.sh" ]]; then
    if cmp -s "$REPO_ROOT/defaults/hooks/hook-wiring.sh" "$REPO_ROOT/.loom/hooks/hook-wiring.sh"; then
        ok "defaults/hooks/hook-wiring.sh == .loom/hooks/hook-wiring.sh"
    else
        bad "defaults/hooks/hook-wiring.sh == .loom/hooks/hook-wiring.sh" "the installed copy has drifted"
    fi
    if [[ -x "$REPO_ROOT/.loom/hooks/hook-wiring.sh" ]]; then
        ok ".loom/hooks/hook-wiring.sh is executable"
    else
        bad ".loom/hooks/hook-wiring.sh is executable" "lost its +x bit"
    fi
else
    echo "  (skipped: not the Loom source tree)"
fi

echo ""
printf 'test-hook-wiring: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
