#!/usr/bin/env bash
# test-champion-pr-merge-hold-release.sh - #11416: a human removing
# `loom:blocked` releases a Champion merge-risk hold, and a hold is never left
# as plain `loom:pr`, unmerged and invisible.
#
# Extract-and-execute (#7979), not a mirror: the SHIPPED bash blocks are lifted
# out of champion-pr-merge.md and run against canned forge JSON with `gh`,
# `loom-daemon`, `$GH_READ` and `post-comment.sh` stubbed. Blocks run:
#   1. criterion #2's sticky-hold precheck ("Run this precheck before judging
#      the axes") -> MERGE_BLOCKED_BY_HOLD / RELEASE_REASON /
#      MANUAL_RELEASE_SINCE_HOLD / label repair;
#   2. "Hold behavior" -> same-concern PASS vs. fresh hold notice + label;
#   3. the Held-PR Census `RELEASED` line.
# The four risk axes are an LLM judgment with no executable form, so the
# narrowed criterion-2 rule and its kept set are pinned as doc literals
# (Test 1); the precheck itself is shown never to hold an unheld PR.
#
# Hermetic: no network, no forge, no tokens.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-pr-merge-hold-release.sh

# SC2034: PR_NUMBER / GH_READ / CONCERN_BULLET are read by the eval'd shipped
# blocks. SC2043: the one-pass loop gives the precheck's `continue` a loop.
# shellcheck disable=SC2034,SC2043

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
# Installed (.loom/scripts -> ../../.claude) vs. source (defaults/scripts ->
# ../.claude) layouts differ in depth; probe both (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$1', got '$2')"; fi
}
assert_contains() {
    if grep -qF -- "$2" <<<"$1"; then pass "$3"; else fail "$3 (missing '$2' in: $1)"; fi
}
assert_lacks() {
    if grep -qF -- "$2" <<<"$1"; then fail "$3 (unexpected '$2' in: $1)"; else pass "$3"; fi
}
assert_doc_contains() {
    if grep -qF -- "$1" "$CHAMPION_MD"; then pass "$2"; else fail "$2 (missing literal: $1)"; fi
}

# Print the first ```bash block that follows the first line containing $1.
extract_block() {
    awk -v m="$1" '
        f == 0 && index($0, m) { f = 1; next }
        f == 1 && /^```bash$/  { f = 2; next }
        f == 2 && /^```$/      { exit }
        f == 2                 { print }
    ' "$CHAMPION_MD" | grep -v '^PR_NUMBER=<number>$'
}

PRECHECK="$(extract_block '**Run this precheck before judging the axes**')"
HOLD_BEHAVIOR="$(extract_block '**Hold behavior** — name the')"
CENSUS_RELEASED="$(grep -A3 -F 'RELEASED=$("$GH_READ" pr list' "$CHAMPION_MD")"

WORK="$(mktemp -d)" && trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/.loom/scripts"
printf '%s\n' '#!/usr/bin/env bash' 'echo "POST $*" >>"$STUB_LOG"' >"$WORK/.loom/scripts/post-comment.sh"
chmod +x "$WORK/.loom/scripts/post-comment.sh"

# ---- forge stubs ------------------------------------------------------------
# FIX_PR       gh pr view --json commits,labels,headRefOid payload
# FIX_COMMENTS loom-daemon forge trusted-comments --gh-shape payload
# FIX_EVENTS   issues/N/events payload; FIX_EVENTS_FAIL=1 makes the read fail
# FIX_LIST     gh pr list payload for the census
gh() {
    case "$1 $2" in
        "pr view") printf '%s\n' "$FIX_PR" ;;
        "pr edit") echo "EDIT $*" >>"$STUB_LOG" ;;
        "api repos/{owner}/{repo}/issues/"*)
            [[ "${FIX_EVENTS_FAIL:-0}" == 1 ]] && return 1
            local filter="" a
            local -a args=("$@")
            for ((a = 0; a < ${#args[@]}; a++)); do
                [[ "${args[a]}" == "--jq" ]] && filter="${args[a + 1]}"
            done
            jq -r "$filter" <<<"$FIX_EVENTS"
            ;;
        *) echo "unexpected gh call: $*" >&2; return 99 ;;
    esac
}
loom-daemon() {
    # #11861: the hold label is loom:blocked, applied with its park record.
    [[ "$1 $2" == "park-record apply" ]] && { echo "PARK $*" >>"$STUB_LOG"; return 0; }
    [[ "$1 $2 $3" == "forge trusted-comments --fetch" ]] || { echo "unexpected loom-daemon: $*" >&2; return 99; }
    printf '%s\n' "$FIX_COMMENTS"
}
gh_read() {
    local filter="" a
    local -a args=("$@")
    for ((a = 0; a < ${#args[@]}; a++)); do
        [[ "${args[a]}" == "--jq" ]] && filter="${args[a + 1]}"
    done
    case "$1 $2" in
        "pr view") jq -r "$filter" <<<"{\"comments\": $FIX_COMMENTS}" ;;
        "pr list") jq -r "$filter" <<<"$FIX_LIST" ;;
        *) echo "unexpected GH_READ call: $*" >&2; return 99 ;;
    esac
}
export -f gh loom-daemon gh_read

HOLD_AT="2026-10-09T08:53:41Z"
HOLD_HEAD="d4ea576427e62a710459ab601250e462993c19b7"
BULLET='- **Blast radius / reversibility**: rewrites claim-liveness in `review_claim.rs`.'
HOLD_COMMENT=$(jq -n --arg b "<!-- champion:merge-risk-hold -->
<!-- champion:hold-state head=$HOLD_HEAD -->
**Champion: Holding for Human Merge**

$BULLET" --arg at "$HOLD_AT" '{author: {login: "loom-fleet-dispatch"}, body: $b, createdAt: $at}')

pr_json() { # <head> <labels...>
    local head="$1"; shift
    jq -n --arg h "$head" --args '{headRefOid: $h, commits: [{committedDate: "2026-10-08T00:00:00Z"}], labels: [$ARGS.positional[] | {name: .}]}' "$@"
}
unlabeled_event() { # <created_at> [actor-login] [actor-type]
    jq -n --arg at "$1" --arg who "${2:-rjwalters}" --arg type "${3:-User}" '[{event: "labeled", label: {name: "loom:pr"}, actor: {login: "judge"}, created_at: "2026-10-08T00:00:00Z"},
                          {event: "unlabeled", label: {name: ($ENV.UNLABELED // "loom:blocked")}, actor: {login: $who, type: $type}, created_at: $at}]'
}

# Run the extracted precheck once; print the decision variables and the stub log.
run_precheck() {
    (
        cd "$WORK" || exit 1
        export STUB_LOG="$WORK/log"; : >"$STUB_LOG"
        PR_NUMBER=10280
        for _once in 1; do eval "$PRECHECK" >/dev/null 2>&1; done
        echo "BLOCKED=$MERGE_BLOCKED_BY_HOLD PRIOR=$PRIOR_HOLD MANUAL=${MANUAL_RELEASE_SINCE_HOLD:-} REASON=${RELEASE_REASON:-}"
        cat "$STUB_LOG"
    )
}

# Run the precheck, then "Hold behavior" with $1 as this tick's concern bullet.
run_hold_behavior() {
    local concern="$1"
    (
        cd "$WORK" || exit 1
        export STUB_LOG="$WORK/log"; : >"$STUB_LOG"
        PR_NUMBER=10280
        GH_READ=gh_read
        for _once in 1; do eval "$PRECHECK" >/dev/null 2>&1; done
        MERGE_BLOCKED_BY_HOLD=false   # axes judged red this tick -> Hold behavior runs
        local block
        block=$(grep -v '^CONCERN_BULLET=' <<<"$HOLD_BEHAVIOR")
        CONCERN_BULLET="$concern"
        eval "$block" >/dev/null 2>&1
        echo "BLOCKED=$MERGE_BLOCKED_BY_HOLD"
        cat "$STUB_LOG"
    )
}

echo "=== test-champion-pr-merge-hold-release.sh ==="
echo

echo "Test 0: the shipped blocks are extractable (this suite RUNS them, it does not mirror them)"
assert_contains "$PRECHECK" 'MERGE_BLOCKED_BY_HOLD=false' "precheck block extracted"
assert_contains "$HOLD_BEHAVIOR" 'CONCERN_BULLET=' "Hold behavior block extracted"
assert_contains "$CENSUS_RELEASED" 'Released holds, not yet merged' "census RELEASED line extracted"
echo

echo "Test 1: criterion #2 narrowed — claim/label machinery is not red by itself; the kept set is listed (#11416)"
assert_doc_contains '**Claim/label machinery is not red by itself (#11416).**' "the narrowed rule is stated"
assert_doc_contains '"Reverting cannot undo forge labels already changed" or "a defect could strand work fleet-wide" is **not** a red reason.' \
    "the reasoning behind the #10280/#11321 holds is named and rejected"
assert_doc_contains '1. **Irreversible migration, no rollback step**' "kept set item 1: irreversible migrations"
assert_doc_contains '2. **The merge gate itself**' "kept set item 2: the merge gate"
assert_doc_contains 'Writing a verdict label (the Judge'"'"'s `loom:pr` approval) is part of the gate, not a role hand-back' \
    "kept set item 2: verdict-label writes are the gate, not a hand-back"
for producer in post-verdict.sh verdict-staleness-guard.sh reconcile_pr_verdicts comment_trust.rs 'forge trusted-comments' check-review-feedback.sh; do
    assert_doc_contains "\`$producer\`" "kept set item 2 names gate-input producer $producer"
done
assert_doc_contains '3. **The other red entries in the table above**, unchanged.' "kept set item 3: existing red entries"
echo

echo "Test 2: never held -> the precheck does not block (a claim-machinery PR is judged on the axes alone)"
export FIX_PR FIX_COMMENTS FIX_EVENTS FIX_EVENTS_FAIL FIX_LIST
FIX_PR=$(pr_json "$HOLD_HEAD" loom:pr); FIX_COMMENTS='[]'; FIX_EVENTS='[]'; FIX_EVENTS_FAIL=0
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=false PRIOR=false" "no prior hold, nothing blocks"
echo

echo "Test 3: REGRESSION — hold + loom:blocked present + head unchanged -> still blocked"
FIX_PR=$(pr_json "$HOLD_HEAD" loom:pr loom:blocked); FIX_COMMENTS="[$HOLD_COMMENT]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true MANUAL=false REASON=" "sticky hold binds"
assert_lacks "$OUT" "EDIT" "no label write on a correctly-labelled sticky hold"
echo

echo "Test 4: THE #10280 SHAPE — hold + loom:blocked removed by a human + head unchanged -> released (e)"
FIX_PR=$(pr_json "$HOLD_HEAD" loom:pr); FIX_EVENTS=$(unlabeled_event "2026-10-10T12:00:00Z")
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=false" "the hold no longer blocks the merge"
assert_contains "$OUT" "MANUAL=true" "MANUAL_RELEASE_SINCE_HOLD is set"
assert_contains "$OUT" 'REASON=the hold label removed by rjwalters at 2026-10-10T12:00:00Z' "RELEASE_REASON names who removed it and when"
assert_lacks "$OUT" "EDIT" "loom:blocked is not put back"
echo

echo "Test 5: label absent but the only removal PREDATES the hold -> not a release; label repaired"
FIX_EVENTS=$(unlabeled_event "2026-10-01T00:00:00Z")
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true" "still held"
assert_contains "$OUT" "PARK park-record apply --pr 10280 --reason champion merge-risk hold --by champion" "loom:blocked restored, so the census counts it"
echo

echo "Test 6: label absent, no unlabeled event at all (failed --add-label on the hold tick) -> repaired, not released"
FIX_EVENTS='[]'
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true" "a missing label alone is never a release"
assert_contains "$OUT" "PARK park-record apply --pr 10280 --reason champion merge-risk hold --by champion" "label repaired"
echo

echo "Test 7: label absent, events read FAILS -> held this pass, and no label write over a possible human removal"
FIX_EVENTS_FAIL=1
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true" "unread -> conservative hold"
assert_lacks "$OUT" "EDIT" "no repair on an unread event list"
FIX_EVENTS_FAIL=0
echo

echo "Test 7b: label removed after the hold by a BOT (Champion's own Step 2 / Hold Cleared write) -> not a release; repaired"
for bot in 'loom-fleet-dispatch[bot] Bot' 'loom-fleet-dispatch[bot] User' 'loom-app Bot'; do
    read -r login type <<<"$bot"
    FIX_EVENTS=$(unlabeled_event "2026-10-10T12:00:00Z" "$login" "$type")
    OUT=$(run_precheck)
    assert_contains "$OUT" "BLOCKED=true PRIOR=true MANUAL=false" "removal by $login ($type) does not release the hold"
    assert_contains "$OUT" "PARK park-record apply --pr 10280 --reason champion merge-risk hold --by champion" "label repaired after $login ($type) removal"
done
echo

echo "Test 7c: removal with unidentified actor (null actor, missing/empty login or type) -> not a release; repaired"
for actor in 'null' '{"type":"User"}' '{"login":"","type":"User"}' '{"login":"rjwalters"}'; do
    FIX_EVENTS=$(jq -n --argjson a "$actor" '[{event: "labeled", label: {name: "loom:pr"}, actor: {login: "judge"}, created_at: "2026-10-08T00:00:00Z"},
                          {event: "unlabeled", label: {name: ($ENV.UNLABELED // "loom:blocked")}, actor: $a, created_at: "2026-10-10T12:00:00Z"}]')
    OUT=$(run_precheck)
    assert_contains "$OUT" "BLOCKED=true PRIOR=true MANUAL=false" "unidentified actor $actor does not release the hold"
    assert_contains "$OUT" "PARK park-record apply --pr 10280 --reason champion merge-risk hold --by champion" "label repaired for actor $actor"
done
echo

echo "Test 7b: a human removal of the pre-#11861 loom:operator still releases (#11861)"
FIX_PR=$(pr_json "$HOLD_HEAD" loom:pr); FIX_COMMENTS="[$HOLD_COMMENT]"
FIX_EVENTS=$(UNLABELED=loom:operator unlabeled_event "2026-10-10T12:00:00Z")
OUT=$(run_precheck)
assert_contains "$OUT" "MANUAL=true" "a legacy loom:operator removal is release (e)"
echo

echo "Test 8: head moved AND label removed -> (c) is the reason, MANUAL still set for the outcome rule"
FIX_PR=$(pr_json "abcdef1234567890" loom:pr); FIX_EVENTS=$(unlabeled_event "2026-10-10T12:00:00Z")
OUT=$(run_precheck)
assert_contains "$OUT" "MANUAL=true REASON=new head commit abcdef1" "the earlier signal keeps precedence for RELEASE_REASON"
echo

echo "Test 9: Hold behavior — human release + SAME concern -> PASS, no notice, no label"
FIX_PR=$(pr_json "$HOLD_HEAD" loom:pr)
OUT=$(run_hold_behavior "$BULLET")
assert_contains "$OUT" "BLOCKED=false" "not blocked: merges via Step 2 with the reversal block"
assert_lacks "$OUT" "POST" "no hold notice"
assert_lacks "$OUT" "EDIT" "loom:blocked not reasserted"
echo

echo "Test 10: Hold behavior — human release + NEW concern -> fresh notice despite the existing marker, label back"
OUT=$(run_hold_behavior '- **Kept set (merge gate)**: edits `merge-pr.sh` verdict guard.')
assert_contains "$OUT" "BLOCKED=true" "held"
assert_contains "$OUT" "POST 10280 --pr --body" "a fresh hold notice is posted, so the next removal releases THIS concern"
assert_contains "$OUT" "PARK park-record apply --pr 10280 --reason champion merge-risk hold --by champion" "loom:blocked reapplied"
echo

echo "Test 11: Hold behavior — no human release, marker on record -> notice suppressed, label reasserted"
FIX_PR=$(pr_json "abcdef1234567890" loom:pr loom:blocked)
OUT=$(run_hold_behavior "$BULLET")
assert_contains "$OUT" "BLOCKED=true" "held"
assert_lacks "$OUT" "POST" "idempotency guard suppresses the repeat notice"
assert_contains "$OUT" "PARK park-record apply --pr 10280 --reason champion merge-risk hold --by champion" "label reasserted"
echo

echo "Test 12: census lists hand-released, unmerged holds (merge-risk and critical-file), not quoted markers"
FIX_LIST=$(jq -n '[
  {number: 10280, comments: [{body: "<!-- champion:merge-risk-hold -->\nheld"}]},
  {number: 9001,  comments: [{body: "<!-- champion:critical-file-hold -->\nheld"}]},
  {number: 9002,  comments: [{body: "LGTM"}]},
  {number: 9003,  comments: [{body: "quoting <!-- champion:merge-risk-hold --> in prose"}]},
  {number: 9004,  comments: [{body: "<!-- champion:critical-file-hold -->\nheld"}, {body: "<!-- champion:critical-file-hold-cleared -->\nnarrowed"}]}
]')
OUT=$(
    GH_READ=gh_read
    eval "$CENSUS_RELEASED"
)
assert_eq "Released holds, not yet merged: #10280 #9001" "$OUT" "exactly the marker-bearing PRs are listed (auto-cleared critical-file hold #9004 excluded)"
FIX_LIST='[]'
OUT=$(
    GH_READ=gh_read
    eval "$CENSUS_RELEASED"
)
assert_eq "Released holds, not yet merged: none" "$OUT" "an empty set still reports a line"
echo

echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
