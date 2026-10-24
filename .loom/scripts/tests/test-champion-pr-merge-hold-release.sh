#!/usr/bin/env bash
# test-champion-pr-merge-hold-release.sh - #11416: a human removing
# `loom:blocked` releases a Champion merge-risk hold, and a hold is never left
# as plain `loom:pr`, unmerged and invisible.
#
# Extract-and-execute (#7979), not a mirror: the SHIPPED bash blocks are lifted
# out of champion-pr-merge.md and run against canned forge JSON with `gh`,
# `loom-daemon`, `$GH_READ` and `post-comment.sh` stubbed. Blocks run:
#   1. the merge-risk criterion's sticky-hold precheck ("Run this precheck before judging
#      the axes") -> MERGE_BLOCKED_BY_HOLD / RELEASE_REASON /
#      MANUAL_RELEASE_SINCE_HOLD / label repair;
#   2. "Hold behavior" -> same-concern PASS vs. fresh hold notice + label;
#   3. the Held-PR Census `RELEASED` line.
# The four risk axes are an LLM judgment with no executable form, so the
# narrowed merge-risk rule and its kept set are pinned as doc literals
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

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

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
# FIX_PR       the "0. PR facts" payload (commits, labels, headRefOid; #12242)
# FIX_COMMENTS its trusted comments (loom-daemon forge trusted-comments --gh-shape)
# FIX_EVENTS   issues/N/events payload; FIX_EVENTS_FAIL=1 makes the read fail
# FIX_LIST     gh pr list payload for the census
git() { [[ "$*" == "rev-parse --git-common-dir" ]] && echo "$WORK"; }
# Write the "0. PR facts" file the precheck reads, from FIX_PR + FIX_COMMENTS.
write_facts() { jq --argjson c "$FIX_COMMENTS" '.comments = $c' <<<"$FIX_PR" >"$WORK/loom-champion-facts-pr-$1.json"; }
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
export -f git gh loom-daemon gh_read

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
        write_facts "$PR_NUMBER"
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
        write_facts "$PR_NUMBER"
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

echo "Test 1: the merge-risk criterion narrowed — claim/label machinery is not red by itself; the kept set is listed (#11416)"
assert_doc_contains '**Claim/label machinery is not red by itself (#11416).**' "the narrowed rule is stated"
assert_doc_contains '"Reverting cannot undo forge labels already changed" or "a defect could strand work fleet-wide" is **not** a red reason.' \
    "the reasoning behind the #10280/#11321 holds is named and rejected"
assert_doc_contains '- **Irreversible migration, no rollback step**' "kept set: irreversible migrations"
assert_doc_contains '- **The merge gate itself**' "kept set: the merge gate"
assert_doc_contains 'Writing a verdict label (the Judge'"'"'s `loom:pr` approval) is part of the gate, not a role hand-back' \
    "kept set: the merge gate: verdict-label writes are the gate, not a hand-back"
for producer in post-verdict.sh verdict-staleness-guard.sh reconcile_pr_verdicts comment_trust.rs 'forge trusted-comments' check-review-feedback.sh; do
    assert_doc_contains "\`$producer\`" "kept set: the merge gate names gate-input producer $producer"
done
assert_doc_contains '- **The other red entries in the table above**, unchanged.' "kept set: existing red entries"
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

echo "Test 7c: a loom:operator removal AFTER the capped-hold recommendation clears the inbox, not the hold (#11905)"
REC_COMMENT=$(jq -n '{author: {login: "loom-fleet-dispatch"}, body: "<!-- champion:capped-hold-recommendation -->\nreview it", createdAt: "2026-10-10T06:00:00Z"}')
FIX_COMMENTS="[$HOLD_COMMENT, $REC_COMMENT]"
FIX_EVENTS=$(UNLABELED=loom:operator unlabeled_event "2026-10-10T12:00:00Z")
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true MANUAL=false" "the inbox clearing is not release (e)"
assert_contains "$OUT" "PARK park-record apply --pr 10280 --reason champion merge-risk hold --by champion" "the missing loom:blocked is repaired instead"
FIX_EVENTS=$(unlabeled_event "2026-10-10T12:00:00Z")
OUT=$(run_precheck)
assert_contains "$OUT" "MANUAL=true" "a human loom:blocked removal still releases after the recommendation"
FIX_EVENTS=$(UNLABELED=loom:operator unlabeled_event "2026-10-10T03:00:00Z")
OUT=$(run_precheck)
assert_contains "$OUT" "MANUAL=true" "a loom:operator removal before the recommendation still releases"
FIX_COMMENTS="[$HOLD_COMMENT]"
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

echo "Test 13: operator release condition (#12594) — survives a new head, no prior hold marker needed"
cond_comment() { # <marker-suffix ""|-met> <createdAt> <text> [author]
    jq -n --arg m "<!-- loom:release-condition$1 -->" --arg t "$3" --arg at "$2" --arg who "${4:-rjwalters}" \
        '{author: {login: $who}, body: ($m + "\n" + $t), createdAt: $at}'
}
COND=$(cond_comment "" "2026-10-10T09:00:00Z" "release only after predecessor #12000 merges")
MET=$(cond_comment "-met" "2026-10-12T09:00:00Z" "predecessor merged, healthy on all hosts")
NEWHEAD="abcdef1234567890"
FIX_EVENTS='[]'
FIX_PR=$(pr_json "$NEWHEAD" loom:pr loom:blocked); FIX_COMMENTS="[$HOLD_COMMENT, $COND]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true MANUAL=false REASON=" "new head + open condition still HOLDs"
assert_contains "$OUT" "release only after predecessor #12000 merges" "the notice names the condition text"
assert_contains "$OUT" "POST 10280 --pr --body" "a condition notice is posted once"
FIX_COMMENTS="[$HOLD_COMMENT, $COND]"
FIX_PR=$(pr_json "$NEWHEAD" loom:pr loom:blocked)
FIX_COMMENTS=$(jq -n --argjson c "$COND" '[$c, {author: {login: "loom-fleet-dispatch"}, body: "<!-- champion:release-condition-hold cond=2026-10-10T09:00:00Z -->\nheld", createdAt: "2026-10-10T09:05:00Z"}]')
OUT=$(run_precheck)
assert_lacks "$OUT" "POST" "the notice is idempotent per condition comment"
echo

echo "Test 13b: condition with no hold marker, label removed by a human, judgment-release paths all fire -> still HOLD, no label write"
FIX_PR=$(pr_json "$NEWHEAD" loom:pr); FIX_COMMENTS="[$COND]"; FIX_EVENTS=$(unlabeled_event "2026-10-11T12:00:00Z")
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true MANUAL=false" "human label removal does not satisfy the condition"
assert_lacks "$OUT" "PARK" "no label repair for a condition hold"
FIX_EVENTS='[]'
echo

echo "Test 13c: a later trusted -met comment closes it -> normal evaluation"
FIX_PR=$(pr_json "$NEWHEAD" loom:pr loom:blocked); FIX_COMMENTS="[$HOLD_COMMENT, $COND, $MET]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=false PRIOR=true" "condition closed, the new head releases the hold as before"
assert_contains "$OUT" "REASON=new head commit abcdef1" "release reason is the ordinary one"
FIX_COMMENTS="[$COND, $MET]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=false PRIOR=false" "closed condition and no hold marker -> never held"
echo

echo "Test 13d: amended condition wins; a -met older than the newest condition does not close it"
COND2=$(cond_comment "" "2026-10-14T09:00:00Z" "also needs a ruleset census")
FIX_COMMENTS="[$COND, $MET, $COND2]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true" "condition reopened by the newer comment"
assert_contains "$OUT" "also needs a ruleset census" "the latest condition text is quoted"
assert_lacks "$OUT" "predecessor #12000" "the superseded text is not"
echo

echo "Test 13e: Champion-authored lookalikes are ignored (condition and -met)"
CHAMP_COND=$(jq -n '{author: {login: "loom-fleet-dispatch"}, body: "<!-- loom:release-condition -->\nfake\n\n---\n*Automated by Champion role*", createdAt: "2026-10-10T09:00:00Z"}')
FIX_COMMENTS="[$CHAMP_COND]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=false PRIOR=false" "a Champion-authored condition does not hold"
CHAMP_MET=$(jq -n '{author: {login: "loom-fleet-dispatch"}, body: "<!-- loom:release-condition-met -->\nok\n\n*Automated by Champion role*", createdAt: "2026-10-12T09:00:00Z"}')
FIX_COMMENTS="[$COND, $CHAMP_MET]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true" "a Champion-authored -met does not close the condition"
echo

echo "Test 13f: loom:auto-merge-ok still overrides an open condition"
FIX_PR=$(pr_json "$NEWHEAD" loom:pr loom:blocked loom:auto-merge-ok); FIX_COMMENTS="[$HOLD_COMMENT, $COND]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=false PRIOR=true" "the explicit human override keeps its meaning"
assert_contains "$OUT" "REASON=override: \`loom:auto-merge-ok\` applied" "release reason is the override"
echo

echo "Test 13g: the doc states the condition rule"
assert_doc_contains '`<!-- loom:release-condition -->`' "condition marker documented"
assert_doc_contains '`<!-- loom:release-condition-met -->`' "met marker documented"
assert_doc_contains 'select(.body | is_release_instruction)' "path (b) reads the shared release-instruction definition"
echo

echo "Test 13h: the operator's own form — a 'clear the hold —' that cites the condition closes it; one that does not, or a reinforcement, does not"
person_comment() { # <createdAt> <body> [author]
    jq -n --arg at "$1" --arg b "$2" --arg who "${3:-rjwalters}" '{author: {login: $who}, body: $b, createdAt: $at}'
}
CLEAR_CITE=$(person_comment "2026-10-12T09:00:00Z" "clear the hold — 2026-10-20 operator ruling (rjwalters, attended session): the release condition in the ruling above is met.

**Evidence**: predecessor #12000 merged in v0.19.1178; every host healthy on it.")
CLEAR_OTHER=$(person_comment "2026-10-12T09:00:00Z" "clear the hold — the blast-radius hold is accepted; this change is revert-safe.")
REINFORCE=$(person_comment "2026-10-12T09:00:00Z" "do not clear the hold — the condition is not met yet.")
# Same head as the hold, so only a comment can release anything.
FIX_PR=$(pr_json "$HOLD_HEAD" loom:pr loom:blocked); FIX_COMMENTS="[$HOLD_COMMENT, $COND, $CLEAR_CITE]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=false PRIOR=true" "a citing clear closes the condition and releases the hold"
assert_contains "$OUT" "REASON=operator comment cleared the hold — rjwalters at 2026-10-12T09:00:00Z" "released on path (b)"
FIX_COMMENTS="[$HOLD_COMMENT, $COND, $CLEAR_OTHER]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true MANUAL=false REASON=" "an unrelated clear releases the merge-risk hold, and the condition re-imposes it"
assert_contains "$OUT" "release only after predecessor #12000 merges" "the notice names the still-open condition"
FIX_COMMENTS="[$HOLD_COMMENT, $COND, $REINFORCE]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true" "a reinforcement is not a release"
echo

echo "Test 13i: an App-spelled author (an unattended fleet role) can neither close a condition nor cite it closed"
BOT_MET=$(cond_comment "-met" "2026-10-12T09:00:00Z" "predecessor merged" "loom-fleet-dispatch[bot]")
BOT_CLEAR=$(person_comment "2026-10-12T09:00:00Z" "clear the hold — the release condition is met." "2am-fleet-dispatch-rjwalters[bot]")
FIX_COMMENTS="[$HOLD_COMMENT, $COND, $BOT_MET]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true" "a [bot] -met does not close the condition"
FIX_COMMENTS="[$HOLD_COMMENT, $COND, $BOT_CLEAR]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true" "a [bot] citing clear does not close the condition"
# Closing the condition is not itself a release of the merge-risk hold: at the
# held head the ordinary rows apply (still held); a new head then releases it.
FIX_COMMENTS="[$HOLD_COMMENT, $COND, $(cond_comment "-met" "2026-10-12T09:00:00Z" "predecessor merged" "example-reviewer")]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true MANUAL=false REASON=" "a person's -met closes the condition; the merge-risk hold still stands at its head"
assert_lacks "$OUT" "POST" "no condition notice once it is closed"
FIX_PR=$(pr_json "$NEWHEAD" loom:pr loom:blocked)
OUT=$(run_precheck)
assert_contains "$OUT" "REASON=new head commit abcdef1" "and the ordinary release paths apply again"
echo

echo "Test 13j: loom:auto-merge-ok overrides a condition that has no hold marker (path (a) never ran)"
FIX_PR=$(pr_json "$NEWHEAD" loom:pr loom:auto-merge-ok); FIX_COMMENTS="[$COND]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=false PRIOR=false" "the label overrides, marker or not"
assert_lacks "$OUT" "POST" "no condition notice under the override"
echo

echo "Test 13k: a condition that quotes a Champion marker name still binds, and same-line text is quoted"
QUOTING=$(person_comment "2026-10-10T09:00:00Z" "<!-- loom:release-condition --> release after #12000 merges; the champion:merge-risk-hold above stays until then")
FIX_PR=$(pr_json "$NEWHEAD" loom:pr loom:blocked); FIX_COMMENTS="[$HOLD_COMMENT, $QUOTING]"
OUT=$(run_precheck)
assert_contains "$OUT" "BLOCKED=true PRIOR=true" "quoting champion:merge-risk-hold does not void the condition"
assert_contains "$OUT" "release after #12000 merges; the champion:merge-risk-hold above stays until then" "text on the marker line is quoted"
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
