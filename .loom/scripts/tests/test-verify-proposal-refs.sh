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

# Since #8656 the line-range check is `loom-daemon git-blob-lines --range`, so
# every range assertion below (fixtures 3 and 6) needs a BUILT binary. FATAL,
# not a skip: the whole point of #8656 is that the range check now follows a
# 120000 tree entry to the document it points at, and a suite that quietly
# skipped itself would report green while re-admitting exactly the bug.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "git-blob-lines"

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
echo "=== Fixture 6: .loom/docs symlinks (#8656) ==="
# Since #7842 every `.loom/docs/*.md` with a `defaults/docs/` counterpart is a
# SYMLINK on origin/main (tree mode 120000, target relative to the link's own
# directory). Reading such a path with `git show <rev>:<path> | wc -l` — what
# this script did before #8656 — measures the LINK-TARGET STRING, so every
# in-range citation under `.loom/docs/` was reported as a miss and the script
# BLOCKS FILING on it. This fixture reproduces that exact tree shape.
(
    cd "$FIXTURE_REPO" || exit 1
    mkdir -p defaults/docs .loom/docs
    seq 1 300 > defaults/docs/longdoc.md            # 300 lines, the real document
    ln -sf ../../defaults/docs/longdoc.md .loom/docs/longdoc.md
    ln -sf ../../defaults/docs/gone.md .loom/docs/dangling.md   # points at nothing
    git add -A
    git commit -qm "symlinked installed docs" >/dev/null
    git update-ref refs/remotes/origin/main refs/heads/main
)
# Guard the fixture itself: if these stop being 120000 entries the three cases
# below would pass for the wrong reason.
assert_eq "120000" \
    "$(git -C "$FIXTURE_REPO" ls-tree origin/main .loom/docs/longdoc.md | awk '{print $1}')" \
    "fixture: .loom/docs/longdoc.md is a symlink tree entry, as on real origin/main"

IN_RANGE_BODY="$BODY_DIR/symlink-in-range.md"
cat > "$IN_RANGE_BODY" <<'EOF'
See `.loom/docs/longdoc.md:100-200` — an in-range span of a 300-line document
reached through its installed symlink path.
EOF
OUT="$(run_vpr "$IN_RANGE_BODY")"
RC=$?
assert_eq "0" "$RC" "an in-range span under .loom/docs/ exits 0 (the symlink is followed)"
assert_contains "$OUT" "all references check out" "the in-range symlink citation is not reported as a miss"

OUT_OF_RANGE_BODY="$BODY_DIR/symlink-out-of-range.md"
cat > "$OUT_OF_RANGE_BODY" <<'EOF'
See `.loom/docs/longdoc.md:250-9999` — genuinely past the end of the document.
EOF
OUT="$(run_vpr "$OUT_OF_RANGE_BODY")"
RC=$?
assert_eq "1" "$RC" "a genuinely out-of-range span under .loom/docs/ still misses"
assert_contains "$OUT" "BAD LINE RANGE" "the out-of-range symlink citation is labeled"
assert_contains "$OUT" ".loom/docs/longdoc.md:250-9999" "the citation is quoted as the body wrote it"
assert_contains "$OUT" "defaults/docs/longdoc.md" "the message names the RESOLVED path, not the link"
assert_contains "$OUT" "has only 300 lines" "the message reports the resolved document's real line count"

DANGLING_BODY="$BODY_DIR/symlink-dangling.md"
cat > "$DANGLING_BODY" <<'EOF'
See `.loom/docs/dangling.md:1-5` — the link exists in the tree but its target
does not.
EOF
OUT="$(run_vpr "$DANGLING_BODY")"
RC=$?
assert_eq "0" "$RC" "a dangling symlink does not block filing — unreadable is inconclusive, not a disproof"
assert_contains "$OUT" "BROKEN SYMLINK" "a dangling symlink is reported as unreadable"
if [[ "$OUT" == *"has only 0 lines"* ]]; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: a dangling symlink must NOT be reported as a 0-line file"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: a dangling symlink is never reported as a 0-line file"
fi

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
