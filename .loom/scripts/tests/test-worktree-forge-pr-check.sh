#!/usr/bin/env bash
# test-worktree-forge-pr-check.sh — Tests for #7765.
#
# `worktree.sh N` used to resolve the issue branch ONLY against `origin`
# (`git fetch origin "$BRANCH_NAME" 2>/dev/null || true`, then a plain
# `show-ref` check). When that ref was absent it fell straight through to
# creating a FRESH branch at the base ref's HEAD under the same name and
# reported success — with zero forge awareness of whether an open PR already
# claims that branch name. This is blind to:
#   - a cross-repository (fork) PR, whose head branch never shows up as
#     origin/<branch> at all (rjwalters/loom#7765's headline case)
#   - a same-repo PR whose head simply wasn't fetched yet by the plain-name
#     fetch (a variant of #4823's in-flight-cycle case, reachable even when
#     that fetch fails/misses)
#   - a forge query that itself fails (gh missing/unauthenticated/rate-limited)
#     — which is NOT the same as "confirmed no PR exists"
#
# This suite verifies `_worktree_open_pr_for_branch` (the new helper) is
# consulted before falling through to a fresh branch, in exactly the case
# where NEITHER a local branch NOR `refs/remotes/origin/$BRANCH_NAME` exists:
#   1. Cross-repo open PR found -> worktree.sh REFUSES (exit != 0), never
#      silently reports success on a fresh main-HEAD branch of the same name.
#   2. Same-repo open PR found, but its ref wasn't fetched by the plain-name
#      fetch -> worktree.sh fetches it (via refs/pull/<n>/head, which the
#      forge publishes for every open PR) and REUSES it (#4823 extended).
#   3. Forge query genuinely unavailable (gh present but the query itself
#      fails, e.g. auth/rate-limit) -> worktree.sh REFUSES rather than
#      guessing "safe to create fresh".
#   4. gh itself reports that no remote points at a known GitHub host ->
#      treated as "no PR to shadow"; worktree.sh still creates the fresh
#      branch as before.
#   5. No open PR matches this branch name at all (forge reachable, confirmed
#      clean) -> worktree.sh still creates the fresh branch as before.
#   6. Every remote is a local filesystem path AND gh is unauthenticated (so
#      it bails out before it can emit the scenario-4 signal) -> still
#      classified "no forge remote", worktree still created. This is the
#      #7863 regression: it was misfiled as scenario 3 and refused, breaking
#      every hermetic suite that builds a synthetic local-origin repo.
#   7. Every remote is a local filesystem path -> the forge is not consulted
#      at all (the decision is made from the remote URLs, with no round-trip).
#
# Companion to test-worktree-stale-merged-branch.sh (#5657) and
# test-worktree-remote-branch-tracking.sh (#4823) — both must keep passing
# unmodified (verified separately); this suite exercises the NEW code path
# that only runs when origin has no ref under $BRANCH_NAME at all.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Build a throwaway repo with an origin/main ref and NOTHING pushed under
# feature/issue-<issue> at all (neither locally nor on origin) — the "no ref
# anywhere" gap this issue targets. When with_pull_ref=true, also publishes
# refs/pull/<pr_number>/head on origin carrying a unique artifact, modeling
# GitHub's own auto-published PR merge ref (present for every open PR,
# same-repo or cross-repo) WITHOUT ever pushing the branch under its own name
# to origin — i.e. the exact ref the #4823-reuse fetch above cannot find, but
# the forge's PR-number-addressed ref can. Echoes the working-tree path.
setup_repo() {
    local name="$1"
    local with_pull_ref="${2:-false}"
    local pr_number="${3:-}"
    # Whether this synthetic repo should look like it has a forge relationship
    # at all. `origin` is always the local bare repo (so fetch/push stay
    # hermetic — no network), so a repo that is supposed to HAVE a forge gets
    # a second, never-fetched remote carrying a real GitHub URL. Without it
    # the repo is a pure local-filesystem clone, which is now (correctly)
    # classified `no_forge_remote` with no forge call at all — #7863.
    local with_forge_remote="${4:-true}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtforge.XXXXXX)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/$name"
    (
        cd "$tmp/$name"
        git config user.email t@t
        git config user.name t
        git commit --allow-empty -q -m init
        git remote add origin "$tmp/origin.git"
        git push -q origin main
        if [[ "$with_forge_remote" == "true" ]]; then
            git remote add forge https://github.com/rjwalters/loom.git
        fi
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh

        if [[ "$with_pull_ref" == "true" ]]; then
            git checkout -q -b pr-source-branch
            echo "same-repo-pr-artifact" > pr-artifact.txt
            git add pr-artifact.txt
            git commit -q -m "same-repo PR #$pr_number artifact"
            git push -q origin "HEAD:refs/pull/$pr_number/head"
            git checkout -q main
            git branch -q -D pr-source-branch
        fi
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# A minimal `gh` stand-in on PATH, modeling
# `pr list --state open --head <branch> --json number,isCrossRepository,headRepository,headRefName,url --limit 5`
# (also answers any stray `pr list ... --state merged` call with "[]", since
# that's the sibling #5657 check's shape - never expected to be reached in
# these scenarios, but harmless if it is). loom-daemon's `forge` passthrough
# shells out to `gh` on PATH too, so this intercepts both call shapes.
#   FAKE_GH_MODE=cross_repo  -> one open PR, head on a FORK
#   FAKE_GH_MODE=same_repo   -> one open PR, head in this same repo
#   FAKE_GH_MODE=none        -> no open PR matches (confirmed clean)
#   FAKE_GH_MODE=no_host     -> gh's own "no known GitHub host" failure
#                               (origin isn't a real forge remote)
#   FAKE_GH_MODE=unauth      -> gh's UNAUTHENTICATED bail-out (exit 4), which
#                               happens before it ever inspects the remotes,
#                               so its text carries no "known GitHub host"
#                               signal at all — the #7863 CI shape
#   FAKE_GH_MODE=unavailable -> a genuine forge failure (e.g. rate limit)
install_fake_gh() {
    local pr_number="${1:-999}"
    local fake_bin
    fake_bin="$(mktemp -d /tmp/loom-wtforge-bin.XXXXXX)"
    cat > "$fake_bin/gh" << EOF
#!/bin/bash
if [[ "\$1" == "pr" && "\$2" == "list" ]]; then
    shift 2
    state="" branch=""
    while [[ \$# -gt 0 ]]; do
        case "\$1" in
            --state) state="\$2"; shift 2 ;;
            --head)  branch="\$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    if [[ "\$state" == "merged" ]]; then
        echo "[]"
        exit 0
    fi
    case "\${FAKE_GH_MODE:-none}" in
        cross_repo)
            echo '[{"number": $pr_number, "isCrossRepository": true, "headRepository": {"nameWithOwner": "forkuser/loom"}, "headRefName": "'"\$branch"'", "url": "https://github.com/rjwalters/loom/pull/$pr_number"}]'
            ;;
        same_repo)
            echo '[{"number": $pr_number, "isCrossRepository": false, "headRepository": {"nameWithOwner": "rjwalters/loom"}, "headRefName": "'"\$branch"'", "url": "https://github.com/rjwalters/loom/pull/$pr_number"}]'
            ;;
        none)
            echo "[]"
            ;;
        no_host)
            echo "none of the git remotes configured for this repository point to a known GitHub host. To tell gh about a new GitHub host, please use \`gh auth login\`" >&2
            exit 1
            ;;
        unauth)
            echo "To get started with GitHub CLI, please run:  gh auth login" >&2
            echo "Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token." >&2
            exit 4
            ;;
        unavailable)
            echo "gh: API rate limit exceeded for this token" >&2
            exit 1
            ;;
    esac
    exit 0
fi
echo "fake gh: unsupported invocation: \$*" >&2
exit 1
EOF
    chmod +x "$fake_bin/gh"
    echo "$fake_bin"
}

# --- Test 1: cross-repo open PR -> refuse, never silently fresh ---
echo "Test 1: open PR's head is on a FORK -> worktree.sh refuses, does not create a shadowing fresh branch"
REPO=$(setup_repo crossrepo1)
FAKE_BIN=$(install_fake_gh 1234)
OUT_LOG="/tmp/wtforge-cross.$$"
RC=0
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=cross_repo ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1
) || RC=$?
if [[ "$RC" -ne 0 ]]; then
    pass "worktree.sh exits non-zero when the branch name is already an open cross-repo PR's head"
else
    fail "worktree.sh exited 0 despite a cross-repo PR already claiming this branch name"
fi
if [[ ! -d "$REPO/.loom/worktrees/issue-77" ]]; then
    pass "no worktree was created (no silent fresh main-HEAD branch)"
else
    fail "a worktree was created despite the cross-repo PR conflict"
fi
if grep -qi "1234" "$OUT_LOG" && grep -qi "forkuser/loom" "$OUT_LOG"; then
    pass "output names the conflicting PR number and its fork repo"
else
    fail "output does not name the conflicting PR/fork (see $OUT_LOG)"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -rf "$FAKE_BIN"
rm -f "$OUT_LOG"

# --- Test 2: same-repo open PR, ref not yet fetched -> fetch + reuse ---
echo ""
echo "Test 2: open PR's head is in THIS repo but unfetched -> worktree.sh fetches refs/pull/<n>/head and reuses it"
REPO=$(setup_repo samerepo1 true 999)
FAKE_BIN=$(install_fake_gh 999)
OUT_LOG="/tmp/wtforge-same.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=same_repo ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -f "$REPO/.loom/worktrees/issue-77/pr-artifact.txt" ]]; then
    pass "worktree contains the open PR's artifact (fetched refs/pull/999/head and reused it, not a fresh branch)"
else
    fail "worktree is missing the open PR's artifact — fell through to a fresh branch instead of reusing"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -rf "$FAKE_BIN"
rm -f "$OUT_LOG"

# --- Test 3: forge query genuinely unavailable -> refuse ---
echo ""
echo "Test 3: forge query fails (not just 'no GitHub remote') -> worktree.sh refuses rather than guessing 'safe'"
REPO=$(setup_repo unavailable1)
FAKE_BIN=$(install_fake_gh)
OUT_LOG="/tmp/wtforge-unavail.$$"
RC=0
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=unavailable ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1
) || RC=$?
if [[ "$RC" -ne 0 ]]; then
    pass "worktree.sh exits non-zero when the forge query itself fails"
else
    fail "worktree.sh exited 0 despite being unable to verify via the forge"
fi
if [[ ! -d "$REPO/.loom/worktrees/issue-77" ]]; then
    pass "no worktree was created (did not proceed blind)"
else
    fail "a worktree was created despite the forge query failing"
fi
cleanup_repo "$REPO"
rm -rf "$FAKE_BIN"
rm -f "$OUT_LOG"

# --- Test 4: gh says no remote points at a known forge host -> proceed ---
echo ""
echo "Test 4: gh reports no remote points at a known GitHub host -> still creates the fresh branch"
REPO=$(setup_repo nohost1)
FAKE_BIN=$(install_fake_gh)
OUT_LOG="/tmp/wtforge-nohost.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=no_host ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -d "$REPO/.loom/worktrees/issue-77" ]]; then
    pass "worktree was created from base (no forge relationship -> nothing to shadow)"
else
    fail "worktree.sh refused even though there is no forge to check against"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -rf "$FAKE_BIN"
rm -f "$OUT_LOG"

# --- Test 5: forge confirms no open PR matches -> proceed as before ---
echo ""
echo "Test 5: forge reachable, confirms no open PR claims this branch -> still creates the fresh branch"
REPO=$(setup_repo none1)
FAKE_BIN=$(install_fake_gh)
OUT_LOG="/tmp/wtforge-none.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=none ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -d "$REPO/.loom/worktrees/issue-77" ]]; then
    pass "worktree was created from base (forge confirmed no conflicting PR)"
else
    fail "worktree.sh refused even though the forge confirmed no open PR matches"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -rf "$FAKE_BIN"
rm -f "$OUT_LOG"

# --- Test 6: local-filesystem-only clone + UNAUTHENTICATED gh -> proceed ---
#
# The #7863 regression, verbatim: a repo whose only remote is a local bare
# path (every hermetic suite in defaults/scripts/tests/ builds one this way)
# on a runner with no GH_TOKEN. gh bails out before it ever looks at the
# remotes, so its stderr carries none of the "no known GitHub host" signal
# Test 4 relies on — and the old code misfiled that as `unavailable` and
# refused to create ANY worktree.
echo ""
echo "Test 6: local-filesystem-only origin + unauthenticated gh -> still creates the fresh branch (#7863)"
REPO=$(setup_repo localonly1 false "" false)
FAKE_BIN=$(install_fake_gh)
OUT_LOG="/tmp/wtforge-unauth.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=unauth ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -d "$REPO/.loom/worktrees/issue-77" ]]; then
    pass "worktree was created (no forge remote -> nothing to shadow, even though gh could not answer)"
else
    fail "worktree.sh refused on a local-only clone because gh was unauthenticated (#7863 regression)"
    cat "$OUT_LOG"
fi
if ! grep -qi "refusing to create a same-named branch" "$OUT_LOG"; then
    pass "no 'could not verify via the forge' refusal was emitted"
else
    fail "emitted a forge-unavailable refusal for a repo that has no forge remote"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -rf "$FAKE_BIN"
rm -f "$OUT_LOG"

# --- Test 7: local-filesystem-only clone -> the forge is not consulted ---
#
# Guards the short-circuit itself: with no forge remote there is no PR that
# could shadow this branch, so the query is skipped entirely rather than
# asked and then reinterpreted. Uses the mode that would otherwise REFUSE
# (cross-repo PR found), so a regression that reintroduces the forge call here
# fails loudly instead of silently costing a round-trip.
echo ""
echo "Test 7: local-filesystem-only origin -> forge is never consulted at all"
REPO=$(setup_repo localonly2 false "" false)
FAKE_BIN=$(install_fake_gh 4242)
OUT_LOG="/tmp/wtforge-skip.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=cross_repo ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -d "$REPO/.loom/worktrees/issue-77" ]] && ! grep -q "4242" "$OUT_LOG"; then
    pass "worktree was created without consulting the forge (no remote could be one)"
else
    fail "the forge was consulted (or creation refused) for a repo with no forge remote"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -rf "$FAKE_BIN"
rm -f "$OUT_LOG"

# --- Test 8: loom-daemon forge declines the forge (Gitea) -> proceed ---
# `loom-daemon forge pr list` exits 3 (EX_FORGE_DECLINED) with "gitea is not
# handled natively" on a Gitea remote. The helper prefers loom-daemon over gh
# when both are on PATH, so before this case that decline was misfiled as
# `unavailable` and every fresh-branch worktree on a Gitea repo was refused.
echo ""
echo "Test 8: loom-daemon forge declines (gitea not handled natively) -> still creates the fresh branch"
REPO=$(setup_repo gitea1)
FAKE_BIN=$(install_fake_gh)
cat > "$FAKE_BIN/loom-daemon" << 'EOF'
#!/bin/bash
if [[ "$1" == "forge" && "$2" == "pr" && "$3" == "list" ]]; then
    echo "loom-daemon forge: gitea is not handled natively; falling back to the caller's shell path" >&2
    exit 3
fi
if [[ "$1" == "worktree-lock" && "$2" == "check-issue" ]]; then
    # No claim lock in this fixture -- exit 0 (free), same as a real daemon
    # with no .loom/locks/issue-<N>/owner.json (#8553).
    exit 0
fi
echo "fake loom-daemon: unsupported invocation: $*" >&2
exit 1
EOF
chmod +x "$FAKE_BIN/loom-daemon"
OUT_LOG="/tmp/wtforge-gitea.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=unavailable ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -d "$REPO/.loom/worktrees/issue-77" ]]; then
    pass "worktree was created from base (forge declined by loom-daemon -> nothing this check can consult)"
else
    fail "worktree.sh refused after loom-daemon forge declined a non-GitHub forge"
    cat "$OUT_LOG"
fi
if grep -q "forge-check-unavailable\|refusing to create a same-named branch" "$OUT_LOG"; then
    fail "emitted a forge-unavailable refusal for a forge loom-daemon declined to handle"
else
    pass "no forge-unavailable refusal was emitted"
fi
cleanup_repo "$REPO"
rm -rf "$FAKE_BIN"
rm -f "$OUT_LOG"

# --- Test 9: the fd-3 refusals stay parseable for adversarial forge values ---
#
# #9109: every refusal above used to be a quoted JSON literal with the forge's
# own `headRefName` / `headRepository` concatenated into it. A `"` is legal in
# neither of those on GitHub today, but nothing in this code path enforced that
# — and the failure mode is the worst one available: UNPARSEABLE JSON on the
# exact channel a consumer reads to learn that a refusal happened at all.
#
# Driving worktree.sh end-to-end cannot reach this: it derives the branch name
# from the issue number, so the adversarial value has to be injected at the
# forge-lookup boundary. These call the decision function directly with
# `_worktree_open_pr_for_branch` stubbed, capture fd 3, and require `jq -e .`
# to accept the result AND the values to round-trip byte-identically.
echo ""
echo "Test 9: fd-3 refusal JSON survives adversarial forge values (#9109)"

ADV_REF='evil"ref\with<>chars'
ADV_REPO='fork"user/loom'
# Run outside any git repo: the same-repo arm shells out to git, and we only
# care about the JSON it emits when that cannot resolve the ref.
JSON_TMP=$(mktemp -d /tmp/loom-wtforge-json.XXXXXX)

# Driven entirely by STUB_* env vars: STUB_STATUS / STUB_CROSS / STUB_PR /
# STUB_FETCH. Echoes ONLY what the decision function wrote to fd 3.
emit_refusal() {
    (
        cd "$JSON_TMP" || exit 1
        # shellcheck source=../lib/worktree-forge-pr-check.sh
        source "$SCRIPTS_DIR/lib/worktree-forge-pr-check.sh"
        _worktree_open_pr_for_branch() {
            _WT_OPEN_PR_STATUS="$STUB_STATUS"
            _WT_OPEN_PR_IS_CROSS_REPO="$STUB_CROSS"
            _WT_OPEN_PR_NUMBER="$STUB_PR"
            _WT_OPEN_PR_HEAD_REPO="$ADV_REPO"
            _WT_OPEN_PR_HEAD_REF="$ADV_REF"
            _WT_OPEN_PR_URL=""
        }
        _worktree_guard_fresh_branch_against_open_pr \
            "feature/issue-77" "77" "true" "main" "origin/main" "$STUB_FETCH" \
            3>&1 >/dev/null 2>/dev/null
    )
}

assert_json() {
    local got
    got=$(printf '%s' "$2" | jq -r "$3" 2>/dev/null || true)
    if [[ "$got" == "$4" ]]; then pass "$1"; else fail "$1 (got '$got', want '$4')"; fi
}

CROSS_JSON=$(STUB_STATUS=found STUB_CROSS=true STUB_PR=1234 STUB_FETCH=ok emit_refusal || true)
if printf '%s' "$CROSS_JSON" | jq -e . >/dev/null 2>&1; then
    pass "shadowed-cross-repo-pr refusal is valid JSON with a headRefName containing \" \\ < >"
else
    fail "shadowed-cross-repo-pr refusal is NOT valid JSON: $CROSS_JSON"
fi
assert_json "headRef round-trips byte-identically" "$CROSS_JSON" '.headRef' "$ADV_REF"
assert_json "headRepo round-trips byte-identically" "$CROSS_JSON" '.headRepo' "$ADV_REPO"
assert_json "prNumber stays a JSON number" "$CROSS_JSON" '.prNumber|tostring + ":" + type' "1234:number"
assert_json "issueNumber stays a JSON number" "$CROSS_JSON" '.issueNumber|tostring + ":" + type' "77:number"
assert_json "error code is unchanged" "$CROSS_JSON" '.error' "shadowed-cross-repo-pr"

# Same-repo arm, with an EMPTY PR number — the empty/null case the old literal
# form tolerated via ${_WT_OPEN_PR_NUMBER:-null}. It must still degrade to
# JSON null rather than abort the emitter.
SAME_JSON=$(STUB_STATUS=found STUB_CROSS=false STUB_PR="" STUB_FETCH=ok emit_refusal || true)
if printf '%s' "$SAME_JSON" | jq -e . >/dev/null 2>&1; then
    pass "open-pr-ref-fetch-failed refusal is valid JSON with an empty PR number"
else
    fail "open-pr-ref-fetch-failed refusal is NOT valid JSON: $SAME_JSON"
fi
assert_json "empty PR number degrades to JSON null" "$SAME_JSON" '.prNumber|type' "null"

# forge-check-unavailable arm carries origin_fetch_result, which is the one
# other non-numeric value this file interpolates.
UNAVAIL_JSON=$(STUB_STATUS=unavailable STUB_CROSS=false STUB_PR="" STUB_FETCH='fetch-failed"; rm -rf /' emit_refusal || true)
if printf '%s' "$UNAVAIL_JSON" | jq -e . >/dev/null 2>&1; then
    pass "forge-check-unavailable refusal is valid JSON with a quote-bearing originFetch"
else
    fail "forge-check-unavailable refusal is NOT valid JSON: $UNAVAIL_JSON"
fi
assert_json "originFetch round-trips byte-identically" "$UNAVAIL_JSON" '.originFetch' 'fetch-failed"; rm -rf /'
rm -rf "$JSON_TMP"

# --- Test 10: grep-auditable — no literal-JSON fd-3 emitter remains ---
echo ""
echo "Test 10: every >&3 emitter builds its JSON with jq, not string concatenation (#9109)"
FORGE_LIB="$SCRIPTS_DIR/lib/worktree-forge-pr-check.sh"
# Every >&3 CODE line (comments excluded — the invariant is documented in the
# file header, which naturally mentions `>&3`).
EMITTERS=$(grep -n '>&3' "$FORGE_LIB" | grep -vE '^[0-9]+:[[:space:]]*#' || true)
# The splice signature: a shell quote immediately followed by the opposite
# quote is how a variable gets concatenated into a quoted JSON literal
# (`'{"headRef": "'"$VAR"'"}'`). jq-built emitters never have adjacent
# opposite quotes.
LITERAL_EMITTERS=$(printf '%s\n' "$EMITTERS" | grep -E "'\"|\"'" || true)
if [[ -z "$LITERAL_EMITTERS" ]]; then
    pass "no >&3 line concatenates a value into a quoted JSON literal"
else
    fail "literal-JSON >&3 emitter(s) remain:"$'\n'"$LITERAL_EMITTERS"
fi
UNVETTED=$(printf '%s\n' "$EMITTERS" | grep -vE '^$|jq -cn|worktree-closed-pr-branch' || true)
if [[ -z "$UNVETTED" ]]; then
    pass "every >&3 emitter is either 'jq -cn' or the loom-daemon passthrough"
else
    fail "unvetted >&3 emitter(s):"$'\n'"$UNVETTED"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
