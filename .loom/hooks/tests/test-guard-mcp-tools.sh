#!/usr/bin/env bash
# test-guard-mcp-tools.sh — end-to-end suite for the `mcp__loom__*` PreToolUse
# guard (issue #9108).
#
# Drives the REAL hook file on stdin, exactly as Claude Code does, so this
# covers the whole path: the Shape-A stub resolves a daemon, execs
# `loom-daemon guard-mcp-tools`, and the decision comes back as a
# `hookSpecificOutput` document on stdout with exit 0.
#
# The rule logic also has dense unit coverage in Rust
# (loom-daemon/src/mcp_tool_guard/tests.rs, 24 cases). What only THIS suite can
# show is that the wiring in between actually carries a payload and a verdict —
# the failure #9108 exists to prevent was a guard that was never on the path at
# all, which no amount of unit testing the decision would have caught.
#
# Covers:
#   AC1 - `get_agent_metrics` with `role: "x; touch /tmp/mcp-guard-pwn"` DENIES,
#         and nothing is executed (the marker file is never created)
#   AC2 - a clean, fully-documented call is ALLOWED, and both verdicts land in
#         the decision log when `guards.decisionLog` is on
#   AC3 - the settings-wiring contract check passes on this checkout (its own
#         discriminating power is fixture-tested in
#         loom-daemon/src/guard_wiring/tests.rs)
#   plus: an off-enum value denies; a reviewed free-text sink (send_terminal_input
#         .input) is exempt; an out-of-namespace tool is untouched; the
#         guards.mcpToolArgs toggle (env and config); fail-open when no daemon
#         resolves; the exit-0-always contract; defaults/ vs .loom/ parity.
#
# NEEDS A BUILT loom-daemon (pinned via lib/require-daemon-bin.sh, which FAILS
# rather than SKIPs) — so it is listed in ci-excluded.txt and wired into the
# "Native Port Suites" CI job, which downloads the shared debug build. A suite
# that quietly skipped itself here would report green having asserted nothing
# about the guard this issue added.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HOOKS_DIR/../.." && pwd)"
HELPERS_DIR="$(cd "$REPO_ROOT/defaults/scripts" && pwd)"
DEFAULTS_HOOK="$HOOKS_DIR/guard-mcp-tools.sh"
INSTALLED_HOOK="$REPO_ROOT/.loom/hooks/guard-mcp-tools.sh"

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'ok:   %s\n' "$*"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$*"; }

command -v jq >/dev/null 2>&1 || {
    echo "jq is required" >&2
    exit 1
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- fixture workspace -------------------------------------------------------
# A real git repo so the hook's git-common-dir resolution and the daemon's
# main-checkout resolution both land on $WS. The hook is copied to the installed
# layout (.loom/hooks/) and .loom/scripts is symlinked at the real
# defaults/scripts, which is exactly how this repo itself is laid out — the hook
# sources ../scripts/lib/locate-daemon-bin.sh relative to its own directory.
WS="$TMP/ws"
mkdir -p "$WS/.loom/hooks" "$WS/.loom/logs"
git init -q "$WS"
ln -s "$HELPERS_DIR" "$WS/.loom/scripts"
cp "$DEFAULTS_HOOK" "$WS/.loom/hooks/guard-mcp-tools.sh"
chmod +x "$WS/.loom/hooks/guard-mcp-tools.sh"
HOOK="$WS/.loom/hooks/guard-mcp-tools.sh"

# --- pin the daemon under test ----------------------------------------------
# shellcheck source=../../scripts/tests/lib/require-daemon-bin.sh
source "$REPO_ROOT/defaults/scripts/tests/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "guard-mcp-tools" "check-guard-wiring"

DECISION_LOG="$TMP/decisions.log"

# payload <tool> <json-args> -> writes $PAYLOAD_FILE, echoes nothing
#
# The payload goes to a FILE and every hook invocation reads it with `<`, never
# `payload … | bash "$HOOK"`. A PreToolUse guard is entitled to exit 0 without
# reading stdin (that is exactly what the fail-open paths below do), which hands
# the producing `jq` a SIGPIPE — and under `set -o pipefail` the pipeline then
# reports 141 and the assertion reads it as "the hook exited non-zero". That is
# a race, so it fails intermittently rather than never (#7060/#7771,
# scripts/check-pipefail-early-exit.sh). A redirect has no producer to signal.
PAYLOAD_FILE="$TMP/payload.json"
payload() {
    jq -cn --arg t "$1" --argjson a "$2" --arg cwd "$WS" \
        '{hook_event_name:"PreToolUse", tool_name:$t, tool_input:$a, cwd:$cwd}' \
        >"$PAYLOAD_FILE"
}

# run_hook <tool> <json-args> [extra env assignments...] -> "<exit>|<stdout>"
# LOOM_GUARD_DECISION_LOG is always passed EXPLICITLY: a dispatched sweep child
# inherits LOOM_GUARD_DECISION_LOG=1 from the daemon, so relying on its absence
# would make this suite's result depend on who launched it.
run_hook() {
    local tool="$1" args="$2"
    shift 2
    local out rc=0
    payload "$tool" "$args"
    out="$( (
        cd "$WS" && env \
            LOOM_PROJECT_ROOT="$WS" \
            LOOM_GUARD_DECISION_LOG=0 \
            LOOM_GUARD_DECISION_LOG_FILE="$DECISION_LOG" \
            "$@" bash "$HOOK" 2>/dev/null <"$PAYLOAD_FILE"
    ))" || rc=$?
    printf '%s|%s' "$rc" "$out"
}

decision_of() { # "<exit>|<stdout>" -> deny|allow
    local out="${1#*|}" d
    d="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null)"
    printf '%s' "${d:-allow}"
}

reason_of() { # "<exit>|<stdout>" -> the deny reason (or empty)
    printf '%s' "${1#*|}" |
        jq -r '.hookSpecificOutput.permissionDecisionReason // ""' 2>/dev/null
}

# Fork-free literal-substring test over a deny reason. Deliberately `case`, not
# `reason_of … | grep -qF`: under `set -o pipefail` an early-exit consumer like
# `grep -q` can close the pipe mid-write and report the pipeline as failed
# (scripts/check-pipefail-early-exit.sh, #7060/#7771).
reason_has() { # "<exit>|<stdout>" <literal>
    case "$(reason_of "$1")" in
        *"$2"*) return 0 ;;
        *) return 1 ;;
    esac
}

echo "=== guard-mcp-tools.sh tests (#9108) ==="

# --- AC1: the acceptance case -----------------------------------------------
PWN=/tmp/mcp-guard-pwn
rm -f "$PWN"
r="$(run_hook mcp__loom__get_agent_metrics '{"role":"x; touch /tmp/mcp-guard-pwn"}')"
[ "$(decision_of "$r")" = deny ] &&
    ok "AC1: role with a command separator DENIES" ||
    bad "AC1: role with a command separator did not deny (got: $r)"
[ -e "$PWN" ] && {
    bad "AC1: the injected command EXECUTED — $PWN exists"
    rm -f "$PWN"
} || ok "AC1: nothing was executed (no $PWN)"
reason_has "$r" 'mcp-arg-shell-metachar' &&
    ok "AC1: the deny reason names the stable rule tag" ||
    bad "AC1: deny reason lacks the rule tag: $(reason_of "$r")"
reason_has "$r" 'mcp-guard-pwn' &&
    bad "AC1: the deny reason echoed the argument value back" ||
    ok "AC1: the deny reason never echoes the argument value"
reason_has "$r" 'guards.mcpToolArgs' &&
    ok "AC1: the deny reason names the category toggle" ||
    bad "AC1: deny reason lacks the category toggle: $(reason_of "$r")"

# --- AC2: clean calls are allowed -------------------------------------------
CLEAN='{"command":"costs","role":"builder","period":"week","format":"json","issue":9108}'
r="$(run_hook mcp__loom__get_agent_metrics "$CLEAN")"
[ "${r}" = "0|" ] &&
    ok "AC2: a fully-documented call is ALLOWED (exit 0, no output)" ||
    bad "AC2: a documented call was not a silent allow (got: $r)"

# --- AC2: the decision log, when guards.decisionLog is on --------------------
: >"$DECISION_LOG"
run_hook mcp__loom__get_agent_metrics "$CLEAN" LOOM_GUARD_DECISION_LOG=1 >/dev/null
run_hook mcp__loom__get_agent_metrics '{"role":"x; id"}' LOOM_GUARD_DECISION_LOG=1 >/dev/null
[ "$(wc -l <"$DECISION_LOG" | tr -d ' ')" = 2 ] &&
    ok "AC2: decisionLog on -> one JSONL record per decision" ||
    bad "AC2: expected 2 decision-log records, got: $(cat "$DECISION_LOG")"
jq -e -s '.[0].decision == "allow" and .[0].pattern == "mcp-args-clean"' "$DECISION_LOG" >/dev/null 2>&1 &&
    ok "AC2: the clean call is logged as an allow" ||
    bad "AC2: clean-call record wrong: $(head -1 "$DECISION_LOG")"
jq -e -s '.[1].decision == "deny" and .[1].pattern == "mcp-arg-shell-metachar"' "$DECISION_LOG" >/dev/null 2>&1 &&
    ok "AC2: the denied call is logged with its rule tag" ||
    bad "AC2: deny record wrong: $(tail -1 "$DECISION_LOG")"
jq -e -s 'all(.[]; .command | startswith("mcp__loom__") and (contains("; ") | not))' "$DECISION_LOG" >/dev/null 2>&1 &&
    ok "AC2: the log records the tool and field, never an argument value" ||
    bad "AC2: the decision log carried a value: $(cat "$DECISION_LOG")"

# ...and off by default.
: >"$DECISION_LOG"
run_hook mcp__loom__get_agent_metrics '{"role":"x; id"}' >/dev/null
[ ! -s "$DECISION_LOG" ] &&
    ok "decisionLog off (the default) -> nothing written" ||
    bad "decisionLog off still wrote: $(cat "$DECISION_LOG")"

# --- rule 2: a documented-enum value off its allow-list ----------------------
r="$(run_hook mcp__loom__get_agent_metrics '{"command":"exfiltrate"}')"
[ "$(decision_of "$r")" = deny ] &&
    ok "an off-enum 'command' DENIES even with no metacharacters" ||
    bad "an off-enum 'command' did not deny (got: $r)"
reason_has "$r" 'mcp-arg-off-allowlist' &&
    ok "the off-allowlist deny carries its own rule tag" ||
    bad "off-allowlist deny lacks its tag: $(reason_of "$r")"

# --- the reviewed free-text exemption ---------------------------------------
r="$(run_hook mcp__loom__send_terminal_input '{"terminal_id":"terminal-1","input":"ls | head\n"}')"
[ "$(decision_of "$r")" = allow ] &&
    ok "send_terminal_input.input (a reviewed free-text sink) is exempt" ||
    bad "send_terminal_input.input was denied (got: $r)"
r="$(run_hook mcp__loom__send_terminal_input '{"terminal_id":"terminal-1; id","input":"ok"}')"
[ "$(decision_of "$r")" = deny ] &&
    ok "...but a sibling field of an exempt one is still scanned" ||
    bad "terminal_id was not scanned alongside an exempt input (got: $r)"

# --- out of namespace --------------------------------------------------------
r="$(run_hook Bash '{"command":"rm -rf /; echo x"}')"
[ "$r" = "0|" ] &&
    ok "an out-of-namespace tool name is allowed untouched" ||
    bad "an out-of-namespace tool was not passed through (got: $r)"

# --- the category toggle -----------------------------------------------------
r="$(run_hook mcp__loom__get_agent_metrics '{"role":"x; id"}' LOOM_GUARD_MCP_TOOL_ARGS=0)"
[ "$r" = "0|" ] &&
    ok "toggle: LOOM_GUARD_MCP_TOOL_ARGS=0 -> allow" ||
    bad "toggle: env 0 did not disable the category (got: $r)"

cat >"$WS/.loom/config.json" <<'JSON'
{"guards": {"mcpToolArgs": false}}
JSON
r="$(run_hook mcp__loom__get_agent_metrics '{"role":"x; id"}')"
[ "$r" = "0|" ] &&
    ok "toggle: guards.mcpToolArgs=false -> allow" ||
    bad "toggle: config false did not disable the category (got: $r)"
r="$(run_hook mcp__loom__get_agent_metrics '{"role":"x; id"}' LOOM_GUARD_MCP_TOOL_ARGS=1)"
[ "$(decision_of "$r")" = deny ] &&
    ok "toggle: env 1 overrides a config false" ||
    bad "toggle: env did not beat config (got: $r)"
rm -f "$WS/.loom/config.json"
r="$(run_hook mcp__loom__get_agent_metrics '{"role":"x; id"}')"
[ "$(decision_of "$r")" = deny ] &&
    ok "toggle: the default (no config, no env) is ON" ||
    bad "toggle: the default was not ON (got: $r)"

# --- fail-open when nothing resolves ----------------------------------------
# No library at the expected relative path -> allow, silently. This is the
# guard's own failure mode; the ABSENT-HOOK-FILE case fails CLOSED one level up,
# in hook-wiring.sh rung 5 (covered by test-hook-wiring.sh).
NOLIB="$TMP/nolib"
mkdir -p "$NOLIB/.loom/hooks"
git init -q "$NOLIB"
cp "$DEFAULTS_HOOK" "$NOLIB/.loom/hooks/guard-mcp-tools.sh"
payload mcp__loom__get_agent_metrics '{"role":"x; id"}'
out="$( (cd "$NOLIB" && env LOOM_PROJECT_ROOT="$NOLIB" \
    bash "$NOLIB/.loom/hooks/guard-mcp-tools.sh" 2>/dev/null <"$PAYLOAD_FILE"))"
rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] &&
    ok "no locate-daemon-bin.sh reachable -> allow (fail-open, exit 0)" ||
    bad "missing library did not fail open (rc=$rc out=$out)"

# No resolvable daemon binary at all -> allow. Every tier of
# loom_locate_daemon_bin's precedence ladder has to be neutralised or this
# asserts nothing: LOOM_DAEMON_SELF_BIN/LOOM_DAEMON_BIN are cleared (tier 1),
# PATH is reduced to a binary-free directory (tier 3), LOOM_DAEMON_BIN_DIR and
# HOME point at empty directories so the MACHINE-LEVEL install cannot answer
# (tier 4 — it is probed by absolute path, so a reduced PATH does not hide it),
# and CARGO_TARGET_DIR points somewhere empty so no repo-local build answers
# (tiers 2/5).
EMPTY_TARGET="$TMP/empty-target"
BAREBIN="$TMP/barebin"
NOHOME="$TMP/nohome"
mkdir -p "$EMPTY_TARGET" "$BAREBIN" "$NOHOME"
for b in bash git cd dirname pwd cat env; do
    p="$(command -v "$b" 2>/dev/null)" && ln -sf "$p" "$BAREBIN/$b"
done
payload mcp__loom__get_agent_metrics '{"role":"x; id"}'
out="$( (
    cd "$WS" && env -u LOOM_DAEMON_SELF_BIN -u LOOM_DAEMON_BIN \
        LOOM_PROJECT_ROOT="$WS" CARGO_TARGET_DIR="$EMPTY_TARGET" \
        LOOM_DAEMON_BIN_DIR="$EMPTY_TARGET" HOME="$NOHOME" \
        PATH="$BAREBIN" bash "$HOOK" 2>/dev/null <"$PAYLOAD_FILE"
))"
rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] &&
    ok "no resolvable loom-daemon -> allow (fail-open, exit 0)" ||
    bad "an unresolvable daemon did not fail open (rc=$rc out=$out)"

# --- contract: always exit 0, and a deny is well-formed JSON -----------------
for args in '{"role":"x; id"}' '{"command":"costs"}' '{}'; do
    r="$(run_hook mcp__loom__get_agent_metrics "$args")"
    [ "${r%%|*}" = 0 ] &&
        ok "contract: exit 0 for args $args" ||
        bad "contract: non-zero exit for args $args (got: $r)"
done
r="$(run_hook mcp__loom__get_agent_metrics '{"role":"x; id"}')"
printf '%s' "${r#*|}" | jq -e '.hookSpecificOutput.hookEventName == "PreToolUse"' >/dev/null 2>&1 &&
    ok "contract: the deny document is valid JSON naming PreToolUse" ||
    bad "contract: malformed deny document: ${r#*|}"

# --- AC3: the settings-wiring contract --------------------------------------
# `loom-daemon check-guard-wiring`, not inline shell: the check is new
# executable logic, so it belongs in the daemon per
# .loom/docs/shell-language-policy.md (its own discriminating power is
# fixture-tested in loom-daemon/src/guard_wiring/tests.rs). Run against the
# REAL repo root, which is what the "Daemon Checks" CI gate does.
if "$LOOM_DAEMON_SELF_BIN" check-guard-wiring --repo-root "$REPO_ROOT" >/dev/null 2>&1; then
    ok "AC3: the mcp__loom__.* matcher + fail-closed floor contract passes"
else
    bad "AC3: loom-daemon check-guard-wiring failed on this checkout"
fi

# --- defaults/ vs .loom/ parity ---------------------------------------------
if [ ! -f "$INSTALLED_HOOK" ]; then
    bad "no installed copy at .loom/hooks/guard-mcp-tools.sh (check-hooks-defaults-parity.sh requires one)"
elif diff -q "$DEFAULTS_HOOK" "$INSTALLED_HOOK" >/dev/null 2>&1; then
    ok ".loom/ hook byte-identical to defaults/"
else
    bad ".loom/ hook has drifted from defaults/"
fi

echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
