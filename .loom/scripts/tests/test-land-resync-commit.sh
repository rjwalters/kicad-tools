#!/usr/bin/env bash
# test-land-resync-commit.sh - Smoke tests for land-resync-commit.sh (#6646)
#
# Constructs throwaway git repos (a bare "origin" + a primary checkout clone,
# plus a "third-party" clone standing in for another merged PR) to exercise
# the load-bearing cases. `gh` is a file-driven stub for the whole run (see
# below), so no test ever reaches a real forge.
#   (a) clean tree, nothing ahead           -> no-op, exit 0
#   (b) resync-only dirt, no divergence,    -> committed + pushed directly,
#       forge reports NO branch rules          exit 0 (the pre-check negative)
#   (c) resync dirt + a NON-Loom-authored   -> commits the resync, refuses to
#       commit already ahead of origin         push or rebase, exit 3; the
#                                               operator's commit SHA is
#                                               untouched (never recreated)
#   (d) resync dirt, direct push rejected   -> falls back to the STABLE
#       (origin advanced, no foreign            `chore/resync-installed`
#       commits locally)                        branch + PR (labeled
#                                               loom:review-requested), never
#                                               a forced/bypass push;
#                                               origin/<default> itself is
#                                               left untouched; the primary
#                                               checkout resets back to
#                                               origin's tip afterward with
#                                               the documented reflog
#                                               signature; no local side
#                                               branch is left behind
#   (d2) the fallback again, later          -> the same side branch is
#       (tree re-dirtied after the reset)      force-with-lease-updated and
#                                               the existing PR is ADOPTED --
#                                               no second branch, no second
#                                               `gh pr create`
#   (e) resync dirt + unrelated dirt        -> refuses to commit ANYTHING
#   (f) --dry-run                           -> preview only, no mutation
#   (g) invoked from a linked worktree      -> refuses (mirrors #4563)
#   (h) --allow-worktree                    -> permitted, warns
#   (i) untracked, unignored path matching a pure-copy-surface pattern but
#       with no defaults/ counterpart (the Loom source repo itself only)
#                                          -> excluded from the commit
#       (#6613/#7336 parity), left dirty in the tree; a legitimate resync
#       path alongside it still lands normally
#   (j) forge reports a `pull_request` rule -> direct push SKIPPED even though
#       on the default branch, no divergence   the bare origin would have
#                                               accepted it (a bypass-capable
#                                               identity would have been let
#                                               through); branch + PR instead
#   (k) forge reports no rules but the push -> exit 4, loud BYPASS error; the
#       is accepted with a `Bypassed rule        commit is on origin (never
#       violations` warning on stderr            undone: no force push)
#   (l) re-run idempotency: fetch fails     -> exit 1, commit stays local;
#       after the commit; remote restored;     re-run on the now-CLEAN tree
#       run again                               lands it (never "nothing to
#                                               land" with a stranded commit)
#   (l2) clean tree, only an operator commit-> exit 0, nothing pushed, the
#       ahead of origin                          operator's commit untouched
#   (m) the rules API call FAILS (non-zero  -> treated as PROTECTED (fail
#       exit), bare origin would accept the     closed): direct push SKIPPED,
#       plain push                              branch + PR instead; origin/
#                                               <default> untouched
#
# Usage:
#   ./.loom/scripts/tests/test-land-resync-commit.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/land-resync-commit.sh"

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-land-resync.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

LOOM_EMAIL="ci@loom.test"
LOOM_NAME="Loom CI"

# ---------- the `gh` stub (file-driven, shared by every test) ----------
#
# The script calls gh for exactly three things, all routed through here:
#   gh api repos/{owner}/{repo}/rules/branches/<default> --jq '.[].type'
#       -> answers with the contents of $GH_RULES_FILE (one rule type per
#          line, as gh's --jq would print; empty/missing file = no rules);
#          if $GH_RULES_FAIL_FILE exists the call FAILS instead (exit 1 with
#          an HTTP-style error on stderr, nothing on stdout) -- the
#          transient-REST-failure case of test (m)
#   gh api repos/{owner}/{repo}/branches/<default>/protection ...
#       -> always "Branch not protected" (exit 1), the legacy-API negative
#   gh pr list --head <branch> ...  (create-pr.sh's adopt-first lookup)
#       -> answers with $GH_EXISTING_PR_FILE's contents, or `null`
#   gh pr create ...                (create-pr.sh)
#       -> a fixed URL
# Every call is appended to $GH_CALLS_LOG so tests can assert what was (not)
# invoked. Putting the stub on PATH for the WHOLE run (plus LOOM_FORGE_TYPE)
# keeps every test hermetic -- a real gh reaching the network from a test
# repo whose origin is a local path is never wanted, and CI has no forge.
STUB_BIN="$WORKDIR/stub-bin"
mkdir -p "$STUB_BIN"
GH_RULES_FILE="$WORKDIR/gh-rules"
GH_RULES_FAIL_FILE="$WORKDIR/gh-rules-fail"
GH_EXISTING_PR_FILE="$WORKDIR/gh-existing-pr"
GH_CALLS_LOG="$WORKDIR/gh-calls.log"
cat > "$STUB_BIN/gh" <<'STUB'
#!/usr/bin/env bash
D="$(cd "$(dirname "$0")/.." && pwd)"
printf '%s\n' "gh $*" | tr '\n' ' ' >> "$D/gh-calls.log"; echo >> "$D/gh-calls.log"   # one line per call (bodies contain newlines)
case "$1 $2" in
    "pr list") cat "$D/gh-existing-pr" 2>/dev/null || echo null ;;
    "pr create") echo "https://example.invalid/pr/999" ;;
    api\ */rules/branches/*)
        if [[ -e "$D/gh-rules-fail" ]]; then echo 'gh: HTTP 503: Service Unavailable' >&2; exit 1; fi
        cat "$D/gh-rules" 2>/dev/null; exit 0 ;;
    api\ */protection) echo '{"message":"Branch not protected"}' >&2; exit 1 ;;
    *) echo "stub gh: unhandled args: $*" >&2; exit 3 ;;
esac
STUB
chmod +x "$STUB_BIN/gh"
export PATH="$STUB_BIN:$PATH"
export LOOM_FORGE_TYPE=github

# gh_stub_reset [rule types...] -> clears the call log, the adopted-PR
# answer and the rules-call-fails switch, and sets the rules answer to the
# given types (none = unprotected).
gh_stub_reset() {
    : > "$GH_CALLS_LOG"
    rm -f "$GH_EXISTING_PR_FILE" "$GH_RULES_FAIL_FILE"
    : > "$GH_RULES_FILE"
    local t
    for t in "$@"; do printf '%s\n' "$t" >> "$GH_RULES_FILE"; done
}

# make_origin <name> -> creates $WORKDIR/<name>.git, a bare repo with its HEAD
# symref pointed at refs/heads/main (so a from-empty clone can check out and
# commit into "main" directly, rather than landing detached).
make_origin() {
    local name="$1"
    git init --bare -q "$WORKDIR/$name.git"
    git --git-dir="$WORKDIR/$name.git" symbolic-ref HEAD refs/heads/main
}

# make_primary <origin-name> <clone-name> -> clones <origin-name>.git into
# $WORKDIR/<clone-name>, configures the Loom automation identity, seeds one
# resync-managed file, commits, and pushes it as the initial "main".
make_primary() {
    local origin_name="$1" clone_name="$2"
    git clone -q "$WORKDIR/$origin_name.git" "$WORKDIR/$clone_name"
    git -C "$WORKDIR/$clone_name" config user.email "$LOOM_EMAIL"
    git -C "$WORKDIR/$clone_name" config user.name "$LOOM_NAME"
    mkdir -p "$WORKDIR/$clone_name/.loom/hooks"
    printf 'initial\n' > "$WORKDIR/$clone_name/.loom/hooks/foo.sh"
    git -C "$WORKDIR/$clone_name" checkout -q -b main
    git -C "$WORKDIR/$clone_name" add -A
    git -C "$WORKDIR/$clone_name" commit -q -m init
    git -C "$WORKDIR/$clone_name" push -q -u origin main
}

# advance_origin <origin-name> <clone-name> -> a third party lands an
# unrelated commit on origin/main that the primary hasn't fetched.
advance_origin() {
    local origin_name="$1" clone_name="$2"
    git clone -q "$WORKDIR/$origin_name.git" "$WORKDIR/$clone_name"
    git -C "$WORKDIR/$clone_name" config user.email "someone@example.com"
    git -C "$WORKDIR/$clone_name" config user.name "Someone Else"
    printf 'other change %s\n' "$RANDOM" > "$WORKDIR/$clone_name/other-file-$RANDOM.txt"
    git -C "$WORKDIR/$clone_name" add -A
    git -C "$WORKDIR/$clone_name" commit -q -m "unrelated merged PR"
    git -C "$WORKDIR/$clone_name" push -q origin main
}

echo ""
echo "=== (a) clean tree -> no-op ==="
gh_stub_reset
make_origin origin-a
make_primary origin-a primary-a
OUT="$(cd "$WORKDIR/primary-a" && "$SCRIPT" 2>&1)"; RC=$?
if [[ $RC -eq 0 ]] && grep -q "nothing to land" <<< "$OUT"; then
    pass "clean tree exits 0 with a 'nothing to land' message"
else
    fail "clean tree exits 0 with a 'nothing to land' message (rc=$RC, out=$OUT)"
fi

echo ""
echo "=== (b) resync-only dirt, no divergence, no branch rules -> committed + pushed directly ==="
gh_stub_reset
make_origin origin-b
make_primary origin-b primary-b
printf 'updated\n' > "$WORKDIR/primary-b/.loom/hooks/foo.sh"
OUT="$(cd "$WORKDIR/primary-b" && "$SCRIPT" 2>&1)"; RC=$?
ORIGIN_LOG="$(git --git-dir="$WORKDIR/origin-b.git" log --oneline main)"
if [[ $RC -eq 0 ]] && grep -q "pushed" <<< "$OUT" && grep -q "resync installed Loom surfaces" <<< "$ORIGIN_LOG"; then
    pass "resync-only dirt with no divergence is committed and pushed directly"
else
    fail "resync-only dirt with no divergence is committed and pushed directly (rc=$RC, out=$OUT, origin_log=$ORIGIN_LOG)"
fi
if [[ -z "$(git -C "$WORKDIR/primary-b" status --porcelain)" ]]; then
    pass "primary checkout is clean after a direct push"
else
    fail "primary checkout is clean after a direct push"
fi
if grep -q "rules/branches/main" "$GH_CALLS_LOG" && ! grep -q "^gh pr create" "$GH_CALLS_LOG"; then
    pass "the branch-rules pre-check was consulted and, with no rules, no PR was opened (pre-check negative)"
else
    fail "the branch-rules pre-check was consulted and, with no rules, no PR was opened (calls=$(cat "$GH_CALLS_LOG"))"
fi

echo ""
echo "=== (c) resync dirt + a non-Loom commit already ahead -> commit, refuse to push/rebase ==="
gh_stub_reset
make_origin origin-c
make_primary origin-c primary-c
git -C "$WORKDIR/primary-c" config user.email "operator@example.com"
git -C "$WORKDIR/primary-c" config user.name "An Operator"
printf 'operator change\n' > "$WORKDIR/primary-c/operator-file.txt"
git -C "$WORKDIR/primary-c" add -A
git -C "$WORKDIR/primary-c" commit -q -m "operator: local tooling commit"
OPERATOR_SHA="$(git -C "$WORKDIR/primary-c" rev-parse HEAD)"
git -C "$WORKDIR/primary-c" config user.email "$LOOM_EMAIL"
git -C "$WORKDIR/primary-c" config user.name "$LOOM_NAME"
printf 'updated\n' > "$WORKDIR/primary-c/.loom/hooks/foo.sh"
OUT="$(cd "$WORKDIR/primary-c" && "$SCRIPT" 2>&1)"; RC=$?
ORIGIN_LOG="$(git --git-dir="$WORKDIR/origin-c.git" log --oneline main)"
if [[ $RC -eq 3 ]] && grep -q "NOT pushed" <<< "$OUT" && grep -q "operator commit" <<< "$OUT"; then
    pass "operator commit ahead -> stops with exit 3 and names it"
else
    fail "operator commit ahead -> stops with exit 3 and names it (rc=$RC, out=$OUT)"
fi
if ! grep -q "resync installed Loom surfaces" <<< "$ORIGIN_LOG"; then
    pass "origin is untouched when stopping for an operator commit ahead"
else
    fail "origin is untouched when stopping for an operator commit ahead (origin_log=$ORIGIN_LOG)"
fi
if git -C "$WORKDIR/primary-c" cat-file -e "$OPERATOR_SHA" 2>/dev/null && \
   [[ "$(git -C "$WORKDIR/primary-c" log --format='%H' | sed -n '2p')" == "$OPERATOR_SHA" ]]; then
    pass "the operator's commit SHA is preserved verbatim (never rebased/recreated)"
else
    fail "the operator's commit SHA is preserved verbatim (never rebased/recreated)"
fi
if git -C "$WORKDIR/primary-c" log -1 --format='%s' | grep -q "resync installed Loom surfaces"; then
    pass "the resync commit itself was still made locally (just not pushed)"
else
    fail "the resync commit itself was still made locally (just not pushed)"
fi

echo ""
echo "=== (d) direct push rejected (origin advanced, no foreign local commits) -> branch + PR fallback ==="
gh_stub_reset
make_origin origin-d
make_primary origin-d primary-d
advance_origin origin-d other-d
printf 'updated\n' > "$WORKDIR/primary-d/.loom/hooks/foo.sh"
OUT="$(cd "$WORKDIR/primary-d" && "$SCRIPT" 2>&1)"; RC=$?
ORIGIN_MAIN_LOG="$(git --git-dir="$WORKDIR/origin-d.git" log --oneline main)"
if [[ $RC -eq 0 ]] && grep -q "opened https://example.invalid/pr/999" <<< "$OUT"; then
    pass "rejected push falls back to a branch + PR and exits 0"
else
    fail "rejected push falls back to a branch + PR and exits 0 (rc=$RC, out=$OUT)"
fi
if ! grep -q "resync installed Loom surfaces" <<< "$ORIGIN_MAIN_LOG"; then
    pass "origin/<default> itself is never bypass-pushed — only the fallback branch carries the commit"
else
    fail "origin/<default> itself is never bypass-pushed (origin_main_log=$ORIGIN_MAIN_LOG)"
fi
SIDE_BRANCHES="$(git --git-dir="$WORKDIR/origin-d.git" for-each-ref --format='%(refname:short)' 'refs/heads/chore/*')"
if [[ "$SIDE_BRANCHES" == "chore/resync-installed" ]]; then
    pass "the STABLE chore/resync-installed branch (no timestamp suffix) was pushed to origin"
else
    fail "the STABLE chore/resync-installed branch (no timestamp suffix) was pushed to origin (got: $SIDE_BRANCHES)"
fi
FIRST_FALLBACK_SHA="$(git --git-dir="$WORKDIR/origin-d.git" rev-parse refs/heads/chore/resync-installed)"
if git --git-dir="$WORKDIR/origin-d.git" log -1 --format='%s' refs/heads/chore/resync-installed | grep -q "resync installed Loom surfaces"; then
    pass "the side branch on origin carries the resync commit"
else
    fail "the side branch on origin carries the resync commit"
fi
if grep -q "^gh pr create" "$GH_CALLS_LOG" 2>/dev/null; then
    pass "create-pr.sh's gh pr create was invoked for the fallback branch"
else
    fail "create-pr.sh's gh pr create was invoked for the fallback branch"
fi
if grep "^gh pr create" "$GH_CALLS_LOG" 2>/dev/null | grep -q -- "--label loom:review-requested"; then
    pass "the fallback PR is created with loom:review-requested so it enters the Judge queue"
else
    fail "the fallback PR is created with loom:review-requested (calls=$(cat "$GH_CALLS_LOG"))"
fi
if [[ -z "$(git -C "$WORKDIR/primary-d" status --porcelain)" ]] && \
   [[ "$(git -C "$WORKDIR/primary-d" rev-parse HEAD)" == "$(git -C "$WORKDIR/primary-d" rev-parse origin/main)" ]]; then
    pass "primary checkout resets back to origin's tip after the branch+PR fallback (never sits diverged)"
else
    fail "primary checkout resets back to origin's tip after the branch+PR fallback"
fi
if ! git -C "$WORKDIR/primary-d" show-ref --verify --quiet refs/heads/chore/resync-installed; then
    pass "no local chore/resync-installed branch is left behind in the primary checkout"
else
    fail "no local chore/resync-installed branch is left behind in the primary checkout"
fi
# The documented forensic signature (troubleshooting.md): the fallback writes
# exactly `commit: chore: resync…` immediately followed by `reset: moving to
# origin/main` on the default branch's reflog -- and never a `rebase`.
REFLOG="$(git -C "$WORKDIR/primary-d" reflog show --format='%gs' main)"
if [[ "$(sed -n '1p' <<< "$REFLOG")" == "reset: moving to origin/main" ]] && \
   [[ "$(sed -n '2p' <<< "$REFLOG")" == "commit: chore: resync installed Loom surfaces" ]] && \
   ! grep -q "rebase" <<< "$REFLOG"; then
    pass "the default branch's reflog shows the documented fallback signature (commit, then reset to origin) and no rebase"
else
    fail "the default branch's reflog shows the documented fallback signature (got: $(tr '\n' '|' <<< "$REFLOG"))"
fi

echo ""
echo "=== (d2) fallback again after the reset -> same side branch updated, existing PR adopted ==="
: > "$GH_CALLS_LOG"
printf 'https://example.invalid/pr/999\n' > "$GH_EXISTING_PR_FILE"   # the PR from (d) is still open
advance_origin origin-d other-d2                                       # origin moves on again
printf 'updated once more\n' > "$WORKDIR/primary-d/.loom/hooks/foo.sh"  # resync re-dirties the tree
OUT="$(cd "$WORKDIR/primary-d" && "$SCRIPT" 2>&1)"; RC=$?
SIDE_BRANCHES="$(git --git-dir="$WORKDIR/origin-d.git" for-each-ref --format='%(refname:short)' 'refs/heads/chore/*')"
SECOND_FALLBACK_SHA="$(git --git-dir="$WORKDIR/origin-d.git" rev-parse refs/heads/chore/resync-installed)"
if [[ $RC -eq 0 ]] && grep -q "opened https://example.invalid/pr/999" <<< "$OUT"; then
    pass "a repeated fallback exits 0 and reports the (adopted) PR"
else
    fail "a repeated fallback exits 0 and reports the (adopted) PR (rc=$RC, out=$OUT)"
fi
if [[ "$SIDE_BRANCHES" == "chore/resync-installed" ]] && [[ "$SECOND_FALLBACK_SHA" != "$FIRST_FALLBACK_SHA" ]] && \
   git --git-dir="$WORKDIR/origin-d.git" show "refs/heads/chore/resync-installed:.loom/hooks/foo.sh" | grep -q "updated once more"; then
    pass "the single side branch was force-with-lease-updated to the fresh commit (no second branch)"
else
    fail "the single side branch was force-with-lease-updated to the fresh commit (branches=$SIDE_BRANCHES, first=$FIRST_FALLBACK_SHA, second=$SECOND_FALLBACK_SHA)"
fi
if grep -q "^gh pr list" "$GH_CALLS_LOG" && ! grep -q "^gh pr create" "$GH_CALLS_LOG"; then
    pass "create-pr.sh adopted the existing open PR instead of opening a duplicate"
else
    fail "create-pr.sh adopted the existing open PR instead of opening a duplicate (calls=$(cat "$GH_CALLS_LOG"))"
fi
if ! grep -q "resync installed Loom surfaces" "$(git --git-dir="$WORKDIR/origin-d.git" log --oneline main | head -50 > "$WORKDIR/d2-main.log"; echo "$WORKDIR/d2-main.log")"; then
    pass "origin/<default> is still untouched after the repeated fallback"
else
    fail "origin/<default> is still untouched after the repeated fallback"
fi

echo ""
echo "=== (e) resync dirt + unrelated dirt -> refuses to commit ANYTHING ==="
gh_stub_reset
make_origin origin-e
make_primary origin-e primary-e
printf 'updated\n' > "$WORKDIR/primary-e/.loom/hooks/foo.sh"
printf 'unrelated\n' > "$WORKDIR/primary-e/scratch.txt"
BEFORE_SHA="$(git -C "$WORKDIR/primary-e" rev-parse HEAD)"
OUT="$(cd "$WORKDIR/primary-e" && "$SCRIPT" 2>&1)"; RC=$?
AFTER_SHA="$(git -C "$WORKDIR/primary-e" rev-parse HEAD)"
if [[ $RC -ne 0 ]] && grep -q "non-resync dirt" <<< "$OUT" && [[ "$BEFORE_SHA" == "$AFTER_SHA" ]]; then
    pass "unrelated dirt alongside resync output refuses to commit anything"
else
    fail "unrelated dirt alongside resync output refuses to commit anything (rc=$RC, out=$OUT)"
fi
if [[ -n "$(git -C "$WORKDIR/primary-e" status --porcelain)" ]]; then
    pass "the tree is left exactly as dirty as before (both files still uncommitted)"
else
    fail "the tree is left exactly as dirty as before"
fi

echo ""
echo "=== (f) --dry-run previews only, no mutation ==="
gh_stub_reset
make_origin origin-f
make_primary origin-f primary-f
printf 'updated\n' > "$WORKDIR/primary-f/.loom/hooks/foo.sh"
BEFORE_STATUS="$(git -C "$WORKDIR/primary-f" status --porcelain)"
BEFORE_SHA="$(git -C "$WORKDIR/primary-f" rev-parse HEAD)"
OUT="$(cd "$WORKDIR/primary-f" && "$SCRIPT" --dry-run 2>&1)"; RC=$?
AFTER_STATUS="$(git -C "$WORKDIR/primary-f" status --porcelain)"
AFTER_SHA="$(git -C "$WORKDIR/primary-f" rev-parse HEAD)"
if [[ $RC -eq 0 ]] && grep -q "\[dry-run\]" <<< "$OUT" && \
   [[ "$BEFORE_STATUS" == "$AFTER_STATUS" ]] && [[ "$BEFORE_SHA" == "$AFTER_SHA" ]]; then
    pass "--dry-run previews without committing or pushing"
else
    fail "--dry-run previews without committing or pushing (rc=$RC, out=$OUT)"
fi

echo ""
echo "=== (g) invoked from a linked worktree -> refuses (mirrors #4563) ==="
gh_stub_reset
make_origin origin-g
make_primary origin-g primary-g
git -C "$WORKDIR/primary-g" worktree add -q -b feature/issue-1 "$WORKDIR/primary-g-wt" main
printf 'updated\n' > "$WORKDIR/primary-g/.loom/hooks/foo.sh"
OUT="$(cd "$WORKDIR/primary-g-wt" && "$SCRIPT" 2>&1)"; RC=$?
if [[ $RC -ne 0 ]] && grep -qi "linked git worktree" <<< "$OUT"; then
    pass "refuses to run from a linked worktree"
else
    fail "refuses to run from a linked worktree (rc=$RC, out=$OUT)"
fi
if [[ -n "$(git -C "$WORKDIR/primary-g" status --porcelain)" ]] && \
   [[ "$(git -C "$WORKDIR/primary-g" rev-parse HEAD)" != "" ]] && \
   ! git -C "$WORKDIR/primary-g" log -1 --format='%s' | grep -q "resync installed Loom surfaces"; then
    pass "the primary checkout's dirt is untouched by the refused worktree invocation"
else
    fail "the primary checkout's dirt is untouched by the refused worktree invocation"
fi

echo ""
echo "=== (h) --allow-worktree permits operating on the main checkout from a worktree ==="
OUT="$(cd "$WORKDIR/primary-g-wt" && "$SCRIPT" --allow-worktree 2>&1)"; RC=$?
ORIGIN_LOG="$(git --git-dir="$WORKDIR/origin-g.git" log --oneline main)"
if [[ $RC -eq 0 ]] && grep -qi "allow-worktree" <<< "$OUT" && grep -q "resync installed Loom surfaces" <<< "$ORIGIN_LOG"; then
    pass "--allow-worktree permits landing the commit from a linked worktree, warning loudly"
else
    fail "--allow-worktree permits landing the commit from a linked worktree, warning loudly (rc=$RC, out=$OUT)"
fi
git -C "$WORKDIR/primary-g" worktree remove --force "$WORKDIR/primary-g-wt" 2>/dev/null || true

echo ""
echo "=== (i) retired-but-unlisted pure-copy-surface path is excluded from the commit (#6613/#7336 parity) ==="
gh_stub_reset
make_origin origin-i
make_primary origin-i primary-i
# Only IS_LOOM_SOURCE_REPO=1 (a local defaults/ tree) activates this check.
# Committed as scaffolding FIRST so it isn't itself flagged as unrelated dirt
# when the actual test scenario below runs.
mkdir -p "$WORKDIR/primary-i/defaults/hooks" "$WORKDIR/primary-i/defaults/scripts"
printf 'placeholder\n' > "$WORKDIR/primary-i/defaults/hooks/placeholder.sh"
git -C "$WORKDIR/primary-i" add -A
git -C "$WORKDIR/primary-i" commit -q -m "scaffold: defaults/ tree"
git -C "$WORKDIR/primary-i" push -q origin main
# A legitimate resync change: tracked-and-modified, always included.
printf 'updated\n' > "$WORKDIR/primary-i/.loom/hooks/foo.sh"
# An untracked file matching the pure-copy-surface pattern with NO defaults/
# counterpart -- presumed retired-but-unlisted, must be excluded.
mkdir -p "$WORKDIR/primary-i/.loom/scripts"
printf 'ORPHAN\n' > "$WORKDIR/primary-i/.loom/scripts/some-retired-tool.sh"
OUT="$(cd "$WORKDIR/primary-i" && "$SCRIPT" 2>&1)"; RC=$?
if [[ $RC -eq 0 ]] && grep -q "Excluded from the commit" <<< "$OUT" && grep -q "some-retired-tool.sh" <<< "$OUT"; then
    pass "retired-but-unlisted path is flagged and excluded, run still lands the legitimate change"
else
    fail "retired-but-unlisted path is flagged and excluded, run still lands the legitimate change (rc=$RC, out=$OUT)"
fi
ORIGIN_LOG_TREE="$(git --git-dir="$WORKDIR/origin-i.git" log -1 --format='%H' main | xargs -I{} git --git-dir="$WORKDIR/origin-i.git" ls-tree -r --name-only {})"
if ! grep -q "some-retired-tool.sh" <<< "$ORIGIN_LOG_TREE"; then
    pass "the retired-but-unlisted file was never actually committed"
else
    fail "the retired-but-unlisted file was never actually committed"
fi
if grep -q "\.loom/hooks/foo\.sh" <<< "$ORIGIN_LOG_TREE"; then
    pass "the legitimate resync-managed change was still committed and pushed"
else
    fail "the legitimate resync-managed change was still committed and pushed"
fi
if [[ -f "$WORKDIR/primary-i/.loom/scripts/some-retired-tool.sh" ]] && \
   [[ -n "$(git -C "$WORKDIR/primary-i" status --porcelain -- .loom/scripts/some-retired-tool.sh)" ]]; then
    pass "the excluded file is left untouched (still present, still untracked) for a human to reconcile"
else
    fail "the excluded file is left untouched (still present, still untracked) for a human to reconcile"
fi

echo ""
echo "=== (j) forge reports a pull_request rule on the default branch -> direct push skipped, branch + PR ==="
gh_stub_reset pull_request required_status_checks
make_origin origin-j
make_primary origin-j primary-j
# NO divergence: the bare origin WOULD accept a plain push -- exactly the case
# where a bypass-capable identity gets let through on a real forge.
printf 'updated\n' > "$WORKDIR/primary-j/.loom/hooks/foo.sh"
OUT="$(cd "$WORKDIR/primary-j" && "$SCRIPT" 2>&1)"; RC=$?
ORIGIN_MAIN_LOG="$(git --git-dir="$WORKDIR/origin-j.git" log --oneline main)"
if [[ $RC -eq 0 ]] && grep -q "is protected" <<< "$OUT" && grep -q "opened https://example.invalid/pr/999" <<< "$OUT"; then
    pass "a protected default branch routes to the branch + PR path without attempting the direct push"
else
    fail "a protected default branch routes to the branch + PR path without attempting the direct push (rc=$RC, out=$OUT)"
fi
if ! grep -q "resync installed Loom surfaces" <<< "$ORIGIN_MAIN_LOG"; then
    pass "origin/<default> did NOT receive the commit even though it would have accepted the plain push"
else
    fail "origin/<default> did NOT receive the commit even though it would have accepted the plain push (origin_main_log=$ORIGIN_MAIN_LOG)"
fi
if git --git-dir="$WORKDIR/origin-j.git" show-ref --verify --quiet refs/heads/chore/resync-installed && \
   grep -q "^gh pr create" "$GH_CALLS_LOG"; then
    pass "the side branch was pushed and a PR opened for it"
else
    fail "the side branch was pushed and a PR opened for it (calls=$(cat "$GH_CALLS_LOG"))"
fi

echo ""
echo "=== (k) forge reports no rules but the push is accepted with a 'Bypassed rule violations' warning -> exit 4 ==="
gh_stub_reset
make_origin origin-k
make_primary origin-k primary-k
# Stand in for GitHub letting a bypass-capable identity through: a
# pre-receive hook (installed AFTER the seed push) that accepts the push but
# prints the warning GitHub prints. Git relays hook stderr to the client as
# `remote: ...` lines.
cat > "$WORKDIR/origin-k.git/hooks/pre-receive" <<'HOOK'
#!/usr/bin/env bash
echo "Bypassed rule violations for refs/heads/main:" >&2
echo "" >&2
echo "- Changes must be made through a pull request." >&2
exit 0
HOOK
chmod +x "$WORKDIR/origin-k.git/hooks/pre-receive"
printf 'updated\n' > "$WORKDIR/primary-k/.loom/hooks/foo.sh"
OUT="$(cd "$WORKDIR/primary-k" && "$SCRIPT" 2>&1)"; RC=$?
ORIGIN_MAIN_LOG="$(git --git-dir="$WORKDIR/origin-k.git" log --oneline main)"
if [[ $RC -eq 4 ]] && grep -q "BYPASS PUSH DETECTED" <<< "$OUT" && grep -q "Bypassed rule violations" <<< "$OUT"; then
    pass "an accepted-but-bypassed push is reported as a loud failure (exit 4) quoting the forge's warning"
else
    fail "an accepted-but-bypassed push is reported as a loud failure (exit 4) quoting the forge's warning (rc=$RC, out=$OUT)"
fi
if grep -q "resync installed Loom surfaces" <<< "$ORIGIN_MAIN_LOG"; then
    pass "the bypassed commit is left on origin (never force-pushed away) and the failure says so"
else
    fail "the bypassed commit is left on origin (never force-pushed away) (origin_main_log=$ORIGIN_MAIN_LOG)"
fi

echo ""
echo "=== (l) re-run idempotency: fetch fails after the commit, then a clean-tree re-run lands it ==="
gh_stub_reset
make_origin origin-l
make_primary origin-l primary-l
printf 'updated\n' > "$WORKDIR/primary-l/.loom/hooks/foo.sh"
git -C "$WORKDIR/primary-l" remote set-url origin "$WORKDIR/does-not-exist.git"
OUT="$(cd "$WORKDIR/primary-l" && "$SCRIPT" 2>&1)"; RC=$?
if [[ $RC -eq 1 ]] && grep -q "Could not fetch" <<< "$OUT" && \
   git -C "$WORKDIR/primary-l" log -1 --format='%s' | grep -q "resync installed Loom surfaces" && \
   [[ -z "$(git -C "$WORKDIR/primary-l" status --porcelain)" ]]; then
    pass "a fetch failure after the commit exits 1 with the resync commit left local (tree now clean)"
else
    fail "a fetch failure after the commit exits 1 with the resync commit left local (rc=$RC, out=$OUT)"
fi
STRANDED_SHA="$(git -C "$WORKDIR/primary-l" rev-parse HEAD)"
git -C "$WORKDIR/primary-l" remote set-url origin "$WORKDIR/origin-l.git"
OUT="$(cd "$WORKDIR/primary-l" && "$SCRIPT" 2>&1)"; RC=$?
ORIGIN_LOG="$(git --git-dir="$WORKDIR/origin-l.git" log --format='%H %s' main)"
if [[ $RC -eq 0 ]] && grep -q "from an earlier run" <<< "$OUT" && grep -q "pushed" <<< "$OUT" && \
   grep -q "^$STRANDED_SHA chore: resync installed Loom surfaces" <<< "$ORIGIN_LOG"; then
    pass "the clean-tree re-run lands the stranded commit (same SHA, no rebase) instead of reporting 'nothing to land'"
else
    fail "the clean-tree re-run lands the stranded commit (rc=$RC, out=$OUT, origin_log=$ORIGIN_LOG)"
fi
OUT="$(cd "$WORKDIR/primary-l" && "$SCRIPT" 2>&1)"; RC=$?
if [[ $RC -eq 0 ]] && grep -q "nothing to land" <<< "$OUT"; then
    pass "a third run, with everything landed, is a clean no-op"
else
    fail "a third run, with everything landed, is a clean no-op (rc=$RC, out=$OUT)"
fi

echo ""
echo "=== (l2) clean tree, only an operator commit ahead of origin -> no-op, untouched ==="
gh_stub_reset
make_origin origin-l2
make_primary origin-l2 primary-l2
git -C "$WORKDIR/primary-l2" config user.email "operator@example.com"
printf 'operator change\n' > "$WORKDIR/primary-l2/operator-file.txt"
git -C "$WORKDIR/primary-l2" add -A
git -C "$WORKDIR/primary-l2" commit -q -m "operator: local tooling commit"
git -C "$WORKDIR/primary-l2" config user.email "$LOOM_EMAIL"
OPERATOR_SHA="$(git -C "$WORKDIR/primary-l2" rev-parse HEAD)"
OUT="$(cd "$WORKDIR/primary-l2" && "$SCRIPT" 2>&1)"; RC=$?
ORIGIN_LOG="$(git --git-dir="$WORKDIR/origin-l2.git" log --format='%H' main)"
if [[ $RC -eq 0 ]] && grep -q "nothing to land" <<< "$OUT" && grep -q "operator work" <<< "$OUT" && \
   ! grep -q "$OPERATOR_SHA" <<< "$ORIGIN_LOG" && \
   [[ "$(git -C "$WORKDIR/primary-l2" rev-parse HEAD)" == "$OPERATOR_SHA" ]]; then
    pass "an operator's unpushed commit on a clean tree is neither pushed nor touched"
else
    fail "an operator's unpushed commit on a clean tree is neither pushed nor touched (rc=$RC, out=$OUT)"
fi

echo ""
echo "=== (m) the rules API call FAILS (non-zero exit) -> treated as protected: direct push skipped, branch + PR ==="
gh_stub_reset
touch "$GH_RULES_FAIL_FILE"
make_origin origin-m
make_primary origin-m primary-m
# NO divergence and NO rules answer at all: the bare origin WOULD accept a
# plain push. Before the fail-closed narrowing, "the rules call failed" and
# "the forge reported no rules" were indistinguishable, so a transient REST
# failure with a bypass-capable identity still bypass-pushed (#6646).
printf 'updated\n' > "$WORKDIR/primary-m/.loom/hooks/foo.sh"
OUT="$(cd "$WORKDIR/primary-m" && "$SCRIPT" 2>&1)"; RC=$?
ORIGIN_MAIN_LOG="$(git --git-dir="$WORKDIR/origin-m.git" log --oneline main)"
if [[ $RC -eq 0 ]] && grep -q "fail closed" <<< "$OUT" && grep -q "opened https://example.invalid/pr/999" <<< "$OUT"; then
    pass "a FAILED rules lookup is treated as protected and routes to the branch + PR path (fail closed)"
else
    fail "a FAILED rules lookup is treated as protected and routes to the branch + PR path (fail closed) (rc=$RC, out=$OUT)"
fi
if ! grep -q "resync installed Loom surfaces" <<< "$ORIGIN_MAIN_LOG" && \
   git --git-dir="$WORKDIR/origin-m.git" show-ref --verify --quiet refs/heads/chore/resync-installed; then
    pass "origin/<default> did NOT receive the commit (no direct push was attempted); the side branch was pushed instead"
else
    fail "origin/<default> did NOT receive the commit (no direct push was attempted); the side branch was pushed instead (origin_main_log=$ORIGIN_MAIN_LOG)"
fi
if grep -q "rules/branches/main" "$GH_CALLS_LOG" && ! grep -q "branches/main/protection" "$GH_CALLS_LOG"; then
    pass "the rules endpoint was consulted and its failure short-circuited (no legacy /protection lookup could downgrade it to 'unprotected')"
else
    fail "the rules endpoint was consulted and its failure short-circuited (calls=$(cat "$GH_CALLS_LOG"))"
fi

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
