#!/usr/bin/env bash
# test-guard-secret-scan.sh — pins the guard-loom-workflow.sh block that runs
# `loom-daemon secret-scan --for-command` on a git commit/push (#9133).
#
# The scanner itself is Rust and tested there (loom-daemon/src/secret_scan/
# tests.rs). This suite tests only the WIRING, against a stub daemon selected
# via LOOM_DAEMON_SELF_BIN, so it is hermetic and needs no build:
#   - findings (exit 1 with `(fp …)` lines) DENY, and the reason carries them
#   - a clean scan (exit 0) allows
#   - a daemon too old to have the subcommand (exit 2) allows: fail-open,
#     like every other unavailable check in the guard
#   - a command that is not a git commit/push never invokes the scanner
#   - the value itself never appears in the decision (the stub never has one;
#     this pins that the guard adds nothing of its own)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GUARD="$(cd "$SCRIPT_DIR/.." && pwd)/guard-loom-workflow.sh"

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'ok:   %s\n' "$*"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$*"; }

command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
git init -q "$REPO"

# The stub records each invocation and answers per STUB_MODE.
STUB="$TMP/loom-daemon"
cat > "$STUB" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_CALLS"
case "$STUB_MODE" in
    found)
        echo "secret-scan: .loom/tokens.bak/a.token:1: anthropic-oauth (fp 0badf00d)" >&2
        echo "secret-scan: 1 credential-shaped value(s) found. Values are never printed." >&2
        exit 1 ;;
    clean) exit 0 ;;
    old)   echo "error: unrecognized subcommand 'secret-scan'" >&2; exit 2 ;;
esac
STUB
chmod +x "$STUB"

decision() { # <command> -> deny|ask|allow
    local d
    : > "$TMP/calls"
    d="$(jq -n --arg cmd "$1" --arg cwd "$REPO" '{tool_name:"Bash", tool_input:{command:$cmd}, cwd:$cwd}' \
        | LOOM_DAEMON_SELF_BIN="$STUB" STUB_CALLS="$TMP/calls" STUB_MODE="$STUB_MODE" bash "$GUARD" 2>/dev/null)"
    printf '%s' "$d" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""' > "$TMP/reason" 2>/dev/null
    d="$(printf '%s' "$d" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null)"
    printf '%s' "${d:-allow}"
}

STUB_MODE=found
[ "$(decision "git add -A && git commit -m 'chore: resync installed Loom surfaces'")" = deny ] \
    && ok "findings on git commit deny" || bad "findings on git commit did not deny"
grep -qF '(fp 0badf00d)' "$TMP/reason" && ok "the deny reason carries the finding" || bad "deny reason lacks the finding: $(cat "$TMP/reason")"
grep -q -- '--for-command' "$TMP/calls" && ok "the scanner is asked with --for-command" || bad "scanner not invoked as expected"
[ "$(decision "git -C . push origin HEAD")" = deny ] && ok "findings on git push deny" || bad "findings on git push did not deny"

STUB_MODE=clean
[ "$(decision "git commit -m ok")" = allow ] && ok "a clean scan allows" || bad "a clean scan did not allow"

STUB_MODE=old
[ "$(decision "git commit -m ok")" = allow ] && ok "a daemon without secret-scan (exit 2) allows" || bad "exit 2 did not allow"

STUB_MODE=found
[ "$(decision "git status && ls commit")" = allow ] && ok "a non-commit command allows" || bad "a non-commit command was denied"
[ ! -s "$TMP/calls" ] && ok "a non-commit command never invokes the scanner" || bad "the scanner ran for a non-commit command"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
