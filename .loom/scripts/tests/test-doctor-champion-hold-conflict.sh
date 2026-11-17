#!/usr/bin/env bash
# test-doctor-champion-hold-conflict.sh - runs the SHIPPED Doctor selector for
# Champion-held CONFLICTING PRs (#13505).
#
# Extracts CHAMPION_HELD_CONFLICT from doctor.md and applies it to fixture PR
# JSON (the shape `gh pr view --json number,mergeable,labels` plus trusted
# `.comments`), asserting which shapes are admitted. Also pins the prose
# guarantees (hold stays, no changes-requested).
#
# Usage: ./.loom/scripts/tests/test-doctor-champion-hold-conflict.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
DOCTOR_MD="$PROMPT_DIR/doctor.md"

TESTS_RUN=0
TESTS_FAILED=0
check() { # <expected> <actual> <message>
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$1" == "$2" ]]; then
        echo "  PASS: $3"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo "  FAIL: $3"
        echo "    expected: '$1'"
        echo "    actual:   '$2'"
    fi
}

# Pull the single-quoted jq program out of the shipped block.
PROG=$(awk "/^CHAMPION_HELD_CONFLICT='/{f=1; sub(/^CHAMPION_HELD_CONFLICT='/,\"\")} f{print} f && /'\$/{exit}" "$DOCTOR_MD" | sed '$ s/'"'"'$//')
[[ -n "$PROG" ]] || { echo "FAIL: could not extract CHAMPION_HELD_CONFLICT from $DOCTOR_MD"; exit 1; }

RH='<!-- champion:ratification-hold -->'
MR='<!-- champion:merge-risk-hold -->'

# pr <mergeable> <labels,csv> <comment>...
pr() {
    local m=$1 labels=$2; shift 2
    jq -n --arg m "$m" --arg l "$labels" '{number: 7, mergeable: $m, labels: ($l | split(",") | map({name: .}))}' \
        | jq --args '.comments = ($ARGS.positional | map({body: .}))' "$@"
}
sel() { jq -r "$PROG" <<<"$1"; }

echo "selector admits"
check 7 "$(sel "$(pr CONFLICTING loom:pr,loom:blocked "$RH")")" "ratification hold + CONFLICTING"
check 7 "$(sel "$(pr CONFLICTING loom:pr,loom:blocked "$MR")")" "merge-risk hold + CONFLICTING"
check 7 "$(sel "$(pr CONFLICTING loom:pr,loom:blocked "$RH" "<!-- champion:ratification-hold-cleared -->" "$RH")")" "re-armed hold after a cleared marker"
check 7 "$(sel "$(pr CONFLICTING loom:pr,loom:blocked "$MR" "$RH")")" "both holds present"

echo "selector excludes"
check "" "$(sel "$(pr MERGEABLE loom:pr,loom:blocked "$RH")")" "MERGEABLE"
check "" "$(sel "$(pr CONFLICTING loom:pr,loom:blocked)")" "blocked without any marker (untrusted markers are filtered upstream)"
check "" "$(sel "$(pr CONFLICTING loom:pr,loom:blocked,loom:operator "$RH")")" "loom:operator"
check "" "$(sel "$(pr CONFLICTING loom:pr,loom:blocked,loom:frozen "$RH")")" "loom:frozen"
check "" "$(sel "$(pr CONFLICTING loom:pr,loom:blocked,loom:treating "$RH")")" "already claimed (loom:treating)"
check "" "$(sel "$(pr CONFLICTING loom:pr "$RH")")" "marker but no loom:blocked"
check "" "$(sel "$(pr CONFLICTING loom:blocked "$RH")")" "no loom:pr"
check "" "$(sel "$(pr CONFLICTING loom:pr,loom:blocked "$RH" "<!-- champion:ratification-hold-cleared -->")")" "cleared ratification hold"
check "" "$(sel "$(pr CONFLICTING loom:pr,loom:blocked "$RH" "<!-- champion:ratification-release-respected -->")")" "released ratification hold"
check "" "$(sel "$(pr CONFLICTING loom:pr,loom:blocked "$MR" "<!-- champion:merge-risk-hold-cleared -->")")" "cleared merge-risk hold"
check "" "$(sel "$(pr CONFLICTING loom:pr,loom:blocked "quoting $RH mid-comment")")" "marker not at comment start"

echo "prose"
grep -q 'never add' "$DOCTOR_MD" && grep -q '`loom:changes-requested`' "$DOCTOR_MD"
check 0 $? "doctor.md forbids adding loom:changes-requested on this path"
grep -q 'Never remove `loom:blocked` or `loom:pr`' "$DOCTOR_MD"
check 0 $? "doctor.md forbids removing the hold"
grep -q 'served by Doctor' "$PROMPT_DIR/champion-ratification-hold.md"
check 0 $? "champion-ratification-hold.md notes Doctor serves held conflicts"

echo "Ran $TESTS_RUN, failed $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
