#!/usr/bin/env bash
# test-champion-tracker-marker-skip.sh - Regression test for issue #10886.
#
# A living tracker/hub marked `<!-- loom:tracker -->` (alone on a line, outside a
# code fence) must be skipped by Champion before any rejection is counted, so it
# never reaches the N=2 "Repeated Rejection Without Revision" escalation. An
# unmarked issue with the same two NEEDS REVISION comments is still escalated.
#
# Extracts the shipped guard from champion-issue-promo.md and the Priority 2
# discovery jq from champion.md and runs them against fixtures. Hermetic: file
# reads plus jq. No `set -o pipefail` on purpose (#7790).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi
PROMO="$ROLE_DIR/champion-issue-promo.md"
CHAMPION="$ROLE_DIR/champion.md"

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

command -v jq >/dev/null || { echo "jq required"; exit 1; }

# The guard's first bash block (Idempotency check), up to and including the
# ALREADY_ROUTED assignment.
guard_prefix() {
    awk '
        /^#+ / && !fence { g = index($0, "Idempotency check (run BEFORE claiming") > 0 }
        g && /^```bash/ && !done { fence = 1; next }
        g && fence && /^```$/ { done = 1; fence = 0 }
        g && fence { print; if ($0 ~ /^ALREADY_ROUTED=/) exit }
    ' "$1"
}

# Run the extracted guard against an ISSUE_JSON; print ALREADY_ROUTED and the
# rejection tally. The forge fetch (2 lines) is replaced by the fixture.
run_guard() {
    local tmp
    tmp=$(mktemp -d)
    printf '%s' "$1" > "$tmp/issue.json"
    {
        echo 'ISSUE_NUMBER=1'
        echo 'ISSUE_JSON=$(cat "$FIXTURE")'
        echo 'loom-daemon() { return 1; }'
        guard_prefix "$PROMO" | awk '
            /^ISSUE_NUMBER=/ { next }
            /^ISSUE_JSON=\$\(/ { getline; next }
            { print }'
        echo 'echo "ROUTED=$ALREADY_ROUTED REJ=$PRIOR_REJECTIONS"'
    } > "$tmp/g.sh"
    FIXTURE="$tmp/issue.json" bash "$tmp/g.sh" 2>"$tmp/err" | tail -1
    rm -rf "$tmp"
}

REJ='{"body":"Champion Review: NEEDS REVISION"}'
mk() { jq -nc --arg b "$1" --argjson r "$REJ" --arg l "$2" \
    '{title:"t",body:$b,labels:(([{name:"loom:curated"}])+($l|if .=="" then [] else [{name:.}] end)),comments:[$r,$r]}'; }

echo "Guard (champion-issue-promo.md):"
out=$(run_guard "$(mk $'Track the gap.\n<!-- loom:tracker -->\n' '')")
[[ "$out" == "ROUTED=yes REJ=2" ]] && pass "marked tracker with 2 rejections is ALREADY_ROUTED (not escalated)" || fail "marked tracker: got '$out'"
out=$(run_guard "$(mk 'Ordinary proposal.' '')")
[[ "$out" == "ROUTED=no REJ=2" ]] && pass "unmarked issue with 2 rejections still reaches the bound" || fail "unmarked: got '$out'"
out=$(run_guard "$(mk $'Example:\n```\n<!-- loom:tracker -->\n```\n' '')")
[[ "$out" == "ROUTED=no REJ=2" ]] && pass "marker inside a code fence does not count" || fail "fenced: got '$out'"
out=$(run_guard "$(mk $'> <!-- loom:tracker -->\nsee <!-- loom:tracker --> inline' '')")
[[ "$out" == "ROUTED=no REJ=2" ]] && pass "quoted/inline marker does not count" || fail "quoted: got '$out'"
out=$(run_guard "$(mk $'<!-- loom:tracker -->' 'loom:issue')")
[[ "$out" == "ROUTED=yes REJ=2" ]] && pass "marked tracker that also carries loom:issue is skipped" || fail "marked+issue: got '$out'"

echo "Discovery (champion.md Priority 2):"
DISC=$(awk '/^HOLDS=/ && !f {f=1} f {print} f && /"#\\\(\.number\)/ {exit}' "$CHAMPION" | sed -n '/^gh api --paginate.*loom:curated/,$p' | sed '1d;s/^  | jq/jq/')
issues='[{"number":1,"title":"tracker","created_at":"a","labels":[{"name":"loom:curated"}],"body":"x\n<!-- loom:tracker -->"},{"number":2,"title":"plain","created_at":"b","labels":[{"name":"loom:curated"}],"body":"y"}]'
res=$(printf '%s' "$issues" | HOLDS=$'loom:blocked\nloom:operator\nloom:frozen' bash -c "$DISC" 2>&1)
[[ "$res" == "#2 plain" ]] && pass "discovery lists the unmarked issue and skips the tracker" || fail "discovery: got '$res'"

echo "Docs:"
grep -q 'loom:tracker' "$ROLE_DIR/../../../roles/curator.md" && pass "curator.md documents the marker" || fail "curator.md missing marker"
grep -q 'Tracker marker' "$REPO_ROOT/defaults/docs/label-state-machine.md" && pass "label-state-machine.md documents the marker" || fail "label-state-machine.md missing marker"

echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
