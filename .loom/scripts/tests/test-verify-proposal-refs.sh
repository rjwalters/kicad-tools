#!/usr/bin/env bash
# test-verify-proposal-refs.sh - Tests for verify-proposal-refs.sh (issue
# #7658), the pre-file reference verifier for Hermit/Architect proposals.
#
# Hermit and Architect proposals bypass Curator — the only other role with a
# cited-path existence check — so a false citation (a path from a sibling
# repo, a nonexistent file, a stale line range, a false "N tracked files"
# count) reaches Champion unfiltered. verify-proposal-refs.sh is meant to run
# on the drafted body BEFORE `create-issue.sh`, blocking filing on any miss.
#
# This is a black-box test: verify-proposal-refs.sh is a full CLI script (no
# BASH_SOURCE guard to source functions from), so each case builds a real,
# tiny git repo with a fake `origin/main` ref (a local `update-ref`, no
# network) and runs the real script as a subprocess against a body-file
# fixture, asserting on exit code and output. Hermetic: no network, no live
# forge, no tokens.
#
# Usage:
#   ./.loom/scripts/tests/test-verify-proposal-refs.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
VPR="$SCRIPTS_DIR/verify-proposal-refs.sh"

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
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
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
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (missing literal in $file: $needle)"
    fi
}

if [[ ! -x "$VPR" ]]; then
    echo -e "${RED}FATAL${NC}: $VPR not found or not executable" >&2
    exit 2
fi

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) — the two layouts differ in depth, so probe both rather
# than hard-coding one (#6725, mirroring test-detect-dependency-cycle.sh).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
HERMIT_MD="$PROMPT_DIR/hermit.md"
ARCHITECT_MD="$PROMPT_DIR/architect.md"
CHAMPION_PROMO_MD="$PROMPT_DIR/champion-issue-promo.md"

FIXTURE_ROOT="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_ROOT" 2>/dev/null || true' EXIT

# --- Build a tiny fixture repo with a fake origin/main ref (no network: a
# local `update-ref` pointing at HEAD stands in for a fetched remote branch).
FIXTURE_REPO="$FIXTURE_ROOT/repo"
mkdir -p "$FIXTURE_REPO/src" "$FIXTURE_REPO/docs"
(
    cd "$FIXTURE_REPO" || exit 1
    git init -q -b main .
    git config user.email "test@example.com"
    git config user.name "Test"
    seq 1 5 > src/foo.py            # 5 lines
    printf 'line1\nline2\n' > docs/bar.md   # 2 lines
    git add .
    git commit -qm "init" >/dev/null
    git update-ref refs/remotes/origin/main refs/heads/main
)

BODY_DIR="$FIXTURE_ROOT/bodies"
mkdir -p "$BODY_DIR"

run_vpr() {
    LOOM_WORKSPACE="$FIXTURE_REPO" "$VPR" "$1" 2>&1
}

echo "=== Fixture 1: clean body (all references correct) ==="
CLEAN_BODY="$BODY_DIR/clean.md"
cat > "$CLEAN_BODY" <<'EOF'
This proposal cites `src/foo.py:3` and `docs/bar.md:1-2` as evidence, and
notes there are 0 tracked `.pyc` files in this fixture repo.
EOF
OUT="$(run_vpr "$CLEAN_BODY")"
RC=$?
assert_eq "0" "$RC" "clean body exits 0"
assert_contains "$OUT" "all references check out" "clean body reports success"

echo
echo "=== Fixture 2: missing file ==="
MISSING_BODY="$BODY_DIR/missing.md"
cat > "$MISSING_BODY" <<'EOF'
See `src/does-not-exist.py:10` and `verification/_repo_utils.py` for context —
neither exists in this repo (a sibling-checkout citation, #7658's motivating
incident).
EOF
OUT="$(run_vpr "$MISSING_BODY")"
RC=$?
assert_eq "1" "$RC" "missing file exits 1"
assert_contains "$OUT" "MISSING FILE" "missing-file miss is labeled"
assert_contains "$OUT" "src/does-not-exist.py" "the specific missing path is named"

echo
echo "=== Fixture 3: bad line range ==="
BADRANGE_BODY="$BODY_DIR/badrange.md"
cat > "$BADRANGE_BODY" <<'EOF'
See `src/foo.py:9999` — this line range runs well past the end of the file.
EOF
OUT="$(run_vpr "$BADRANGE_BODY")"
RC=$?
assert_eq "1" "$RC" "bad line range exits 1"
assert_contains "$OUT" "BAD LINE RANGE" "bad-line-range miss is labeled"
assert_contains "$OUT" "src/foo.py:9999" "the specific bad range is named"

echo
echo "=== Fixture 4: false tracked claim ==="
TRACKED_BODY="$BODY_DIR/tracked.md"
cat > "$TRACKED_BODY" <<'EOF'
There are six tracked `.pyc` files in this repo (there are actually none).
EOF
OUT="$(run_vpr "$TRACKED_BODY")"
RC=$?
assert_eq "1" "$RC" "false tracked claim exits 1"
assert_contains "$OUT" "FALSE TRACKED CLAIM" "false-tracked-claim miss is labeled"
assert_contains "$OUT" "git ls-files shows 0" "the actual git ls-files count is reported"

echo
echo "=== Fixture 5: a true tracked claim does NOT miss ==="
TRUE_TRACKED_BODY="$BODY_DIR/true-tracked.md"
cat > "$TRUE_TRACKED_BODY" <<'EOF'
There are two tracked `*.md` files in this fixture repo.
EOF
(
    cd "$FIXTURE_REPO" || exit 1
    printf 'x\n' > docs/second.md
    git add docs/second.md
    git commit -qm "add second md" >/dev/null
    git update-ref refs/remotes/origin/main refs/heads/main
)
OUT="$(run_vpr "$TRUE_TRACKED_BODY")"
RC=$?
assert_eq "0" "$RC" "a correct tracked-file count does not miss"

echo
echo "=== Usage / prerequisite errors ==="
OUT="$("$VPR" 2>&1)"
RC=$?
assert_eq "2" "$RC" "no body-file argument exits 2"

OUT="$(LOOM_WORKSPACE="$FIXTURE_REPO" "$VPR" "$BODY_DIR/does-not-exist.md" 2>&1)"
RC=$?
assert_eq "2" "$RC" "nonexistent body-file exits 2"

NOT_A_REPO="$(mktemp -d)"
OUT="$(LOOM_WORKSPACE="$NOT_A_REPO" "$VPR" "$CLEAN_BODY" 2>&1)"
RC=$?
assert_eq "2" "$RC" "workspace that is not a git repo exits 2"
rm -rf "$NOT_A_REPO"

echo
echo "--- Workspace rooting (#7658 Ask item 4): never a sibling checkout ---"
# A second, unrelated fixture repo (the "sibling checkout") that DOES contain
# the path the body cites. Pointing LOOM_WORKSPACE at the FIRST repo (which
# does not have it) must still miss — proving the script checks the
# dispatched workspace, not any path-matching sibling it could have found.
SIBLING_REPO="$FIXTURE_ROOT/sibling"
mkdir -p "$SIBLING_REPO/verification"
(
    cd "$SIBLING_REPO" || exit 1
    git init -q -b main .
    git config user.email "test@example.com"
    git config user.name "Test"
    echo "x" > verification/_repo_utils.py
    git add .
    git commit -qm "init" >/dev/null
    git update-ref refs/remotes/origin/main refs/heads/main
)
SIBLING_BODY="$BODY_DIR/sibling.md"
cat > "$SIBLING_BODY" <<'EOF'
See `verification/_repo_utils.py` for the shared helper.
EOF
OUT="$(run_vpr "$SIBLING_BODY")"
RC=$?
assert_eq "1" "$RC" "a path that only exists in a sibling checkout still misses against the real workspace"
assert_contains "$OUT" "verification/_repo_utils.py" "the sibling-only path is named as a miss"

echo
echo "--- Doc pins: Hermit / Architect / Champion wiring ---"
assert_doc_contains "$HERMIT_MD" "verify-proposal-refs.sh" \
    "hermit.md's pre-file step invokes the verifier"
assert_doc_contains "$HERMIT_MD" "Any miss blocks filing" \
    "hermit.md states the blocks-filing rule"
assert_doc_contains "$HERMIT_MD" "not present in this repo" \
    "hermit.md gives the 'not present in this repo' rewrite escape hatch"
assert_doc_contains "$ARCHITECT_MD" "verify-proposal-refs.sh" \
    "architect.md's pre-file step invokes the verifier"
assert_doc_contains "$ARCHITECT_MD" "Any miss blocks filing" \
    "architect.md states the blocks-filing rule"
assert_doc_contains "$ARCHITECT_MD" "not present in this repo" \
    "architect.md gives the 'not present in this repo' rewrite escape hatch"
assert_doc_contains "$CHAMPION_PROMO_MD" "verify-proposal-refs.sh" \
    "champion-issue-promo.md's criteria cite the verifier"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
