#!/usr/bin/env bash
# test-jq-reserved-word-args.sh - Static repo-wide regression guard for #8293:
# jq 1.6 rejects a bare `$<keyword>` variable reference (e.g. `$label`,
# `$end`) even when bound via `--arg`/`--argjson` for that exact name, though
# newer jq (1.7+) accepts it. Renaming the internal jq variable (the output
# JSON field name is unaffected) fixes it. This test needs no jq 1.6 binary
# on PATH — it is a plain `git grep` over tracked `*.sh` files for the
# `--arg(json)? <jq-keyword>` shape.
#
# Two parts:
#   1. The guard run for real against this repo's own tracked tree -> must
#      find zero matches (that's the actual regression check).
#   2. The guard's pattern is exercised against a throwaway fixture to
#      confirm it has no false positives against near-miss names
#      (`--arg endpoint`, `--arg label_name`) and DOES fire against a
#      deliberately reintroduced `--arg end`.
#
# Usage:
#   ./defaults/scripts/tests/test-jq-reserved-word-args.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; [[ -n "${2:-}" ]] && echo "    $2"; }

if [[ -z "$REPO_ROOT" ]]; then
    echo "ERROR: not inside a git repository" >&2
    exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "jq not found on PATH -- skipping test-jq-reserved-word-args.sh (grep-only guard, but jq is used to keep this consistent with the other suites)"
fi

# The reserved-word list independently confirmed against a real jq-1.6
# binary (see issue #8293): `if then elif else end as def reduce foreach try
# catch import include label and or` fail as a bare `$name` reference on jq
# 1.6; `not __loc__ null true false` do not, so they are excluded.
#
# NOTE: this uses `git grep -P` (PCRE), not `-E` (POSIX ERE) as originally
# sketched in the issue body -- this repo's git grep -E does not support the
# `\b` word-boundary escape (it is silently inert, causing both false
# negatives on genuine hits and false "no false positives" on near-misses
# for the wrong reason -- verified empirically while writing this test). `-P`
# implements `\b` correctly and is available wherever the PCRE2 backend is
# compiled in (confirmed present in this repo's git build; the same is true
# for GitHub's ubuntu-latest runners).
KWRE='(if|then|elif|else|end|as|def|reduce|foreach|try|catch|import|include|label|and|or)'

# --- Test 1: the real repo tree has zero matches ----------------------------
echo "Test 1: the guard finds zero --arg(json) <jq-keyword> bindings in the live tree"
# This file's own path is excluded: its comments/echo text and fixture
# heredocs below deliberately spell out the exact violation shape (as
# documentation and as Test 2/3 fixtures), which would otherwise
# self-match once this file itself is tracked -- confirmed the hard way
# while writing this test.
SELF_PATH="defaults/scripts/tests/$(basename "$0")"
matches="$(cd "$REPO_ROOT" && git grep -noP -- "--arg(json)? +${KWRE}\b" -- '*.sh' 2>/dev/null | grep -v '\.loom/worktrees/' | grep -vF "$SELF_PATH:" || true)"
if [[ -z "$matches" ]]; then
    pass "no reserved-word --arg/--argjson bindings found"
else
    fail "no reserved-word --arg/--argjson bindings found" "$matches"
fi

# --- Test 2: the pattern has no false positives on near-miss names ---------
echo "Test 2: the pattern does not false-positive on near-miss names"
fixture_repo="$(mktemp -d)"
trap 'rm -rf "$fixture_repo"' EXIT
(cd "$fixture_repo" && git init -q)
cat > "$fixture_repo/clean.sh" <<'EOF'
#!/usr/bin/env bash
jq -n --arg endpoint "$ENDPOINT" '{endpoint:$endpoint}'
jq -n --arg label_name "$LABEL" '{label_name:$label_name}'
jq -n --argjson reasoning "$FLAG" '{reasoning:$reasoning}'
jq -n --arg claim_label "$LABEL" '{label:$claim_label}'
EOF
(cd "$fixture_repo" && git add clean.sh)
clean_matches="$(cd "$fixture_repo" && git grep -noP -- "--arg(json)? +${KWRE}\b" -- '*.sh' 2>/dev/null || true)"
if [[ -z "$clean_matches" ]]; then
    pass "near-miss names (endpoint, label_name, reasoning, claim_label) are not flagged"
else
    fail "near-miss names (endpoint, label_name, reasoning, claim_label) are not flagged" "$clean_matches"
fi

# --- Test 3: the pattern fires on a deliberately reintroduced violation ----
echo "Test 3: the pattern fires on a deliberately reintroduced --arg end/--argjson label"
cat > "$fixture_repo/dirty.sh" <<'EOF'
#!/usr/bin/env bash
jq -n --arg end "$END" '$end'
jq -n --argjson label "$LABEL" '$label'
EOF
(cd "$fixture_repo" && git add dirty.sh)
dirty_matches="$(cd "$fixture_repo" && git grep -noP -- "--arg(json)? +${KWRE}\b" -- '*.sh' 2>/dev/null || true)"
if grep -q 'dirty.sh:.*--arg end' <<<"$dirty_matches" && grep -q 'dirty.sh:.*--argjson label' <<<"$dirty_matches"; then
    pass "reintroduced --arg end and --argjson label are both caught"
else
    fail "reintroduced --arg end and --argjson label are both caught" "$dirty_matches"
fi
rm -rf "$fixture_repo"
trap - EXIT

# --- Summary -----------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
