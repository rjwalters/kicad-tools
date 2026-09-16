#!/usr/bin/env bash
# Test suite for check-guards-installed.sh (issue #7761).
#
# Usage: ./defaults/scripts/tests/test-check-guards-installed.sh
#
# The script's own `--self-test` covers the status matrix (ok / not-executable /
# --fix / missing / machine-level-degraded / non-Loom-workspace) against
# purpose-built fixtures; this suite drives it as a real CLI — exit codes, flag
# handling, output routing — and pins the contract the rest of the install
# tooling relies on: exit 2 means "a wired hook cannot run", nothing else does.
#
# Subject resolved from the INSTALLED copy first (a Loom-installed consumer repo
# has no defaults/ tree), falling back to defaults/ for Loom's own source tree.
#
# Exit 0 = all pass, 1 = one or more failures.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

SUBJECT="$REPO_ROOT/.loom/scripts/check-guards-installed.sh"
[[ -r "$SUBJECT" ]] || SUBJECT="$REPO_ROOT/defaults/scripts/check-guards-installed.sh"

PASS=0
FAIL=0
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

ok()  { PASS=$((PASS + 1)); printf "  ${GREEN}✓${NC} %s\n" "$1"; }
bad() { FAIL=$((FAIL + 1)); printf "  ${RED}✗${NC} %s\n" "$1"; [[ -n "${2:-}" ]] && printf "      %s\n" "$2"; }
assert_eq() { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected [$2], got [$3]"; fi; }
assert_contains() { if [[ "$3" == *"$2"* ]]; then ok "$1"; else bad "$1" "expected to contain [$2], got [$3]"; fi; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
EMPTY_LOOM_HOME="$TMPROOT/empty-loom-home"
mkdir -p "$EMPTY_LOOM_HOME"

run() { # <args...> -> sets RC/OUT/ERR
    RC=0
    OUT="$(env LOOM_HOME="$EMPTY_LOOM_HOME" bash "$SUBJECT" "$@" 2>"$TMPROOT/err")" || RC=$?
    ERR="$(cat "$TMPROOT/err")"
}

workspace() { # <name> -> path to a Loom workspace wiring guard-destructive.sh
    local ws="$TMPROOT/$1"
    mkdir -p "$ws/.loom/hooks" "$ws/.claude"
    cat > "$ws/.claude/settings.json" <<'JSON'
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "bash -c 'exec \"$W/.loom/hooks/guard-destructive.sh\"'" }
        ]
      }
    ]
  }
}
JSON
    printf '%s' "$ws"
}

echo "=== check-guards-installed.sh (#7761) ==="

run --self-test
assert_eq "--self-test passes" "0" "$RC"
assert_contains "--self-test reports its tally" "0 failed" "$OUT"

run --help
assert_eq "--help exits 0" "0" "$RC"
assert_contains "--help documents the exit codes" "Exit codes:" "$OUT"

run --bogus-flag
assert_eq "an unknown flag is a usage error (exit 1)" "1" "$RC"

run --root "$TMPROOT/does-not-exist"
assert_eq "a non-existent root is a usage error (exit 1)" "1" "$RC"

WS="$(workspace ws-ok)"
printf '#!/bin/sh\nexit 0\n' > "$WS/.loom/hooks/guard-destructive.sh"
chmod +x "$WS/.loom/hooks/guard-destructive.sh"
run --root "$WS"
assert_eq "a healthy install exits 0" "0" "$RC"
assert_contains "a healthy install says so" "OK" "$OUT"

run --root "$WS" --quiet
assert_eq "--quiet on a healthy install exits 0" "0" "$RC"
assert_eq "--quiet on a healthy install prints nothing" "" "$OUT$ERR"

WS="$(workspace ws-noexec)"
printf '#!/bin/sh\nexit 0\n' > "$WS/.loom/hooks/guard-destructive.sh"
chmod -x "$WS/.loom/hooks/guard-destructive.sh"
run --root "$WS"
assert_eq "a non-executable wired hook exits 2" "2" "$RC"
assert_contains "the offender is named on stderr" "guard-destructive.sh" "$ERR"
assert_contains "the repair is spelled out" "chmod +x" "$ERR"

run --root "$WS" --quiet
assert_eq "--quiet still reports problems (it silences OK lines only)" "2" "$RC"
assert_contains "--quiet still names the offender" "guard-destructive.sh" "$ERR"

run --root "$WS" --fix
assert_eq "--fix repairs the executable bit and passes" "0" "$RC"
if [[ -x "$WS/.loom/hooks/guard-destructive.sh" ]]; then
    ok "--fix left the hook executable"
else
    bad "--fix left the hook executable"
fi

WS="$(workspace ws-missing)"
run --root "$WS"
assert_eq "a missing wired hook exits 2" "2" "$RC"
assert_contains "a missing hook is reported as MISSING" "MISSING" "$ERR"

PLAIN="$TMPROOT/plain"
mkdir -p "$PLAIN"
run --root "$PLAIN"
assert_eq "a non-Loom workspace exits 0" "0" "$RC"
assert_contains "a non-Loom workspace says why it is a no-op" "not a Loom workspace" "$OUT"

# This repo itself must pass — the check is only useful if the source tree it
# ships from is honest about its own install.
run --root "$REPO_ROOT"
assert_eq "the Loom source tree's own install passes" "0" "$RC"

echo ""
printf 'test-check-guards-installed: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
