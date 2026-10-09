#!/usr/bin/env bash
# test-worktree-stale-closed-branch.sh — Tests for #9083.
#
# The sibling suite test-worktree-stale-merged-branch.sh (#5657/#8280) covers
# the case where `origin/feature/issue-N`'s tip is the head of an already
# MERGED pull request. Nothing covered the third state that ref can be in: the
# head of a PR that was CLOSED WITHOUT MERGING.
#
# That is the same hazard with a strictly worse payload. A merged branch at
# least contains work that is on the default branch; a closed-unmerged one
# contains work somebody decided NOT to take. On #8195 `worktree.sh 8195`
# reused `origin/feature/issue-8195` at closed PR #8275's head — a reverted
# delegation attempt plus a rejected follow-up, tens of commits behind main —
# and the only signal was the generic "has diverged from main" warning, which
# fires for every legitimate in-flight PR branch too and therefore says nothing
# about whether continuing is right.
#
# The chosen shape is REFUSE BY DEFAULT with an explicit resume path, matching
# the precedent #8280 set for the sibling LOCAL-branch arm:
#   1. origin/feature/issue-N's tip IS the head of a closed-unmerged PR ->
#      worktree.sh exits non-zero, creates no worktree, and says so in wording
#      distinguishable from BOTH the merged case and the divergence warning,
#      naming the PR number.
#   2. The legitimate resume path stays reachable: create the local branch from
#      origin first, and worktree.sh's local-ref arm reuses it (the escape
#      hatch the refusal message prints, and the one this family already
#      prescribes for the forge-unavailable case).
#   3. An OPEN PR on the same branch is untouched (#4823) — including the
#      reopened-as-a-new-PR shape, where a closed PR and an open PR share the
#      branch and the tip still equals the closed one's head.
#   4. The tip has moved PAST the closed PR's head -> reuse, per the #7872
#      exact-tip-match discipline the merged arm already follows.
#   5. Forge lookup unavailable -> fails open to reuse, never blocking worktree
#      creation (the same contract as the merged suite's Test 3).
#   6. --json mode reports the refusal as one JSON document on stdout, with no
#      human text mixed in.
#
# Follows the merged suite's throwaway-bare-origin harness and its fake-forge
# binary pattern; that suite must keep passing unmodified (verified separately).
#
# Needs a BUILT loom-daemon: the decision is `loom-daemon
# worktree-closed-pr-branch`, reached through the single delegating line in
# lib/worktree-forge-pr-check.sh (that file is `contract`-category and the
# portable-shell ratchet gives its growth no override, so the logic lives in
# Rust). FAILS rather than skips without one — a suite that skipped would
# report green while testing nothing.
#
# Usage:
#   cargo build --package loom-daemon
#   bash defaults/scripts/tests/test-worktree-stale-closed-branch.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-closed-pr-branch"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

ISSUE=91
BRANCH="feature/issue-$ISSUE"

# Build a throwaway repo with origin/main plus a pushed feature/issue-91
# carrying one unique commit, and NO local copy of that branch — a fresh clone
# or a cleaned-up worktree host, which is the arm this issue targets.
#
# A second remote carrying a real GitHub URL is added but never fetched, so the
# repo LOOKS like it has a forge relationship: without it, `origin` being a
# local filesystem path makes the sibling #7765 helper classify the repo as
# having no forge at all. This suite's guard asks its own question through
# `loom-daemon worktree-closed-pr-branch`, which fails open on any unanswerable
# probe, so the fake forge below has to be genuinely reachable for the refusal
# to be under test at all.
setup_repo() {
    local name="$1"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtclosed.XXXXXX)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/$name"
    (
        cd "$tmp/$name"
        git config user.email t@t
        git config user.name t
        git commit --allow-empty -q -m init
        git remote add origin "$tmp/origin.git"
        git remote add github "https://github.com/example/loom.git"
        git push -q origin main
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh

        # The "abandoned PR" branch: push it, then drop the LOCAL copy (origin
        # keeps it — auto-delete-head-branches is off, and a closed PR's head
        # branch is never auto-deleted at all).
        git checkout -q -b "$BRANCH"
        echo "rejected-work" > rejected.txt
        git add rejected.txt
        git commit -q -m "wip: work that was later rejected"
        git push -q origin "$BRANCH"
        git checkout -q main
        git branch -q -D "$BRANCH"

        # Move main forward so the reused branch is genuinely behind it — the
        # incident's shape, and what makes the generic divergence warning fire.
        echo "later" > later.txt
        git add later.txt
        git commit -q -m "main: a commit the abandoned branch never saw"
        git push -q origin main
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# A fake forge on PATH covering BOTH queries this path makes:
#
#   pr list --head <b> --state merged --json headRefOid,number   (branch_landed)
#   pr list --head <b> --state all    --json number,state,...    (this guard)
#
# Installed under BOTH names the two probes select between — `gh` and
# `loom-daemon` (whose `forge` verb the fake shifts off) — because both probes
# prefer `loom-daemon forge` when one is on PATH, and a suite that faked only
# `gh` would be at the mercy of whether the installed daemon's passthrough
# happens to reach it. Neither name affects which binary the worktree.sh
# dispatch itself runs: that is $LOOM_DAEMON_SELF_BIN, pinned above.
#
# FAKE_GH_MODE selects what the `--state all` query reports:
#   closed        one CLOSED, unmerged PR whose headRefOid IS origin/<b>'s tip
#   closed-stale  one CLOSED, unmerged PR whose headRefOid is main's tip
#                 instead, i.e. the branch has moved past what was closed
#   reopened      that same CLOSED PR plus an OPEN one on the same branch
#   open          one OPEN PR at the tip (the #4823 in-flight case)
#   none          no PR at all
#   off           every call fails (forge unavailable)
# The merged query answers `[]` in every mode except `off`, so `branch_landed`
# reaches `not-landed` and this guard is the thing under test.
install_fake_gh() {
    local repo="$1"
    local fake_bin
    fake_bin="$(dirname "$repo")/fakebin"
    mkdir -p "$fake_bin"
    cat > "$fake_bin/gh" << EOF
#!/bin/bash
# A fake \`loom-daemon\` is this same script: drop the \`forge\` verb and the
# remaining argv is a \`gh\` command line.
[[ "\$1" == "forge" ]] && shift
if [[ "\${FAKE_GH_MODE:-none}" == "off" ]]; then
    echo "gh: fake forge unavailable in this test" >&2
    exit 1
fi
if [[ "\$1" == "pr" && "\$2" == "list" ]]; then
    shift 2
    branch=""; state=""
    while [[ \$# -gt 0 ]]; do
        case "\$1" in
            --head) branch="\$2"; shift 2 ;;
            --state) state="\$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    if [[ "\$state" != "all" ]]; then
        echo "[]"
        exit 0
    fi
    tip="\$(git -C "$repo" rev-parse --verify -q "refs/remotes/origin/\$branch" 2>/dev/null || echo "")"
    main_tip="\$(git -C "$repo" rev-parse --verify -q refs/remotes/origin/main 2>/dev/null || echo "")"
    case "\${FAKE_GH_MODE:-none}" in
        closed)
            echo "[{\"number\": 8275, \"state\": \"CLOSED\", \"mergedAt\": null, \"headRefOid\": \"\$tip\", \"url\": \"https://example.invalid/pull/8275\"}]" ;;
        closed-stale)
            echo "[{\"number\": 8275, \"state\": \"CLOSED\", \"mergedAt\": null, \"headRefOid\": \"\$main_tip\", \"url\": \"https://example.invalid/pull/8275\"}]" ;;
        reopened)
            echo "[{\"number\": 8275, \"state\": \"CLOSED\", \"mergedAt\": null, \"headRefOid\": \"\$tip\", \"url\": \"https://example.invalid/pull/8275\"}, {\"number\": 8400, \"state\": \"OPEN\", \"mergedAt\": null, \"headRefOid\": \"\$tip\", \"url\": \"https://example.invalid/pull/8400\"}]" ;;
        open)
            echo "[{\"number\": 8400, \"state\": \"OPEN\", \"mergedAt\": null, \"headRefOid\": \"\$tip\", \"url\": \"https://example.invalid/pull/8400\"}]" ;;
        *)
            echo "[]" ;;
    esac
    exit 0
fi
echo "fake gh: unsupported invocation: \$*" >&2
exit 1
EOF
    chmod +x "$fake_bin/gh"
    cp "$fake_bin/gh" "$fake_bin/loom-daemon"
    echo "$fake_bin"
}

# --- Test 1: the incident — origin/<branch>'s tip IS a closed-unmerged PR's head ---
echo "Test 1: origin/$BRANCH is the head of a CLOSED-unmerged PR -> worktree.sh refuses"
REPO=$(setup_repo closedrepo1)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtclosed-refuse.$$"
RC=0
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=closed ./.loom/scripts/worktree.sh "$ISSUE" >"$OUT_LOG" 2>&1
) || RC=$?
if [[ "$RC" -ne 0 ]]; then
    pass "worktree.sh exits non-zero rather than seed a worktree from a closed PR's head"
else
    fail "worktree.sh exited 0 — it must refuse, not silently reuse the abandoned branch"
    cat "$OUT_LOG"
fi
if [[ ! -d "$REPO/.loom/worktrees/issue-$ISSUE" ]]; then
    pass "no worktree was created"
else
    fail "a worktree was created despite the refusal"
    ls -la "$REPO/.loom/worktrees/issue-$ISSUE"
fi
if grep -q "PR #8275" "$OUT_LOG"; then
    pass "the message names the closed PR's number"
else
    fail "the message does not name the PR number (see $OUT_LOG)"
    cat "$OUT_LOG"
fi
if grep -qi "CLOSED WITHOUT MERGING" "$OUT_LOG"; then
    pass "wording is distinguishable from the merged case and the divergence warning"
else
    fail "wording is not distinguishable (see $OUT_LOG)"
    cat "$OUT_LOG"
fi
if ! grep -qi "already-merged PR" "$OUT_LOG"; then
    pass "the merged-case message is NOT emitted (the two arms stay distinct)"
else
    fail "the merged-case message leaked into the closed-unmerged arm"
    cat "$OUT_LOG"
fi
if grep -q "git branch $BRANCH origin/$BRANCH" "$OUT_LOG"; then
    pass "the refusal prints the explicit resume recipe"
else
    fail "the refusal does not name the resume remedy (see $OUT_LOG)"
    cat "$OUT_LOG"
fi
if git -C "$REPO" show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
    pass "origin's branch is left alone (refusal, not deletion)"
else
    fail "origin/$BRANCH was unexpectedly removed — refusal must not be destructive"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 2: the resume path the refusal prints actually works ---
echo ""
echo "Test 2: the printed resume recipe (create the local branch first) still reuses the closed PR's branch"
REPO=$(setup_repo closedrepo2)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtclosed-resume.$$"
RC=0
(
    cd "$REPO"
    git fetch -q origin "$BRANCH"
    git branch "$BRANCH" "origin/$BRANCH"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=closed ./.loom/scripts/worktree.sh "$ISSUE" >"$OUT_LOG" 2>&1
) || RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "worktree.sh succeeds once the local branch exists (the resume path is reachable)"
else
    fail "the resume path was refused too — a deliberately resumed closed PR must still work"
    cat "$OUT_LOG"
fi
if [[ -f "$REPO/.loom/worktrees/issue-$ISSUE/rejected.txt" ]]; then
    pass "the worktree carries the closed PR's work (resumed, not branched fresh)"
else
    fail "the resumed worktree is missing the closed PR's content"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 3: an OPEN PR is untouched (#4823), including the reopened shape ---
echo ""
echo "Test 3: an OPEN PR on the same branch -> still reused (#4823 unaffected)"
for MODE in open reopened; do
    REPO=$(setup_repo "closedrepo3-$MODE")
    FAKE_BIN=$(install_fake_gh "$REPO")
    OUT_LOG="/tmp/wtclosed-open-$MODE.$$"
    RC=0
    (
        cd "$REPO"
        PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE="$MODE" ./.loom/scripts/worktree.sh "$ISSUE" >"$OUT_LOG" 2>&1
    ) || RC=$?
    if [[ "$RC" -eq 0 && -f "$REPO/.loom/worktrees/issue-$ISSUE/rejected.txt" ]]; then
        pass "FAKE_GH_MODE=$MODE: branch reused, the open PR's history continued"
    else
        fail "FAKE_GH_MODE=$MODE: refused or branched fresh — regressed #4823"
        cat "$OUT_LOG"
    fi
    cleanup_repo "$REPO"
    rm -f "$OUT_LOG"
done

# --- Test 4: tip moved past the closed head -> reuse (exact-tip-match, #7872) ---
echo ""
echo "Test 4: the branch has moved PAST the closed PR's head -> still reused"
REPO=$(setup_repo closedrepo4)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtclosed-stale.$$"
RC=0
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=closed-stale ./.loom/scripts/worktree.sh "$ISSUE" >"$OUT_LOG" 2>&1
) || RC=$?
if [[ "$RC" -eq 0 && -f "$REPO/.loom/worktrees/issue-$ISSUE/rejected.txt" ]]; then
    pass "a closed PR whose head is no longer the tip does not refuse (exact-tip-match only)"
else
    fail "refused on a tip that is not the closed PR's head"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 5: forge unavailable -> fail open to reuse ---
echo ""
echo "Test 5: forge lookup unavailable -> worktree.sh falls open and still reuses the branch"
REPO=$(setup_repo closedrepo5)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtclosed-unavailable.$$"
RC=0
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=off ./.loom/scripts/worktree.sh "$ISSUE" >"$OUT_LOG" 2>&1
) || RC=$?
if [[ "$RC" -eq 0 && -f "$REPO/.loom/worktrees/issue-$ISSUE/rejected.txt" ]]; then
    pass "worktree created and branch reused (forge unavailable -> fail open, never block)"
else
    fail "a forge outage blocked worktree creation"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 6: --json purity on the refusal ---
echo ""
echo "Test 6: --json refusal is exactly one JSON document on stdout"
REPO=$(setup_repo closedrepo6)
FAKE_BIN=$(install_fake_gh "$REPO")
JSON_OUT="/tmp/wtclosed-json-out.$$"
JSON_ERR="/tmp/wtclosed-json-err.$$"
RC=0
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=closed ./.loom/scripts/worktree.sh --json "$ISSUE" >"$JSON_OUT" 2>"$JSON_ERR"
) || RC=$?
if [[ "$RC" -ne 0 ]]; then
    pass "--json mode refuses too"
else
    fail "--json mode exited 0"
    cat "$JSON_OUT" "$JSON_ERR"
fi
if [[ "$(wc -l < "$JSON_OUT")" -eq 1 ]]; then
    pass "stdout carries exactly one line"
else
    fail "stdout is not a single line: $(wc -l < "$JSON_OUT") lines"
    cat "$JSON_OUT"
fi
if command -v jq >/dev/null 2>&1; then
    if [[ "$(jq -r '.error' "$JSON_OUT" 2>/dev/null)" == "closed-unmerged-pr-branch" ]] \
       && [[ "$(jq -r '.prNumber' "$JSON_OUT" 2>/dev/null)" == "8275" ]] \
       && [[ "$(jq -r '.issueNumber' "$JSON_OUT" 2>/dev/null)" == "$ISSUE" ]]; then
        pass "the JSON document names the error, the PR number and the issue"
    else
        fail "the JSON document is missing or wrong"
        cat "$JSON_OUT"
    fi
else
    pass "jq unavailable — JSON field assertions skipped"
fi
cleanup_repo "$REPO"
rm -f "$JSON_OUT" "$JSON_ERR"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
