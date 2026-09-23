#!/usr/bin/env bash
# test-judge-payload-drop-guard.sh - Behavioral coverage for issue #8298.
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# judge.md's "If DIRTY: Attempt Automated Rebase" workflow runs
# `git rebase origin/main` and, on exit 0, proceeds straight to
# `git push --force-with-lease`. The incident: two independent single-line
# edits on adjacent lines with no separating context in the same file
# (`.loom/install-metadata.json`'s `loom_version` line and `loom_commit`
# line) let git's own cherry-equivalence detection ("skipped previously
# applied commit") silently drop the PR branch's commit during rebase --
# `git rebase` reports success (exit 0, no conflict) but the resulting tree
# is byte-identical to `origin/main`, discarding the PR's actual payload.
#
# WHAT THIS SUITE DOES
#
# Deliberately NOT a prose-existence assertion (see
# test-judge-tdd-merge-base.sh's header for the citation). EXTRACTS the real
# fenced ```bash``` block shipped under "If DIRTY: Attempt Automated Rebase"
# in judge.md and RUNS it (real `git`, stubbed `gh`/`post-verdict.sh`)
# against purpose-built git fixtures:
#
#   1. The exact repro shape (#8298): a rebase that git silently drops via
#      cherry-equivalence must be caught by the new payload-drop guard --
#      the push must never happen, and the SAME merge-conflict fallback
#      (`loom:merge-conflict` + `loom:changes-requested`) already used for
#      "rebase failed (complex conflicts)" must fire.
#   2. The common case: a genuinely successful rebase (real content
#      difference vs origin/main afterward) must be UNAFFECTED -- push still
#      proceeds, no false positive.
#
# Hermetic: local git fixtures (a local bare "origin") under a private
# TMPDIR. No forge, no network.
#
# Usage:
#   ./.loom/scripts/tests/test-judge-payload-drop-guard.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Installed layout (.loom/scripts/tests -> two `..` to repo root) vs. this
# source repo (defaults/scripts/tests -> one `..`). Probe both (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
JUDGE_MD="$PROMPT_DIR/judge.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (missing '$needle' in: ${haystack//$'\n'/ | })"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (unexpectedly found '$needle')"
    fi
}

[[ -f "$JUDGE_MD" ]] || { echo "FATAL: subject not found: $JUDGE_MD" >&2; exit 1; }

# =====================================================================
# Extract the shipped ```bash``` fence under "If DIRTY: Attempt Automated
# Rebase" -- the real code a Judge session runs, not a local mirror.
# =====================================================================
FENCE_SRC="$(awk '
    /^### If DIRTY: Attempt Automated Rebase$/ { insec = 1 }
    insec && /^```bash$/ && !infence { infence = 1; depth = 1; next }
    infence && /^```bash$/ { depth++; print; next }
    infence && /^```$/ {
        depth--
        if (depth == 0) exit
        print
        next
    }
    infence { print }
' "$JUDGE_MD")"

if [[ -z "$FENCE_SRC" || "$FENCE_SRC" != *"PRE_REBASE_AHEAD"* ]]; then
    echo "FATAL: could not extract the DIRTY-rebase fence (with the #8298 guard) from $JUDGE_MD" >&2
    exit 1
fi
pass "extracted the shipped DIRTY-rebase block (with PRE_REBASE_AHEAD) from judge.md"

# The first line is a literal `PR_NUMBER=<number>` placeholder for a human/
# agent to fill in -- replace it with a real value so the block is runnable,
# leaving everything else byte-for-byte as shipped.
FENCE_SRC="$(printf '%s\n' "$FENCE_SRC" | sed '1s/^PR_NUMBER=<number>$/PR_NUMBER=999/')"
if [[ "${FENCE_SRC%%$'\n'*}" != "PR_NUMBER=999" ]]; then
    echo "FATAL: judge.md's DIRTY-rebase fence no longer starts with 'PR_NUMBER=<number>' -- update this test's substitution" >&2
    exit 1
fi

if ! bash -n <(printf '%s\n' "$FENCE_SRC"); then
    echo "FATAL: the shipped DIRTY-rebase block is not valid bash" >&2
    exit 1
fi
pass "the shipped DIRTY-rebase block parses as bash"

# =====================================================================
# Shared fixture helpers
# =====================================================================
run_dirty_block() {
    # Runs the extracted block in $1 (a repo dir already checked out on the
    # PR branch, with origin fetched). Stubs `gh` and `post-verdict.sh`;
    # everything else (fetch/rebase/diff/push) is real git.
    local repo="$1"
    (
        cd "$repo" || exit 1
        export PATH="$repo/.loom/stub-bin:$PATH"
        bash -c "$FENCE_SRC"
    )
}

make_fixture() {
    # make_fixture <workdir> -- sets up a bare "origin" plus a working repo
    # on branch feature/issue-999, with .loom/worktrees/issue-999 symlinked
    # back to the repo root (so the block's own worktree-cd is a no-op) and
    # stubs for `gh` and `./.loom/scripts/post-verdict.sh`.
    local base="$1"
    local repo="$base/repo"
    mkdir -p "$base/origin.git"
    git init -q --bare "$base/origin.git"
    git init -q "$repo"
    (
        cd "$repo" || exit 1
        git config user.email "test@loom.test"
        git config user.name "Loom Test"
        git config commit.gpgsign false
        git remote add origin "$base/origin.git"
    )

    mkdir -p "$repo/.loom/worktrees" "$repo/.loom/scripts" "$repo/.loom/stub-bin"
    ln -s "../.." "$repo/.loom/worktrees/issue-999"

    cat > "$repo/.loom/scripts/post-verdict.sh" <<'STUB'
#!/usr/bin/env bash
echo "post-verdict.sh called: $*" >> "$(dirname "$0")/../../post-verdict.calls"
exit 0
STUB
    chmod +x "$repo/.loom/scripts/post-verdict.sh"

    cat > "$repo/.loom/stub-bin/gh" <<'STUB'
#!/usr/bin/env bash
LOG="$(git rev-parse --show-toplevel 2>/dev/null)/gh.calls"
echo "$*" >> "$LOG"
case "$*" in
    *"--json mergeStateStatus"*) echo "DIRTY" ;;
    *"--json headRefName"*) echo "feature/issue-999" ;;
    *) exit 0 ;;
esac
STUB
    chmod +x "$repo/.loom/stub-bin/gh"

    printf '%s' "$repo"
}

WORKROOT="$(mktemp -d)"
trap 'rm -rf "$WORKROOT"' EXIT

# =====================================================================
# Case 1: the #8298 repro shape -- git's own cherry-equivalence detection
# silently drops the PR commit during rebase (adjacent single-line JSON
# edits, no separating context, resolving to a content-identical patch).
# =====================================================================
echo
echo "--- #8298 repro: rebase silently drops the PR's payload ---"

BASE1="$WORKROOT/case1"
REPO1="$(make_fixture "$BASE1")"

cat > "$REPO1/install-metadata.json" <<'EOF'
{
  "loom_version": "1.1",
  "loom_commit": "aaaa"
}
EOF
(cd "$REPO1" && git add -A && git commit -qm base && git push -q origin HEAD:main)

(cd "$REPO1" && git checkout -qb feature/issue-999)
cat > "$REPO1/install-metadata.json" <<'EOF'
{
  "loom_version": "1.1",
  "loom_commit": "bbbb"
}
EOF
(cd "$REPO1" && GIT_AUTHOR_DATE="2026-01-01T00:00:00" GIT_COMMITTER_DATE="2026-01-01T00:00:00" \
    git commit -qam "chore: resync installed Loom surfaces (PR)" && \
    git push -qu origin feature/issue-999)

(cd "$REPO1" && git checkout -q main)
cat > "$REPO1/install-metadata.json" <<'EOF'
{
  "loom_version": "1.1",
  "loom_commit": "bbbb"
}
EOF
(cd "$REPO1" && GIT_AUTHOR_DATE="2026-01-02T00:00:00" GIT_COMMITTER_DATE="2026-01-02T00:00:00" \
    git commit -qam "chore: resync installed Loom surfaces (main)" && git push -q origin HEAD:main)

(cd "$REPO1" && git checkout -q feature/issue-999)
PRE_AHEAD="$(cd "$REPO1" && git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")"
assert_eq "1" "$PRE_AHEAD" "sanity: the PR branch carries one real commit ahead of origin/main before the block runs"

OUT1="$(run_dirty_block "$REPO1" 2>&1)"

assert_contains "$OUT1" "Rebase silently dropped PR payload" \
    "the payload-drop guard fires and logs why"
assert_not_contains "$OUT1" "Rebase successful - proceeding with evaluation" \
    "the success path never runs"
assert_eq "0" "$(cd "$REPO1" && git rev-list --count origin/main..HEAD)" \
    "sanity: git really did collapse the branch to origin/main's content (the underlying hazard)"

CALLS1="$(cat "$REPO1/post-verdict.calls" 2>/dev/null || echo "")"
assert_contains "$CALLS1" "changes-requested" \
    "falls back through the EXISTING post-verdict.sh changes-requested path (not a new one)"

GH_CALLS1="$(cat "$REPO1/gh.calls" 2>/dev/null || echo "")"
assert_contains "$GH_CALLS1" "loom:merge-conflict" \
    "reuses the existing loom:merge-conflict label"
assert_contains "$GH_CALLS1" "loom:changes-requested" \
    "reuses the existing loom:changes-requested label"

# =====================================================================
# Case 2: no regression -- a genuine successful rebase (real content
# difference vs origin/main afterward) must be unaffected.
# =====================================================================
echo
echo "--- no regression: a normal DIRTY rebase with real post-rebase content still pushes ---"

BASE2="$WORKROOT/case2"
REPO2="$(make_fixture "$BASE2")"

echo "unrelated base content" > "$REPO2/other.txt"
(cd "$REPO2" && git add -A && git commit -qm base && git push -q origin HEAD:main)

(cd "$REPO2" && git checkout -qb feature/issue-999)
echo "PR-only change" >> "$REPO2/other.txt"
(cd "$REPO2" && git commit -qam "pr: add a line" && git push -qu origin feature/issue-999)

(cd "$REPO2" && git checkout -q main)
echo "main-only change" > "$REPO2/main-only.txt"
(cd "$REPO2" && git add -A && git commit -qam "main: unrelated file" && git push -q origin HEAD:main)

(cd "$REPO2" && git checkout -q feature/issue-999)
PRE_AHEAD2="$(cd "$REPO2" && git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")"
assert_eq "1" "$PRE_AHEAD2" "sanity: the PR branch carries one real commit ahead of origin/main before the block runs"

# `git push --force-with-lease` in the extracted block would try to reach
# the real "origin" -- that IS our local bare repo here, so let it push for
# real and assert on the result instead of stubbing it away.
OUT2="$(run_dirty_block "$REPO2" 2>&1)"

assert_contains "$OUT2" "Rebase successful - proceeding with evaluation" \
    "the normal success path runs when the rebase genuinely changes content"
assert_not_contains "$OUT2" "Rebase silently dropped PR payload" \
    "the payload-drop guard does NOT fire on a real, non-empty rebase result (no false positive)"

POST_REBASE_DIFF_EMPTY2=0
(cd "$REPO2" && git diff --quiet origin/main HEAD) && POST_REBASE_DIFF_EMPTY2=1
assert_eq "0" "$POST_REBASE_DIFF_EMPTY2" \
    "sanity: the rebased branch genuinely still differs from origin/main"

CALLS2="$(cat "$REPO2/post-verdict.calls" 2>/dev/null || echo "")"
assert_eq "" "$CALLS2" "the merge-conflict fallback is never invoked on the common-case path"

REMOTE_PR_HEAD2="$(git --git-dir="$BASE2/origin.git" rev-parse feature/issue-999)"
LOCAL_HEAD2="$(cd "$REPO2" && git rev-parse HEAD)"
assert_eq "$REMOTE_PR_HEAD2" "$LOCAL_HEAD2" \
    "the push actually landed on the (local, hermetic) origin -- the common case still pushes"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
