#!/usr/bin/env bash
# test-check-dependabot-labels.sh - Tests for check-dependabot-labels.sh (#7577)
#
# The gate under test exists because `labels:` in .github/dependabot.yml is
# scoped to the single `updates:` entry it appears in. Dependabot PRs for a
# manifest with no matching entry (including security updates, which are alert-
# driven rather than config-driven) get Dependabot's DEFAULT labels instead, so
# they never carry `loom:review-requested` and land in the Judge's unlabeled
# fallback queue — which applies no labels and therefore cannot clear them
# (#5455). That is what stranded #7474/#7473/#7396/#7395/#7138/#7137/#6890
# inside /mcp-loom and /dashboard/web for weeks.
#
# Verified behavior:
#   - a fully covered fixture exits 0
#   - an entry missing `loom:review-requested` fails and names the directory
#   - a lockfile-bearing manifest with no entry fails and names the manifest
#   - an npm entry pointing at a directory with no package.json fails
#   - a dependency-declaring manifest with no lockfile and no entry is an
#     advisory `note:` only (scaffolds such as quickstarts/), never a failure
#   - a repo with no .github/dependabot.yml is a clean no-op (exit 0)
#   - a config whose `updates:` list cannot be parsed fails loudly
#   - THIS repo's own .github/dependabot.yml passes (the #7577 regression guard)
#
# Hermetic: builds throwaway git trees under mktemp; no network, forge, or
# commits (only `git add`, so no git identity is required).
#
# Usage:
#   ./.loom/scripts/tests/test-check-dependabot-labels.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-dependabot-labels.sh"

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
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-dependabot-labels.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

if [[ ! -f "$SCRIPT" ]]; then
    echo "FATAL: script under test not found: $SCRIPT" >&2
    exit 1
fi

# --- Fixture helpers --------------------------------------------------------

# new_repo <name> -> prints the repo path
new_repo() {
    local repo="$WORKDIR/$1"
    mkdir -p "$repo/.github"
    git -C "$repo" init --quiet 2>/dev/null || git init --quiet "$repo"
    echo "$repo"
}

# add_manifest <repo> <dir-relative-to-repo> <deps-json> [lockfile]
add_manifest() {
    local repo="$1" dir="$2" deps="$3" lock="${4:-}"
    local target="$repo/$dir"
    mkdir -p "$target"
    printf '{\n  "name": "fixture",\n  "dependencies": %s\n}\n' "$deps" > "$target/package.json"
    [[ -n "$lock" ]] && printf '{}\n' > "$target/$lock"
    git -C "$repo" add -A >/dev/null 2>&1
}

# npm_entry <directory> [label-block]
npm_entry() {
    local dir="$1" labelled="${2:-yes}"
    printf '  - package-ecosystem: "npm"\n'
    printf '    directory: "%s"\n' "$dir"
    printf '    schedule:\n      interval: "weekly"\n'
    printf '    labels:\n      - "dependencies"\n'
    if [[ "$labelled" == "yes" ]]; then
        printf '      - "loom:review-requested"\n'
    fi
    printf '    groups:\n      all-dependencies:\n        patterns:\n          - "*"\n'
}

write_config() {
    local repo="$1"
    { printf 'version: 2\nupdates:\n'; cat; } > "$repo/.github/dependabot.yml"
}

run_check() {
    "$SCRIPT" "$1" 2>&1
}

# --- Test 1: fully covered fixture passes -----------------------------------
echo "Test 1: a fully covered repo passes"
REPO="$(new_repo covered)"
add_manifest "$REPO" "." '{}' "package-lock.json"
add_manifest "$REPO" "pkg" '{"left-pad":"1.0.0"}' "package-lock.json"
write_config "$REPO" <<EOF
$(npm_entry "/")
$(npm_entry "/pkg")
EOF
OUT="$(run_check "$REPO")"; RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "exit 0 on a fully covered repo"
else
    fail "expected exit 0, got $RC: $OUT"
fi

# --- Test 2: entry without the review label fails ---------------------------
echo "Test 2: an entry missing loom:review-requested fails"
REPO="$(new_repo unlabeled-entry)"
add_manifest "$REPO" "." '{}' "package-lock.json"
add_manifest "$REPO" "pkg" '{"left-pad":"1.0.0"}' "package-lock.json"
write_config "$REPO" <<EOF
$(npm_entry "/")
$(npm_entry "/pkg" "no")
EOF
OUT="$(run_check "$REPO")"; RC=$?
if [[ "$RC" -ne 0 ]]; then
    pass "non-zero exit when an entry omits the label"
else
    fail "expected non-zero exit, got 0: $OUT"
fi
if grep -q "/pkg" <<< "$OUT"; then
    pass "the offending directory is named"
else
    fail "expected '/pkg' in output: $OUT"
fi

# --- Test 3: uncovered lockfile-bearing manifest fails ----------------------
echo "Test 3: a lockfile-bearing manifest with no entry fails"
REPO="$(new_repo uncovered)"
add_manifest "$REPO" "." '{}' "package-lock.json"
add_manifest "$REPO" "mcp-loom" '{"hono":"4.0.0"}' "package-lock.json"
write_config "$REPO" <<EOF
$(npm_entry "/")
EOF
OUT="$(run_check "$REPO")"; RC=$?
if [[ "$RC" -ne 0 ]]; then
    pass "non-zero exit when a lockfile-bearing manifest is uncovered"
else
    fail "expected non-zero exit, got 0: $OUT"
fi
if grep -q "mcp-loom/package.json" <<< "$OUT"; then
    pass "the uncovered manifest is named"
else
    fail "expected 'mcp-loom/package.json' in output: $OUT"
fi

# --- Test 4: entry pointing at a nonexistent manifest fails -----------------
echo "Test 4: an npm entry with no package.json at its directory fails"
REPO="$(new_repo stale-entry)"
add_manifest "$REPO" "." '{}' "package-lock.json"
write_config "$REPO" <<EOF
$(npm_entry "/")
$(npm_entry "/moved-away")
EOF
OUT="$(run_check "$REPO")"; RC=$?
if [[ "$RC" -ne 0 ]] && grep -q "moved-away" <<< "$OUT"; then
    pass "non-zero exit naming the stale entry directory"
else
    fail "expected non-zero exit naming '/moved-away' (rc=$RC): $OUT"
fi

# --- Test 5: lockfile-less scaffold is advisory only ------------------------
echo "Test 5: a lockfile-less scaffold manifest is a note, not a failure"
REPO="$(new_repo scaffold)"
add_manifest "$REPO" "." '{}' "package-lock.json"
add_manifest "$REPO" "quickstarts/api" '{"hono":"4.0.0"}'
write_config "$REPO" <<EOF
$(npm_entry "/")
EOF
OUT="$(run_check "$REPO")"; RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "exit 0 despite the uncovered scaffold"
else
    fail "expected exit 0, got $RC: $OUT"
fi
if command -v jq >/dev/null 2>&1; then
    if grep -q "^note: quickstarts/api/package.json" <<< "$OUT"; then
        pass "the scaffold is reported as an advisory note"
    else
        fail "expected an advisory note for quickstarts/api: $OUT"
    fi
else
    echo "  SKIP: jq not installed — advisory note check"
fi

# --- Test 6: no dependabot.yml is a clean no-op -----------------------------
echo "Test 6: a repo with no dependabot.yml is a no-op"
REPO="$(new_repo no-config)"
add_manifest "$REPO" "." '{}' "package-lock.json"
OUT="$(run_check "$REPO")"; RC=$?
if [[ "$RC" -eq 0 ]] && grep -q "nothing to check" <<< "$OUT"; then
    pass "exit 0 with a 'nothing to check' message"
else
    fail "expected a clean no-op (rc=$RC): $OUT"
fi

# --- Test 7: an unparseable config fails loudly -----------------------------
echo "Test 7: a config with no parseable entries fails loudly"
REPO="$(new_repo unparseable)"
add_manifest "$REPO" "." '{}' "package-lock.json"
printf 'version: 2\nupdates: []\n' > "$REPO/.github/dependabot.yml"
OUT="$(run_check "$REPO")"; RC=$?
if [[ "$RC" -ne 0 ]] && grep -q "parsed zero entries" <<< "$OUT"; then
    pass "non-zero exit with an explicit parse failure message"
else
    fail "expected a loud parse failure (rc=$RC): $OUT"
fi

# --- Test 8: this repo's own config passes (#7577 regression guard) ---------
echo "Test 8: this repository's .github/dependabot.yml passes"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.github/dependabot.yml" ]]; then
    OUT="$(run_check "$REPO_ROOT")"; RC=$?
    if [[ "$RC" -eq 0 ]]; then
        pass "the live dependabot.yml covers every lockfile-bearing manifest"
    else
        fail "the live dependabot.yml failed the gate (rc=$RC): $OUT"
    fi
else
    echo "  SKIP: no live .github/dependabot.yml to check"
fi

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]] || exit 1
exit 0
