#!/usr/bin/env bash
# test-verify-install-scope.sh - Regression tests for verify-install.sh manifest
# scoping (issue #3600, Part B of #3597).
#
# verify-install.sh previously built its checksum manifest from a hard-coded
# directory walk and hashed CLAUDE.md whole-file. In a multi-tool consumer repo
# (Loom + a sibling installer such as Anvil + Repo Skills) that produced false
# DRIFT:
#
#   1. `find .claude/agents -name '*.md'` captured sibling agent shims Loom
#      never shipped (e.g. anvil-*.md).
#   2. A whole-file CLAUDE.md hash flagged drift whenever a sibling installer
#      edited CLAUDE.md *outside* Loom's <!-- BEGIN/END LOOM ORCHESTRATION -->
#      marker block.
#
# The fix derives the tracked-file set from .loom/install-metadata.json
# "installed_files" (Loom's ownership boundary) and hashes CLAUDE.md over its
# Loom marker region only. It also bumps the manifest schema to v2, so a stale
# v1 manifest reports "schema outdated" instead of false drift.
#
# Test strategy: seed a temp git repo with a hand-written install-metadata.json,
# a CLAUDE.md with a Loom block plus sibling sections, a Loom agent, and a
# sibling agent shim, then invoke the real script from the source checkout and
# assert on the generated manifest and verify exit codes.
#
# Usage:
#   bash .loom/scripts/tests/test-verify-install-scope.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
VERIFY_SCRIPT="$REPO_ROOT/defaults/scripts/verify-install.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (expected '$expected', got '$actual')"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (found unexpected '$needle')"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (missing '$needle')"
        echo "$haystack" | sed 's/^/      /'
    fi
}

if [[ ! -f "$VERIFY_SCRIPT" ]]; then
    echo "ERROR: $VERIFY_SCRIPT not found" >&2
    exit 1
fi
if ! command -v jq &>/dev/null; then
    echo "SKIP: jq not available (required by verify-install.sh verify mode)" >&2
    exit 0
fi

# Seed a temp git repo with a Loom install. Args set which optional bits exist.
# Writes CLAUDE.md with a Loom marker block flanked by sibling sections, one
# Loom agent, one sibling agent shim, settings.json, a user-edited config.json,
# and an install-metadata.json listing only Loom-owned paths.
seed_repo() {
    local tmp
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/loom-vscope.XXXXXX")
    (
        cd "$tmp" || exit 1
        git init -q .
        git config user.email "test@example.com"
        git config user.name "Test"
        mkdir -p .loom .claude/agents

        cat > CLAUDE.md <<'EOF'
# Consumer Project

Sibling intro section owned by another tool.

<!-- BEGIN LOOM ORCHESTRATION -->
## Loom
Loom-managed content lives here.
<!-- END LOOM ORCHESTRATION -->

## Sibling Tail Section
More sibling-owned content.
EOF

        echo "loom builder agent" > .claude/agents/loom-builder.md
        echo "anvil sibling shim" > .claude/agents/anvil-foo.md
        printf '{"schema":"loom"}\n' > .claude/settings.json
        printf '{"nextAgentNumber":9,"user":"edited"}\n' > .loom/config.json

        # install-metadata.json lists only Loom-owned paths; anvil-foo.md is
        # deliberately absent (a sibling installer, not Loom, shipped it).
        cat > .loom/install-metadata.json <<'EOF'
{
  "loom_version": "0.10.9",
  "loom_commit": "deadbee",
  "installed_files": [".claude/agents/loom-builder.md",".claude/settings.json","CLAUDE.md",".loom/config.json"]
}
EOF
    )
    echo "$tmp"
}

# ---------------------------------------------------------------------------
# Case 1: sibling agent shim never enters the manifest.
# ---------------------------------------------------------------------------
echo "Case 1: sibling agent shim excluded from manifest"
REPO=$(seed_repo)
( cd "$REPO" && bash "$VERIFY_SCRIPT" generate --quiet )
MANIFEST=$(cat "$REPO/.loom/manifest.json")
assert_not_contains "$MANIFEST" "anvil-foo.md" "Case 1: anvil-foo.md absent from .loom/manifest.json"
assert_contains "$MANIFEST" "loom-builder.md" "Case 1: Loom agent present in manifest"
assert_contains "$MANIFEST" '"version": 2' "Case 1: manifest schema is v2"
# User-mutable config.json must not be tracked (false-drift guard).
assert_not_contains "$MANIFEST" '".loom/config.json"' "Case 1: user config.json not tracked"
rm -rf "$REPO"
echo ""

# ---------------------------------------------------------------------------
# Case 2: sibling CLAUDE.md edit outside markers is clean; inside is drift.
# ---------------------------------------------------------------------------
echo "Case 2: CLAUDE.md region-scoped hashing"
REPO=$(seed_repo)
( cd "$REPO" && bash "$VERIFY_SCRIPT" generate --quiet )

# Edit OUTSIDE the Loom markers (both the intro and the tail sibling sections).
cat > "$REPO/CLAUDE.md" <<'EOF'
# Consumer Project

Sibling intro section HEAVILY rewritten by another tool.

<!-- BEGIN LOOM ORCHESTRATION -->
## Loom
Loom-managed content lives here.
<!-- END LOOM ORCHESTRATION -->

## Sibling Tail Section
Even more sibling-owned content appended later, plus a new paragraph.
EOF
( cd "$REPO" && bash "$VERIFY_SCRIPT" verify --quiet )
assert_eq "0" "$?" "Case 2a: edit outside Loom markers -> verify exit 0"

# Edit INSIDE the Loom markers -> real drift.
cat > "$REPO/CLAUDE.md" <<'EOF'
# Consumer Project

Sibling intro.

<!-- BEGIN LOOM ORCHESTRATION -->
## Loom
Loom-managed content lives here — but TAMPERED inside the block.
<!-- END LOOM ORCHESTRATION -->

## Sibling Tail Section
tail.
EOF
( cd "$REPO" && bash "$VERIFY_SCRIPT" verify --quiet )
assert_eq "1" "$?" "Case 2b: edit inside Loom markers -> verify exit 1 (drift)"

# Remove the Loom block entirely -> missing-markers drift.
printf '# Consumer Project\n\nNo Loom block remains.\n' > "$REPO/CLAUDE.md"
( cd "$REPO" && bash "$VERIFY_SCRIPT" verify --quiet )
assert_eq "1" "$?" "Case 2c: removed Loom markers -> verify exit 1 (drift)"
rm -rf "$REPO"
echo ""

# ---------------------------------------------------------------------------
# Case 3: a v1-schema manifest reports "schema outdated", not false drift.
# ---------------------------------------------------------------------------
echo "Case 3: outdated v1 manifest schema"
REPO=$(seed_repo)
( cd "$REPO" && bash "$VERIFY_SCRIPT" generate --quiet )
# Downgrade the recorded schema version to 1 in place.
sed 's/"version": 2/"version": 1/' "$REPO/.loom/manifest.json" > "$REPO/.loom/manifest.json.tmp"
mv "$REPO/.loom/manifest.json.tmp" "$REPO/.loom/manifest.json"

HUMAN_OUT=$( cd "$REPO" && bash "$VERIFY_SCRIPT" verify 2>&1 )
HUMAN_EXIT=$?
assert_eq "5" "$HUMAN_EXIT" "Case 3a: v1 manifest -> verify exit 5 (schema outdated, not 1)"
assert_contains "$HUMAN_OUT" "schema outdated" "Case 3b: human message says schema outdated"
assert_not_contains "$HUMAN_OUT" "DRIFT DETECTED" "Case 3c: no per-file drift reported for v1"

JSON_OUT=$( cd "$REPO" && bash "$VERIFY_SCRIPT" verify --json 2>&1 )
assert_contains "$JSON_OUT" "manifest_schema_outdated" "Case 3d: json error is manifest_schema_outdated"

# Auto (no-arg) mode self-heals a v1 manifest by regenerating it.
( cd "$REPO" && bash "$VERIFY_SCRIPT" >/dev/null 2>&1 )
assert_eq "0" "$?" "Case 3e: auto mode regenerates v1 manifest (exit 0)"
REGEN=$(cat "$REPO/.loom/manifest.json")
assert_contains "$REGEN" '"version": 2' "Case 3f: auto mode rewrote manifest to v2"
rm -rf "$REPO"
echo ""

# ---------------------------------------------------------------------------
# Case 4: no install-metadata.json -> warned legacy directory-walk fallback.
# ---------------------------------------------------------------------------
echo "Case 4: legacy fallback when metadata absent"
FBTMP=$(mktemp -d "${TMPDIR:-/tmp}/loom-vscope-fb.XXXXXX")
(
    cd "$FBTMP" || exit 1
    git init -q .
    git config user.email "test@example.com"
    git config user.name "Test"
    mkdir -p .loom/roles .loom/scripts
    echo "builder role" > .loom/roles/builder.md
    echo "helper script" > .loom/scripts/helper.sh
    printf '# top\n\n<!-- BEGIN LOOM ORCHESTRATION -->\nloom\n<!-- END LOOM ORCHESTRATION -->\n' > CLAUDE.md
)
FB_OUT=$( cd "$FBTMP" && bash "$VERIFY_SCRIPT" generate 2>&1 )
assert_contains "$FB_OUT" "falling back to legacy directory walk" "Case 4a: warns about fallback"
FB_COUNT=$(jq '.file_count' "$FBTMP/.loom/manifest.json" 2>/dev/null || echo 0)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$FB_COUNT" -ge 2 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 4b: fallback produced a non-empty manifest ($FB_COUNT files)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 4b: fallback manifest too small ($FB_COUNT files)"
fi
( cd "$FBTMP" && bash "$VERIFY_SCRIPT" verify --quiet )
assert_eq "0" "$?" "Case 4c: fallback verify is clean (exit 0)"
rm -rf "$FBTMP"
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "==============================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
exit 0
