#!/bin/bash
# verify-token-precedence.sh - Manually confirm CLAUDE_CODE_OAUTH_TOKEN
# takes precedence over Keychain auth in the installed Claude Code CLI.
#
# Issue #3236 / curator AC: the lean-genius wrapper relies on the
# precedence assumption but the loom repo has no in-repo citation
# proving it.  Run this script once, on the operator's machine, to
# confirm the assumption holds for the installed Claude Code version.
#
# Usage:
#   ./verify-token-precedence.sh
#
# Exit codes:
#   0 - env-token correctly takes precedence over Keychain
#   1 - env-token did NOT take precedence (precedence claim is false)
#   2 - prerequisites missing (claude not installed, not logged in, etc.)

set -euo pipefail

if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' CLI not found in PATH" >&2
    exit 2
fi

# Ensure the local Keychain is logged in — otherwise the comparison is
# meaningless (env-token would be the only auth source).
if ! claude auth status --json 2>/dev/null | grep -q '"loggedIn": *true'; then
    echo "ERROR: 'claude auth status' reports not logged in." >&2
    echo "Run 'claude auth login' first so the Keychain has a valid token," >&2
    echo "then rerun this script." >&2
    exit 2
fi

echo "Step 1: Keychain-only auth status"
keychain_email=$(claude auth status --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("email",""))' 2>/dev/null || echo "")
echo "  Keychain account email: ${keychain_email:-<unknown>}"
echo ""

echo "Step 2: Auth status with env-token set to a deliberately bogus value"
echo "  CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-deliberately-bogus claude auth status --json"
set +e
env_output=$(CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-deliberately-bogus" \
    claude auth status --json 2>&1)
env_exit=$?
set -e

echo "  Exit code: ${env_exit}"
echo "  Output: ${env_output}"
echo ""

# If precedence holds, the bogus token should make auth FAIL or report a
# different account than the Keychain.  If precedence does NOT hold, the
# CLI silently falls back to Keychain and reports the same account.
env_logged_in=$(echo "${env_output}" | python3 -c \
    'import json,sys
try:
    print(json.load(sys.stdin).get("loggedIn", False))
except Exception:
    print("parse-error")' 2>/dev/null || echo "parse-error")

if [[ "${env_exit}" -ne 0 || "${env_logged_in}" != "True" ]]; then
    echo "PASS: env-token takes precedence over Keychain."
    echo "      (Bogus token caused auth to fail; Keychain was NOT consulted.)"
    exit 0
fi

env_email=$(echo "${env_output}" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("email",""))' 2>/dev/null || echo "")

if [[ -n "${keychain_email}" && -n "${env_email}" && \
      "${keychain_email}" == "${env_email}" ]]; then
    echo "FAIL: env-token did NOT take precedence."
    echo "      With a bogus env-token, auth still reported the Keychain"
    echo "      account (${keychain_email}).  Token rotation will NOT work"
    echo "      on this Claude Code version — it falls back to Keychain."
    exit 1
fi

echo "INCONCLUSIVE: could not determine precedence from output."
echo "  Keychain email: ${keychain_email:-<unknown>}"
echo "  Env-token email: ${env_email:-<unknown>}"
echo "  Output: ${env_output}"
exit 2
