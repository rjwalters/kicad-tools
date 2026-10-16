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
#   3. RESOLUTION_DISCARDED (#9282) blocks the push the same way.
#   4. An unanswered guard (a daemon predating the verb) warns loudly and
#      does NOT block the push.
#
# Since #10771 the block updates by MERGE unless `loom-daemon branch-update
# strategy` prints `rebase`. Both cases therefore run three ways, with a
# `loom-daemon` stub on PATH: pinned `rebase` (the original #8298 shape),
# `merge`, and a daemon that predates the subcommand (clap's "unrecognized
# subcommand", exit 2), which must degrade to merge. The guard checks for an
# empty diff after ANY update, so it must fire in merge mode too: a merge whose
# result equals origin/main has dropped the payload just as surely.
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

if [[ -z "$FENCE_SRC" || "$FENCE_SRC" != *"forge rebase-guard --base origin/main"* ]]; then
    echo "FATAL: could not extract the DIRTY-rebase fence (with the #9282 rebase-guard call) from $JUDGE_MD" >&2
    exit 1
fi
pass "extracted the shipped DIRTY-rebase block (with the rebase-guard call) from judge.md"
assert_not_contains "$FENCE_SRC" "PRE_REBASE_AHEAD" \
    "the block no longer carries its own inline empty-diff predicate (#9282)"

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
    # run_dirty_block <repo> <strategy> -- runs the extracted block in <repo>
    # (already checked out on the PR branch, with origin fetched). Stubs `gh`,
    # `post-verdict.sh` and `loom-daemon`; everything else (fetch/rebase/merge/
    # diff/push) is real git. <strategy> is what the `loom-daemon` stub's
    # `branch-update strategy` prints (rebase|merge), or `missing` for a daemon
    # that predates the subcommand.
    local repo="$1" strategy="$2"
    (
        cd "$repo" || exit 1
        export PATH="$repo/.loom/stub-bin:$PATH"
        export STUB_BRANCH_UPDATE="$strategy"
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

    # `loom-daemon` stand-in: `branch-update strategy` prints $STUB_BRANCH_UPDATE
    # (rebase|merge; `missing` = a daemon predating the subcommand), and
    # `forge rebase-guard` (#9282) answers per GUARD_MODE:
    #   emptied (default) -- the verb's EMPTIED predicate in miniature: blocked
    #                        (rc 0) when HEAD's tree equals the base's, else
    #                        safe (rc 1);
    #   discarded         -- RESOLUTION_DISCARDED, rc 0;
    #   predates          -- clap's "unrecognized subcommand", rc 2.
    cat > "$repo/.loom/stub-bin/loom-daemon" <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2" == "branch-update strategy" && "${STUB_BRANCH_UPDATE:-}" != missing ]]; then
    echo "${STUB_BRANCH_UPDATE:?}"; exit 0
fi
if [[ "$1 $2" == "forge rebase-guard" ]]; then
    echo "$*" >> "$(git rev-parse --show-toplevel 2>/dev/null)/daemon.calls"
    [[ "$3" == "--base" ]] || exit 64
    case "${GUARD_MODE:-emptied}" in
        discarded) echo "RESOLUTION_DISCARDED 0123abc"; echo "BLOCKER: stub" >&2; exit 0 ;;
        predates) echo "error: unrecognized subcommand 'rebase-guard'" >&2; exit 2 ;;
    esac
    if git diff --quiet "$4" HEAD; then echo "EMPTIED"; echo "BLOCKER: stub" >&2; exit 0; fi
    exit 1
fi
echo "error: unrecognized subcommand '$1'" >&2; exit 2
STUB
    chmod +x "$repo/.loom/stub-bin/loom-daemon"

    printf '%s' "$repo"
}

WORKROOT="$(mktemp -d)"
trap 'rm -rf "$WORKROOT"' EXIT

# =====================================================================
# Case 1: the #8298 repro shape -- git's own cherry-equivalence detection
# silently drops the PR commit during rebase (adjacent single-line JSON
# edits, no separating context, resolving to a content-identical patch).
# In merge mode the same shape merges cleanly to a tree equal to origin/main.
# =====================================================================
build_case1() {
    local repo
    repo="$(make_fixture "$1")"
    cat > "$repo/install-metadata.json" <<'EOF'
{
  "loom_version": "1.1",
  "loom_commit": "aaaa"
}
EOF
    (cd "$repo" && git add -A && git commit -qm base && git push -q origin HEAD:main)

    (cd "$repo" && git checkout -qb feature/issue-999)
    sed -i.bak 's/aaaa/bbbb/' "$repo/install-metadata.json" && rm -f "$repo/install-metadata.json.bak"
    (cd "$repo" && GIT_AUTHOR_DATE="2026-01-01T00:00:00" GIT_COMMITTER_DATE="2026-01-01T00:00:00" \
        git commit -qam "chore: resync installed Loom surfaces (PR)" && \
        git push -qu origin feature/issue-999)

    (cd "$repo" && git checkout -q main)
    sed -i.bak 's/aaaa/bbbb/' "$repo/install-metadata.json" && rm -f "$repo/install-metadata.json.bak"
    (cd "$repo" && GIT_AUTHOR_DATE="2026-01-02T00:00:00" GIT_COMMITTER_DATE="2026-01-02T00:00:00" \
        git commit -qam "chore: resync installed Loom surfaces (main)" && git push -q origin HEAD:main)

    (cd "$repo" && git checkout -q feature/issue-999)
    printf '%s' "$repo"
}

check_case1() {
    # check_case1 <base> <strategy> <label>
    local base="$1" strategy="$2" mode="$3" repo out calls gh_calls
    echo
    echo "--- #8298 repro ($mode): the update silently drops the PR's payload ---"
    repo="$(build_case1 "$base")"
    assert_eq "1" "$(cd "$repo" && git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")" \
        "[$mode] sanity: the PR branch carries one real commit ahead of origin/main before the block runs"
    local pre_remote
    pre_remote="$(git --git-dir="$base/origin.git" rev-parse feature/issue-999)"

    out="$(run_dirty_block "$repo" "$strategy" 2>&1)"

    assert_contains "$out" "EMPTIED" \
        "[$mode] the guard fires and its blocked answer is surfaced"
    gh_calls="$(cat "$repo/gh.calls" 2>/dev/null || echo "")"
    assert_not_contains "$gh_calls" "Automatically updated the branch" \
        "[$mode] the success path never runs"
    assert_eq "0" "$( (cd "$repo" && git diff --quiet origin/main HEAD) && echo 0 || echo 1)" \
        "[$mode] sanity: the update really did leave origin/main's content (the underlying hazard)"
    assert_eq "$pre_remote" "$(git --git-dir="$base/origin.git" rev-parse feature/issue-999)" \
        "[$mode] nothing was pushed: the remote PR head is unchanged"

    calls="$(cat "$repo/post-verdict.calls" 2>/dev/null || echo "")"
    assert_contains "$calls" "changes-requested" \
        "[$mode] falls back through the EXISTING post-verdict.sh changes-requested path (not a new one)"
    assert_contains "$gh_calls" "loom:merge-conflict" \
        "[$mode] reuses the existing loom:merge-conflict label"
    assert_contains "$gh_calls" "loom:changes-requested" \
        "[$mode] reuses the existing loom:changes-requested label"
}

# =====================================================================
# Case 2: no regression -- a genuine successful update (real content
# difference vs origin/main afterward) must be unaffected and must push.
# =====================================================================
build_case2() {
    local repo
    repo="$(make_fixture "$1")"
    echo "unrelated base content" > "$repo/other.txt"
    (cd "$repo" && git add -A && git commit -qm base && git push -q origin HEAD:main)

    (cd "$repo" && git checkout -qb feature/issue-999)
    echo "PR-only change" >> "$repo/other.txt"
    (cd "$repo" && git commit -qam "pr: add a line" && git push -qu origin feature/issue-999)

    (cd "$repo" && git checkout -q main)
    echo "main-only change" > "$repo/main-only.txt"
    (cd "$repo" && git add -A && git commit -qam "main: unrelated file" && git push -q origin HEAD:main)

    (cd "$repo" && git checkout -q feature/issue-999)
    printf '%s' "$repo"
}

check_case2() {
    # check_case2 <base> <strategy> <label> <expected-mode-in-comment>
    local base="$1" strategy="$2" mode="$3" want="$4" repo out calls gh_calls pre_remote
    echo
    echo "--- no regression ($mode): a normal DIRTY update with real content still pushes ---"
    repo="$(build_case2 "$base")"
    assert_eq "1" "$(cd "$repo" && git rev-list --count origin/main..HEAD 2>/dev/null || echo "?")" \
        "[$mode] sanity: the PR branch carries one real commit ahead of origin/main before the block runs"
    pre_remote="$(git --git-dir="$base/origin.git" rev-parse feature/issue-999)"

    # The block's push reaches the real "origin" -- our local bare repo -- so
    # let it push for real and assert on the result.
    out="$(run_dirty_block "$repo" "$strategy" 2>&1)"

    gh_calls="$(cat "$repo/gh.calls" 2>/dev/null || echo "")"
    assert_contains "$gh_calls" "Automatically updated the branch from main ($want)" \
        "[$mode] the normal success path runs, updating by $want"
    assert_not_contains "$out" "EMPTIED" \
        "[$mode] the guard does NOT fire on a real, non-empty result (no false positive)"
    assert_eq "1" "$( (cd "$repo" && git diff --quiet origin/main HEAD) && echo 0 || echo 1)" \
        "[$mode] sanity: the updated branch genuinely still differs from origin/main"

    calls="$(cat "$repo/post-verdict.calls" 2>/dev/null || echo "")"
    assert_eq "" "$calls" "[$mode] the merge-conflict fallback is never invoked on the common-case path"

    assert_eq "$(cd "$repo" && git rev-parse HEAD)" "$(git --git-dir="$base/origin.git" rev-parse feature/issue-999)" \
        "[$mode] the push actually landed on the (local, hermetic) origin"
    if [[ "$want" == merge ]]; then
        assert_eq "0" "$(git --git-dir="$base/origin.git" merge-base --is-ancestor "$pre_remote" feature/issue-999 && echo 0 || echo 1)" \
            "[$mode] merge mode never rewrites: the old remote head is an ancestor of the new one (no force push)"
    fi
}

check_case1 "$WORKROOT/case1-rebase"  rebase  "pinned rebase"
check_case1 "$WORKROOT/case1-merge"   merge   "merge"
check_case1 "$WORKROOT/case1-old"     missing "daemon without branch-update"
check_case2 "$WORKROOT/case2-rebase"  rebase  "pinned rebase" rebase
check_case2 "$WORKROOT/case2-merge"   merge   "merge"         merge
check_case2 "$WORKROOT/case2-old"     missing "daemon without branch-update" merge

# =====================================================================
# Case 3 (#9282): RESOLUTION_DISCARDED blocks the push exactly like EMPTIED,
# on an update whose result is clean and non-empty.
# =====================================================================
echo
echo "--- #9282: a dropped hand-made merge resolution blocks the push ---"

BASE3="$WORKROOT/case3"
REPO3="$(build_case2 "$BASE3")"
REMOTE_BEFORE3="$(git --git-dir="$BASE3/origin.git" rev-parse feature/issue-999)"

OUT3="$(GUARD_MODE=discarded run_dirty_block "$REPO3" merge 2>&1)"

assert_contains "$OUT3" "RESOLUTION_DISCARDED" "the guard's blocked answer is surfaced"
assert_not_contains "$(cat "$REPO3/gh.calls" 2>/dev/null)" "Automatically updated the branch" \
    "the success path never runs"
assert_eq "$REMOTE_BEFORE3" "$(git --git-dir="$BASE3/origin.git" rev-parse feature/issue-999)" \
    "the push never happened: origin's PR head is unchanged"
assert_contains "$(cat "$REPO3/post-verdict.calls" 2>/dev/null)" "changes-requested" \
    "falls back through the existing post-verdict.sh changes-requested path"
assert_contains "$(cat "$REPO3/gh.calls" 2>/dev/null)" "loom:merge-conflict" \
    "reuses the existing loom:merge-conflict label"

# =====================================================================
# Case 4 (#9282): an UNANSWERED guard does not block. A role prompt newer
# than the installed binary must not strand every update in the fleet -- but
# it must say, loudly, that the push went out unchecked.
# =====================================================================
echo
echo "--- #9282: a daemon predating the verb warns and does not block ---"

BASE4="$WORKROOT/case4"
REPO4="$(build_case2 "$BASE4")"

OUT4="$(GUARD_MODE=predates run_dirty_block "$REPO4" merge 2>&1)"

assert_contains "$OUT4" "WARNING: rebase-guard gave no answer (rc=2) - pushing UNCHECKED (#9282)" \
    "the unanswered guard is reported loudly, with its exit code"
assert_contains "$(cat "$REPO4/gh.calls" 2>/dev/null)" "Automatically updated the branch" \
    "the push still proceeds (non-blocking by design)"
assert_eq "$(cd "$REPO4" && git rev-parse HEAD)" "$(git --git-dir="$BASE4/origin.git" rev-parse feature/issue-999)" \
    "the updated head landed on origin"
assert_eq "" "$(cat "$REPO4/post-verdict.calls" 2>/dev/null || echo "")" \
    "no change request is posted for an unanswered guard"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
