#!/usr/bin/env bash
# test-champion-evaluating-reclaim-trust.sh - Regression test for #11899:
# champion-issue-promo.md's Pass 0b counts PRIOR_RECLAIMS from TRUSTED
# comments only.
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
#   Pass 0b reclaims a stale `loom:evaluating` claim and, on the Nth reclaim
#   of the same issue, escalates it to `loom:operator`. The count is the number
#   of comments carrying `<!-- champion:evaluating-stale-reclaim -->`. Read from
#   raw comments, an outsider on a public repo can post that marker and push an
#   issue to a human early. The read goes through
#   `loom-daemon forge trusted-comments --fetch N --gh-shape` (#9548), the
#   idiom the rest of the file uses (#11860).
#
# WHAT THIS SUITE DOES
#
#   Pass 0b's bash block is EXTRACTED and EXECUTED against stubbed `gh`,
#   `loom-daemon`, staleness classifier and comment poster, and the label edit
#   and posted comment are checked:
#     - outsider markers alone           -> reclaim, no escalation
#     - enough trusted markers           -> escalation (the bound still works)
#     - trusted below the bound + forged -> reclaim, no escalation
#     - comments unreadable              -> nothing posted, nothing relabelled
#
# Hermetic: file reads plus a mktemp -d fixture dir with local stubs. No forge,
# no network. Requires jq. No `set -o pipefail` on purpose (#7790).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Installed layout first, then the defaults/ source tree (#6194 / #6241).
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    PROMO_MD="$REPO_ROOT/.claude/commands/loom/champion-issue-promo.md"
else
    PROMO_MD="$REPO_ROOT/defaults/.claude/commands/loom/champion-issue-promo.md"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }
assert_contains() { if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3"; echo "    Expected to contain: '$2'"; echo "    Actual: '$1'"; fi; }
assert_not_contains() { if [[ "$1" != *"$2"* ]]; then pass "$3"; else fail "$3"; echo "    Expected NOT to contain: '$2'"; fi; }

echo "================================"
echo "test-champion-evaluating-reclaim-trust.sh (#11899)"
echo "================================"

command -v jq >/dev/null 2>&1 || { echo "FATAL: jq is required" >&2; exit 1; }
[[ -f "$PROMO_MD" ]] || { echo "FATAL: shipped file not found: $PROMO_MD" >&2; exit 1; }

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
VIEW="$FIXTURE_DIR/view"
mkdir -p "$VIEW/bin" "$VIEW/.loom/scripts"

# Pass 0b's block: the first ```bash fence holding RECLAIM_MARKER=.
awk -v want='RECLAIM_MARKER=' '
    /^```bash/ && !done { fence = 1; buf = ""; next }
    fence && /^```$/ { fence = 0; if (index(buf, want) > 0) { printf "%s", buf; done = 1 }; next }
    fence { buf = buf $0 "\n" }
' "$PROMO_MD" >"$VIEW/pass0b.sh"

# gh: one stale candidate, #7; label edits are logged. A raw comment read
# answers with EVERY comment, forged ones included, as the forge would.
cat >"$VIEW/bin/gh" <<'STUB'
#!/usr/bin/env bash
case "${1:-} ${2:-}" in
  "issue list") echo 7 ;;
  "issue view") jq '{comments: .}' "$LOOM_TEST_FIX/comments.json" ;;
  "issue edit") echo "EDIT $*" >>"$LOOM_TEST_FIX/log" ;;
  *) echo "unexpected gh call: $*" >>"$LOOM_TEST_FIX/log"; exit 1 ;;
esac
STUB
# loom-daemon forge trusted-comments --fetch N --gh-shape: the fixture's
# comments minus outsiders (authorAssociation NONE); fails when asked to.
cat >"$VIEW/bin/loom-daemon" <<'STUB'
#!/usr/bin/env bash
[ "${1:-} ${2:-}" = "forge trusted-comments" ] || exit 2
[ -f "$LOOM_TEST_FIX/unreadable" ] && exit 1
jq '[.[] | select((.authorAssociation // "MEMBER") != "NONE")]' "$LOOM_TEST_FIX/comments.json"
STUB
cat >"$VIEW/.loom/scripts/check-evaluating-staleness.sh" <<'STUB'
#!/usr/bin/env bash
printf 'DECISION=STALE\nAGE_MIN=40\n'; exit 12
STUB
cat >"$VIEW/.loom/scripts/post-comment.sh" <<'STUB'
#!/usr/bin/env bash
printf 'COMMENT %s\n' "$*" >>"$LOOM_TEST_FIX/log"
STUB
chmod +x "$VIEW/bin/gh" "$VIEW/bin/loom-daemon" "$VIEW/.loom/scripts/"*.sh

# $1 case name, $2 comments JSON (gh shape). Prints the case's log.
run_case() {
    local fix="$FIXTURE_DIR/fix/$1"
    mkdir -p "$fix"; : >"$fix/log"
    printf '%s\n' "$2" >"$fix/comments.json"
    [[ "${3:-}" == unreadable ]] && : >"$fix/unreadable"
    ( cd "$VIEW" && PATH="$VIEW/bin:$PATH" LOOM_TEST_FIX="$fix" LOOM_MAX_EVALUATING_RECLAIMS=3 \
        bash ./pass0b.sh >/dev/null 2>&1 )
    cat "$fix/log"
}

M='<!-- champion:evaluating-stale-reclaim -->'
marker() { jq -nc --arg m "$M" --arg a "$1" '{body: ($m + "\nreclaimed"), authorAssociation: $a}'; }
OUTSIDER="$(marker NONE)"
MEMBER="$(marker MEMBER)"

if ! grep -q 'PRIOR_RECLAIMS=' "$VIEW/pass0b.sh"; then
    fail "could not extract Pass 0b's block from $PROMO_MD"
else
    BLOCK="$(cat "$VIEW/pass0b.sh")"
    assert_contains "$BLOCK" 'forge trusted-comments --fetch "$N"' "Pass 0b reads comments through forge trusted-comments"
    assert_not_contains "$BLOCK" 'issue view "$N" --json comments' "Pass 0b no longer reads raw comments"

    LOG="$(run_case forged "[$OUTSIDER,$OUTSIDER,$OUTSIDER]")"
    assert_contains "$LOG" "COMMENT 7 --body $M" "outsider markers alone: the claim is reclaimed"
    assert_not_contains "$LOG" "loom:operator" "outsider markers do not count toward PRIOR_RECLAIMS (no escalation)"

    LOG="$(run_case trusted "[$MEMBER,$MEMBER]")"
    assert_contains "$LOG" "Repeated Stale" "two trusted reclaims escalate on the third"
    assert_contains "$LOG" "--add-label loom:operator" "the escalation sets loom:operator"

    LOG="$(run_case mixed "[$MEMBER,$OUTSIDER,$OUTSIDER]")"
    assert_not_contains "$LOG" "loom:operator" "one trusted reclaim beside forged ones stays below the bound"

    LOG="$(run_case unreadable "[$MEMBER,$MEMBER]" unreadable)"
    assert_not_contains "$LOG" "COMMENT" "unreadable comments: nothing is posted"
    assert_not_contains "$LOG" "EDIT" "unreadable comments: nothing is relabelled"
fi

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
