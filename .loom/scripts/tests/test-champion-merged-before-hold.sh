#!/usr/bin/env bash
# test-champion-merged-before-hold.sh - #13534: Champion's "Hold behavior" re-reads
# the PR's live state before posting, so a PR that merged mid-assessment is never
# left with a hold notice or `loom:blocked`.
#
# Extract-and-execute: the SHIPPED "Hold behavior" bash block is lifted out of
# champion-pr-merge.md and run with `gh`, `loom-daemon`, `$GH_READ` and
# post-comment.sh stubbed. Hermetic: no network, no forge, no tokens.
#
# Usage: ./.loom/scripts/tests/test-champion-merged-before-hold.sh

# shellcheck disable=SC2034
set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"

source "$TEST_DIR/lib/assert.sh"
assert_contains() { if grep -qF -- "$2" <<<"$1"; then pass "$3"; else fail "$3 (missing '$2' in: $1)"; fi; }
assert_lacks() { if grep -qF -- "$2" <<<"$1"; then fail "$3 (unexpected '$2' in: $1)"; else pass "$3"; fi; }
count_of() { grep -cF -- "$2" <<<"$1"; }

HOLD_BEHAVIOR="$(awk -v m='**Hold behavior** — name the' '
    f == 0 && index($0, m) { f = 1; next }
    f == 1 && /^```bash$/  { f = 2; next }
    f == 2 && /^```$/      { exit }
    f == 2                 { print }' "$CHAMPION_MD" | grep -v -e '^PR_NUMBER=<number>$' -e '^CONCERN_BULLET=')"

WORK="$(mktemp -d)" && trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/.loom/scripts"
printf '%s\n' '#!/usr/bin/env bash' 'echo "POST $*" >>"$STUB_LOG"' >"$WORK/.loom/scripts/post-comment.sh"
chmod +x "$WORK/.loom/scripts/post-comment.sh"

# LIVE: what the live REST read returns ("" = read failure). NOTE_POSTED: marker already on PR.
gh() {
    case "$1 $2" in
        "api repos/{owner}/{repo}/pulls/"*)
            [[ -z "$LIVE" ]] && return 1
            echo "$LIVE" ;;
        "api -X") echo "DELETE $*" >>"$STUB_LOG" ;;
        *) echo "unexpected gh call: $*" >&2; return 99 ;;
    esac
}
loom-daemon() {
    [[ "$1 $2" == "park-record apply" ]] && { echo "PARK $*" >>"$STUB_LOG"; return 0; }
    [[ "$1 $2 $3" == "config get champion.standingAuthorizations" ]] && return 0
    echo "unexpected loom-daemon: $*" >&2; return 99
}
gh_read() { echo "${NOTE_POSTED:-false}"; }
export -f gh loom-daemon gh_read

run() {
    (
        cd "$WORK" || exit 1
        export STUB_LOG="$WORK/log" LIVE NOTE_POSTED; : >"$STUB_LOG"
        PR_NUMBER=555; GH_READ=gh_read
        PR_JSON='{"headRefOid":"abc1234","comments":[]}'
        HOLD_BODY=""; MANUAL_RELEASE_SINCE_HOLD=false
        CONCERN_BULLET='- **Blast radius**: rewrites the merge gate.'
        BLOCKER_NUMBERS=""
        eval "$HOLD_BEHAVIOR" >/dev/null 2>&1
        echo "BLOCKED=$MERGE_BLOCKED_BY_HOLD"
        cat "$STUB_LOG"
    )
}

echo "=== test-champion-merged-before-hold.sh ==="
assert_contains "$HOLD_BEHAVIOR" 'LIVE_STATE=' "live re-read is in the shipped Hold behavior block"

echo "Test 1: merged before the hold -> one note, no hold marker, no park record, no label write"
LIVE="true false"; NOTE_POSTED=false; OUT=$(run)
assert_contains "$OUT" "merged before hold could apply" "note posted"
assert_contains "$OUT" "rewrites the merge gate" "note quotes the concern bullet"
assert_lacks "$OUT" "champion:merge-risk-hold" "no hold marker"
assert_lacks "$OUT" "PARK" "no loom:blocked park record"
assert_lacks "$OUT" "DELETE" "nothing to remove"
[[ "$(count_of "$OUT" 'POST 555')" == 1 ]] && pass "exactly one comment" || fail "exactly one comment ($OUT)"

echo "Test 2: note is idempotent"
NOTE_POSTED=true; OUT=$(run)
assert_lacks "$OUT" "POST" "no second note"

echo "Test 3: hold already landed on the merged PR -> loom:blocked removed"
LIVE="true true"; NOTE_POSTED=false; OUT=$(run)
assert_contains "$OUT" "DELETE api -X DELETE repos/{owner}/{repo}/issues/555/labels/loom%3Ablocked" "loom:blocked removed"
assert_lacks "$OUT" "PARK" "no park record"

echo "Test 4: live read fails -> fail closed, nothing posted"
LIVE=""; OUT=$(run)
assert_contains "$OUT" "BLOCKED=true" "no merge this pass"
assert_lacks "$OUT" "POST" "no comment"
assert_lacks "$OUT" "DELETE" "no label write"
assert_lacks "$OUT" "PARK" "no park record"

echo "Test 5: still open -> unchanged hold behavior"
LIVE="false false"; NOTE_POSTED=false; OUT=$(run)
assert_contains "$OUT" "BLOCKED=true" "held"
assert_contains "$OUT" "POST 555 --pr --body" "hold notice posted"
assert_contains "$OUT" "champion:merge-risk-hold" "hold marker present"
assert_contains "$OUT" "PARK park-record apply --pr 555 --reason champion merge-risk hold --by champion" "loom:blocked applied"
assert_lacks "$OUT" "merged before hold could apply" "no merged note"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
