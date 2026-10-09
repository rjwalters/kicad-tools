#!/usr/bin/env bash
# test-guard-forge-egress.sh — pins the guard-loom-workflow.sh block that runs
# `loom-daemon forge egress guard --for-command` (#9989, tag loom:forge-egress).
#
# The classifier, policy gating and toggle are Rust and tested there
# (loom-daemon/src/forge_egress/guard_tests.rs, plus the real-binary hook
# matrix in loom-daemon/tests/forge_egress_guard_hook.rs). This suite tests
# only the WIRING, against a stub daemon selected via LOOM_DAEMON_SELF_BIN, so
# it is hermetic and needs no build:
#   - exit 1 + the `BLOCKED [routing.denied-by-guard]` prefix DENIES, verbatim
#   - exit 0 (no policy / observe / toggle off / no match) allows
#   - a daemon too old to have the verb (exit 2) allows: rule inert
#   - exit 1 WITHOUT the prefix allows (a crash is not a verdict)
#   - the prefilter keeps the daemon off commands that cannot match
#   - the daemon sees the MASKED text: a --body mention never reaches it

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

STUB="$TMP/loom-daemon"
cat > "$STUB" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_CALLS"
[[ "$1 $2 $3" == "forge egress guard" ]] || exit 2
case "$STUB_MODE" in
    deny)    echo "BLOCKED [routing.denied-by-guard]: stub reason (origin: machine)"; exit 1 ;;
    allow)   exit 0 ;;
    old)     echo "error: unrecognized subcommand 'guard'" >&2; exit 2 ;;
    garbage) echo "thread 'main' panicked"; exit 1 ;;
esac
STUB
chmod +x "$STUB"

decision() { # <command> -> deny|ask|allow; reason in $TMP/reason
    local d
    : > "$TMP/calls"
    d="$(jq -n --arg cmd "$1" --arg cwd "$REPO" '{tool_name:"Bash", tool_input:{command:$cmd}, cwd:$cwd}' \
        | LOOM_DAEMON_SELF_BIN="$STUB" STUB_CALLS="$TMP/calls" STUB_MODE="$STUB_MODE" \
          LOOM_GUARD_DECISION_LOG=0 bash "$GUARD" 2>/dev/null)"
    printf '%s' "$d" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""' > "$TMP/reason" 2>/dev/null
    d="$(printf '%s' "$d" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null)"
    printf '%s' "${d:-allow}"
}
guard_calls() { grep -c '^forge egress guard' "$TMP/calls" 2>/dev/null || true; }

CURL='curl -sS https://api.github.com/zen'

STUB_MODE=deny
[ "$(decision "$CURL")" = deny ] && ok "exit 1 + prefix denies" || bad "exit 1 + prefix must deny"
grep -q '^BLOCKED \[routing.denied-by-guard\]: stub reason (origin: machine)$' "$TMP/reason" \
    && ok "the daemon's reason is the denial, verbatim" || bad "reason not passed through: $(cat "$TMP/reason")"
for cmd in "bash -c \"$CURL\"" "eval \"$CURL\"" "echo \"$CURL\" | sh" "GH_HOST=x gh issue list" \
           "/opt/homebrew/bin/gh issue list" "pip install PyGithub" "env -i gh api user"; do
    [ "$(decision "$cmd")" = deny ] && ok "prefilter passes: $cmd" || bad "prefilter dropped: $cmd"
done
# The shell joins these back into `gh`; a literal-`gh` prefilter used to skip
# the daemon for them (PR #10543 review).
ESCAPED=('g\h auth login' 'g\h api https://api.example.test/user' 'g""h auth login' "g''h auth login")
for cmd in "${ESCAPED[@]}"; do
    [ "$(decision "$cmd")" = deny ] && [ "$(guard_calls)" = 1 ] \
        && ok "prefilter passes escaped spelling: $cmd" || bad "prefilter dropped escaped spelling: $cmd"
done
grep -q -- '^forge egress guard --for-command ' "$TMP/calls" 2>/dev/null \
    && ok "the daemon is called as \`forge egress guard --for-command\`" || bad "no --for-command call recorded"

for cmd in "ls -la" "cargo test -p loom-daemon" "git status" "make build"; do
    [ "$(decision "$cmd")" = allow ] && [ "$(guard_calls)" = 0 ] \
        && ok "prefilter skips the daemon: $cmd" || bad "daemon invoked or denied for: $cmd"
done

decision "gh issue comment 1 --body 'never curl https://api.github.com'" >/dev/null
if grep -q 'api.github.com' "$TMP/calls" 2>/dev/null; then
    bad "the daemon saw an unmasked --body value"
else
    ok "the daemon sees masked text (--body mention never reaches it)"
fi

STUB_MODE=allow
[ "$(decision "$CURL")" = allow ] && ok "exit 0 allows (no policy / observe / toggle off)" || bad "exit 0 must allow"
for cmd in "${ESCAPED[@]}"; do
    [ "$(decision "$cmd")" = allow ] && ok "exit 0 allows escaped spelling: $cmd" || bad "exit 0 must allow: $cmd"
done

STUB_MODE=old
[ "$(decision "$CURL")" = allow ] && ok "daemon without \`forge egress guard\` (exit 2) allows: rule inert" \
    || bad "an older daemon must never deny"
[ "$(decision "${ESCAPED[0]}")" = allow ] && ok "older daemon allows escaped spelling: rule inert" \
    || bad "an older daemon must never deny an escaped spelling"

STUB_MODE=garbage
[ "$(decision "$CURL")" = allow ] && ok "exit 1 without the deny prefix allows" || bad "a crash must not deny"

missing="$(jq -n --arg cmd "$CURL" --arg cwd "$REPO" '{tool_name:"Bash", tool_input:{command:$cmd}, cwd:$cwd}' \
    | LOOM_DAEMON_SELF_BIN="$TMP/nonexistent" bash "$GUARD" 2>/dev/null)"
[ -z "$missing" ] && ok "no daemon binary allows" || bad "a missing daemon must allow: $missing"

echo
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
