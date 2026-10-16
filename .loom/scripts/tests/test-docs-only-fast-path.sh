#!/usr/bin/env bash
# test-docs-only-fast-path.sh - Regression tests for the docs-only fast path
# (#6134): Judge/Champion's mechanical eligibility check that lets a PR whose
# diff is confined to WORK_LOG.md/WORK_PLAN.md/README.md skip the full code
# evaluation + merge-risk judgment cycle, while still landing safely.
#
# Judge's "Docs-Only Fast Path" section and Champion's criterion #2 "Docs-only
# fast path" subsection are prose an LLM instance reads and executes, not a
# standalone script (same situation as test-champion-critical-file-check.sh)
# — so this file mirrors the documented `docs_only_fast_path_check()`
# function in a local copy and pins the shipped markdown's exact commands
# with `assert_doc_contains`, catching drift between the two.
#
# What this asserts:
#   1. The mirrored eligibility check correctly returns ELIGIBLE for diffs
#      confined to any non-empty subset of {WORK_LOG.md, WORK_PLAN.md,
#      README.md}.
#   2. It correctly rejects ANY diff that includes a file outside that exact
#      allowlist — including a nested README.md/WORK_LOG.md (path match, not
#      substring), an empty diff, and the two edge cases the issue's Test
#      Plan calls out explicitly: `.github/workflows/*.yml` and
#      `.loom/config.json` alongside otherwise-docs-only changes.
#   3. Judge's and Champion's shipped markdown both define an identical
#      allowlist and both independently re-derive the file list via the
#      paginated files API (never `gh pr view --json files`, and never by
#      trusting a marker/label).
#   4. Guide's create_docs_pr() emits the informational marker but the
#      shipped Judge/Champion markdown never gates eligibility on it.
#
# Usage:
#   ./.loom/scripts/tests/test-docs-only-fast-path.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
JUDGE_MD="$PROMPT_DIR/judge.md"
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"
GUIDE_MD="$PROMPT_DIR/guide.md"

# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# Pin a literal snippet as present verbatim in a doc file — catches drift
# between this test's mirrored function and the shipped markdown.
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

# Pin a literal snippet's ABSENCE from a doc file — catches a regression back
# to a truncating/trust-the-marker shortcut.
assert_doc_lacks() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (found stale/unsafe literal in $file: $needle)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

# =====================================================================
# The docs-only fast-path eligibility check, mirrored verbatim from BOTH
# judge.md's "Docs-Only Fast Path" section and champion-pr-merge.md's
# "Docs-only fast path" subsection of criterion #2 — the two shipped copies
# are intentionally identical, and the doc pins below confirm both actually
# match this mirror.
# =====================================================================
DOCS_FAST_PATH_ALLOWLIST=("WORK_LOG.md" "WORK_PLAN.md" "README.md")

docs_only_fast_path_check() {
    # Reads a newline-separated file list on stdin. Echoes "ELIGIBLE" if
    # every file is an exact match against DOCS_FAST_PATH_ALLOWLIST, or
    # "NOT ELIGIBLE: <file>" for the first file that is not.
    local file matched
    local saw_any=0
    while IFS= read -r file; do
        [ -z "$file" ] && continue
        saw_any=1
        matched=0
        for allowed in "${DOCS_FAST_PATH_ALLOWLIST[@]}"; do
            if [ "$file" = "$allowed" ]; then
                matched=1
                break
            fi
        done
        if [ "$matched" -ne 1 ]; then
            echo "NOT ELIGIBLE: $file"
            return 0
        fi
    done
    if [ "$saw_any" -eq 0 ]; then
        echo "NOT ELIGIBLE: empty file list"
        return 0
    fi
    echo "ELIGIBLE"
}

echo "--- docs_only_fast_path_check: eligible diffs ---"

out="$(printf '%s\n' "WORK_LOG.md" | docs_only_fast_path_check)"
assert_eq "ELIGIBLE" "$out" "a single-file WORK_LOG.md-only diff is eligible"

out="$(printf 'WORK_LOG.md\nWORK_PLAN.md\n' | docs_only_fast_path_check)"
assert_eq "ELIGIBLE" "$out" "WORK_LOG.md + WORK_PLAN.md is eligible"

out="$(printf 'WORK_LOG.md\nWORK_PLAN.md\nREADME.md\n' | docs_only_fast_path_check)"
assert_eq "ELIGIBLE" "$out" "all three allowlisted files together are eligible"

out="$(printf 'README.md\n' | docs_only_fast_path_check)"
assert_eq "ELIGIBLE" "$out" "a single-file README.md-only diff is eligible"

echo
echo "--- docs_only_fast_path_check: rejects any file outside the exact allowlist ---"

out="$(printf 'WORK_LOG.md\nsrc/lib.rs\n' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: src/lib.rs" "$out" \
    "a docs file plus a source file is rejected (mixed diff)"

out="$(printf 'WORK_LOG.md\ndocs/README.md\n' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: docs/README.md" "$out" \
    "a nested docs/README.md is rejected (path match, not substring/basename match)"

out="$(printf 'WORK_LOG.md\nmcp-loom/README.md\n' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: mcp-loom/README.md" "$out" \
    "a nested mcp-loom/README.md is rejected"

out="$(printf 'notes/WORK_LOG.md\n' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: notes/WORK_LOG.md" "$out" \
    "a non-root-level WORK_LOG.md is rejected"

out="$(printf '' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: empty file list" "$out" \
    "an empty diff (no files at all) is rejected, not silently treated as eligible"

echo
echo "--- docs_only_fast_path_check: issue #6134 Test Plan edge cases ---"

# "a docs-only PR that also modifies .github/workflows/*.yml ... must NOT
# qualify for the fast path even though those aren't 'code' in the
# traditional sense".
out="$(printf 'WORK_LOG.md\nWORK_PLAN.md\nREADME.md\n.github/workflows/ci.yml\n' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: .github/workflows/ci.yml" "$out" \
    "all three docs files plus .github/workflows/ci.yml is rejected"

# "... or .loom/config.json must NOT qualify for the fast path".
out="$(printf 'WORK_LOG.md\n.loom/config.json\n' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: .loom/config.json" "$out" \
    "WORK_LOG.md plus .loom/config.json is rejected"

# The order in which the disqualifying file appears must not matter — the
# loop must not stop at the first ALLOWED file and declare victory early.
out="$(printf '.loom/config.json\nWORK_LOG.md\nWORK_PLAN.md\nREADME.md\n' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: .loom/config.json" "$out" \
    ".loom/config.json first in the list is still caught (not masked by later allowed files)"

echo
echo "--- docs_only_fast_path_check: smuggling can't be won by a mislabeled/marked PR ---"

# A file list carrying something that merely *looks* docs-adjacent by name
# (e.g. a file whose name contains WORK_LOG as a substring but isn't the
# exact root file) must still be rejected — the check is exact-match, not
# substring/glob.
out="$(printf 'WORK_LOG.md.bak\n' | docs_only_fast_path_check)"
assert_eq "NOT ELIGIBLE: WORK_LOG.md.bak" "$out" \
    "a file merely containing the allowlisted name as a substring is rejected"

echo
echo "--- Doc pins: judge.md ships the mechanical, marker-independent eligibility check ---"

assert_doc_contains "$JUDGE_MD" \
    'DOCS_FAST_PATH_ALLOWLIST=("WORK_LOG.md" "WORK_PLAN.md" "README.md")' \
    "judge.md defines the exact 3-file allowlist"

assert_doc_contains "$JUDGE_MD" \
    'FILES=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/files" --paginate --jq '"'"'.[].filename'"'"')' \
    "judge.md fetches the changed-file list via the paginated REST endpoint (not the truncating gh pr view --json files)"

assert_doc_contains "$JUDGE_MD" \
    'docs_only_fast_path_check() {' \
    "judge.md defines docs_only_fast_path_check()"

assert_doc_contains "$JUDGE_MD" \
    "loom:docs-fast-path-evaluation" \
    "judge.md's fast-path approval carries the loom:docs-fast-path-evaluation marker"

assert_doc_contains "$JUDGE_MD" \
    "#6134" \
    "judge.md documents the #6134 fast path"

echo
echo "--- Doc pins: champion-pr-merge.md independently re-derives eligibility, never trusts Judge's marker ---"

assert_doc_contains "$CHAMPION_MD" \
    'DOCS_FAST_PATH_ALLOWLIST=("WORK_LOG.md" "WORK_PLAN.md" "README.md")' \
    "champion-pr-merge.md defines the same exact 3-file allowlist"

assert_doc_contains "$CHAMPION_MD" \
    'docs_only_fast_path_check() {' \
    "champion-pr-merge.md defines its own docs_only_fast_path_check()"

assert_doc_contains "$CHAMPION_MD" \
    'FAST_PATH_FILES=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/files" --paginate --jq '"'"'.[].filename'"'"')' \
    "champion-pr-merge.md fetches the changed-file list via the paginated REST endpoint for its own fast-path check"

assert_doc_contains "$CHAMPION_MD" \
    "Never trust a marker, label, or the PR body's stated intent" \
    "champion-pr-merge.md explicitly states it never trusts Judge's marker/label for the fast path"

assert_doc_contains "$CHAMPION_MD" \
    "#6134" \
    "champion-pr-merge.md documents the #6134 fast path"

echo
echo "--- Doc pins: neither Judge nor Champion gates eligibility on a label/marker alone ---"

# Regression guard for AC2 ("verify the diff's file list server-side... not
# just trust the PR's stated intent"): the fast path must never be phrased
# as "if the marker/label is present, skip verification".
assert_doc_lacks "$JUDGE_MD" \
    'if grep -q "loom:docs-only-fast-path"' \
    "judge.md never short-circuits eligibility on a grep for the marker alone"

assert_doc_lacks "$CHAMPION_MD" \
    'if grep -q "loom:docs-only-fast-path"' \
    "champion-pr-merge.md never short-circuits eligibility on a grep for the marker alone"

echo
echo "--- Doc pins: guide.md's create_docs_pr() marker is informational only ---"

assert_doc_contains "$GUIDE_MD" \
    "<!-- loom:docs-only-fast-path -->" \
    "guide.md's create_docs_pr() emits the informational fast-path marker"

assert_doc_contains "$GUIDE_MD" \
    "Judge and Champion never trust it" \
    "guide.md documents that the marker is informational, not authoritative"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
