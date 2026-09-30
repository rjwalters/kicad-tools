#!/usr/bin/env bash
# test-champion-issue-promo-escalation-respects-human-hold.sh - Regression test
# for issue #8245 (the PROPOSAL-side port of #7734 / #7921 / #7965).
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# champion-issue-promo.md's "Idempotency check" escalates an unrevised,
# repeatedly-rejected proposal to `loom:operator-only` once
# LOOM_MAX_UNREVISED_EVALUATIONS is spent. The epic ladder had exactly two ways
# to re-fight a ruling a human had already made on an unchanged item, both
# fixed there (#7734 / #7921 / #7965) and both still open here until #8245:
#
#   widened hold set   The only "a human owns this" signal the proposal check
#                      read was `loom:operator-only` itself. A proposal an
#                      operator parked on `loom:blocked` (or held with
#                      `loom:operator`) was tallied and escalated like an
#                      untouched one, re-applying `loom:operator-only` on top
#                      of the park.
#   un-park ruling     An operator un-parked a proposal (removed
#                      `loom:operator-only`, added nothing). Its label state
#                      was then byte-identical to "never escalated", the body
#                      hash was unchanged and the tally was at the cap -- so
#                      the very next pass re-escalated. No label can
#                      distinguish that case, so the guard reads the issue
#                      TIMELINE: an `unlabeled loom:operator-only` event newer
#                      than the current revision's own verdict comment is a
#                      ruling on this exact body.
#
# ONE PROPOSAL-SPECIFIC DIFFERENCE, AND WHY IT IS TESTED HARDEST
#
# The epic guard's actor-blind timeline read is made safe by BOT_UNESCALATABLE:
# it refuses to rule when a `<!-- champion:proposal-escalated -->` comment is
# present, because that comment is classify-dependency-block.sh's own
# precondition for un-escalating anything. That check CANNOT be ported verbatim
# to this file: here that marker is present by construction on every escalated
# proposal, so copying it would make OPERATOR_RULED dead code. The proposal
# path uses the positive record instead -- classify-dependency-block.sh --apply
# posts a `champion:proposal-unescalated[-facts]:` marker comment immediately
# AFTER removing the label -- so attribution is by TIME (BOT_UNESCALATED_AT),
# not by presence. Test 4c is the control that the bot's own un-park still
# cannot masquerade as a human ruling.
#
# WHAT THIS SUITE DOES
#
#   1-3. LINT/WIRING -- the shipped guard names the three hold labels, derives
#        ALREADY_ROUTED from HELD_BY, reads the timeline un-park, initialises
#        OPERATOR_RULED, and orders its branches routed -> ruled -> escalate
#        without tallying or escalating in the ruled branch. Exercised against
#        pre-fix / post-fix fixtures so no lint can pass vacuously.
#   4.   BEHAVIOUR -- the guard's own bash block is EXTRACTED from the shipped
#        prompt and EXECUTED against stubbed `gh` / `$GH_READ` /
#        classify-dependency-block.sh, over five fixtures. This is what proves
#        the mechanism, not just its spelling.
#   5.   WIRING -- Step 4's escalation preconditions and the batch outcome
#        table carry both flags.
#
# Hermetic: pure file reads plus a mktemp -d fixture dir with local stubs. No
# forge, no network. Requires jq (already required by many CI-wired suites).
# No `set -o pipefail` on purpose (#7790).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Role prompts are shipped (installed at .claude/commands/loom), so resolve the
# way each layout actually lays it out: the installed path first (consumer
# repos, and Loom's own dogfooded checkout), falling back to the defaults/
# source-tree path. See issue #6194 / #6241.
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi

PROMO="$ROLE_DIR/champion-issue-promo.md"

# The same three labels champion-epic.md's guard keys on since #7734.
EXPECTED_HOLD_LABELS=$'loom:blocked\nloom:operator\nloom:operator-only'

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Extract one section by a substring of its heading, from that heading up to the
# next heading of the SAME OR SHALLOWER level. Fenced code blocks are skipped
# when looking for headings so a shell comment is never mistaken for one.
section_body() {
    awk -v want="$2" '
        /^```/ { fence = !fence }
        !fence && /^#+ / {
            n = 0
            while (substr($0, n + 1, 1) == "#") n++
            if (inside && n <= lvl) inside = 0
            if (!inside && index($0, want) > 0) { inside = 1; lvl = n }
        }
        inside { print }
    ' "$1"
}

# The guard's own check block: the first fenced ```bash block inside the
# "Idempotency check (run BEFORE claiming" section, with file-relative line
# numbers prefixed so branch ordering can be asserted.
guard_block() {
    awk '
        /^#+ / && !fence { in_guard = index($0, "Idempotency check (run BEFORE claiming") > 0 }
        in_guard && /^```bash/ && !done { fence = 1; next }
        in_guard && fence && /^```$/ { done = 1; fence = 0 }
        in_guard && fence { printf "%d\t%s\n", FNR, $0 }
    ' "$1"
}

# Every `loom:*` label literally named in the line(s) deriving the hold decision:
# the HELD_BY assignment and its jq continuation, or -- on the pre-fix form --
# the single-label ALREADY_ROUTED assignment.
hold_labels_of() {
    guard_block "$1" \
        | cut -f2- \
        | awk '
            /^HELD_BY=/ || /^ALREADY_ROUTED=\$\(printf/ { grab = 2 }
            grab > 0 { print; grab-- }
        ' \
        | grep -oE 'loom:[a-z-]+' \
        | LC_ALL=C sort -u
}

# 0 if the guard block reads the timeline for an `unlabeled loom:operator-only`
# event, compares it against a verdict `created_at`, and derives OPERATOR_RULED
# from that comparison; 1 otherwise.
has_unpark_check() {
    local block
    block="$(guard_block "$1" | cut -f2-)"
    grep -qE 'issues/\$ISSUE_NUMBER/timeline' <<<"$block" \
        && grep -qE '\.event == "unlabeled"' <<<"$block" \
        && grep -qE '\.label\.name == "loom:operator-only"' <<<"$block" \
        && grep -qE 'VERDICT_CREATED_AT=.*created_at' <<<"$block" \
        && grep -qE 'UNPARKED_AT.*>.*VERDICT_CREATED_AT' <<<"$block" \
        && grep -qE '^ *OPERATOR_RULED=yes' <<<"$block"
}

# The whole (possibly line-continued) `if` condition guarding OPERATOR_RULED=yes,
# flattened onto one line.
unpark_condition_of() {
    guard_block "$1" | cut -f2- | awk '
        /^ *if / { cond = $0; collecting = 1; next }
        collecting && /^ *OPERATOR_RULED=yes/ { print cond; exit }
        collecting { cond = cond " " $0 }
    '
}

# 0 if the guard attributes an un-park by TIME against the bot's own
# un-escalation marker comment (the proposal-side replacement for the epic
# path's presence-based BOT_UNESCALATABLE); 1 otherwise.
has_bot_attribution_probe() {
    local block
    block="$(guard_block "$1" | cut -f2-)"
    grep -qE '^ *BOT_UNESCALATED_AT=' <<<"$block" \
        && grep -qF 'champion:proposal-unescalated' <<<"$block"
}

echo "================================"
echo "test-champion-issue-promo-escalation-respects-human-hold.sh (#8245)"
echo "================================"

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# A minimal guard in the PRE-fix shape: one label, no timeline read.
cat >"$FIXTURE_DIR/pre-fix.md" <<'EOF'
### Idempotency check (run BEFORE claiming — skip silently on a match)

```bash
PRIOR_REJECTIONS=$(printf '%s\n' "$ISSUE_JSON" | jq '[.comments[]] | length')
ALREADY_ROUTED=$(printf '%s\n' "$ISSUE_JSON" | jq -e '.labels[] | select(.name=="loom:operator-only")' >/dev/null && echo yes || echo no)
SKIP_STREAK=0
if [ "$ALREADY_ROUTED" = "yes" ]; then
  echo "already routed"
elif [ "$UNREVISED_EVALS" -ge "${LOOM_MAX_UNREVISED_EVALUATIONS:-2}" ]; then
  ESCALATE_UNREVISED=yes
fi
```
EOF

# The same guard in the POST-fix shape.
cat >"$FIXTURE_DIR/post-fix.md" <<'EOF'
### Idempotency check (run BEFORE claiming — skip silently on a match)

```bash
PRIOR_REJECTIONS=$(printf '%s\n' "$ISSUE_JSON" | jq '[.comments[]] | length')
HELD_BY=$(printf '%s\n' "$ISSUE_JSON" | jq -r \
  '[.labels[].name | select(. == "loom:operator-only" or . == "loom:blocked" or . == "loom:operator")] | join(",")')
ALREADY_ROUTED=$([ -n "$HELD_BY" ] && echo yes || echo no)
OPERATOR_RULED=no
if true; then
  VERDICT_CREATED_AT=$(printf '%s\n' "$VERDICT_COMMENT" | jq -r '.created_at // ""')
  UNPARKED_AT=$(gh api "repos/{owner}/{repo}/issues/$ISSUE_NUMBER/timeline" --paginate \
    --jq '.[] | select(.event == "unlabeled" and .label.name == "loom:operator-only") | .created_at' \
    | sort | tail -n 1)
  if [ -n "$UNPARKED_AT" ] && [[ "$UNPARKED_AT" > "$VERDICT_CREATED_AT" ]]; then
    OPERATOR_RULED=yes
  fi
fi
```
EOF

# --- Test 1: controls -- the lints discriminate the pre-fix shape from the fix
echo ""
echo "Test 1: the lints flag the pre-#8245 guard shape and accept the fixed one"
PRE_LABELS="$(hold_labels_of "$FIXTURE_DIR/pre-fix.md")"
if [[ "$PRE_LABELS" == "loom:operator-only" ]]; then
    pass "a guard keyed on loom:operator-only alone is reported as such"
else
    fail "pre-fix fixture: expected only loom:operator-only, got:"$'\n'"$PRE_LABELS"
fi
POST_LABELS="$(hold_labels_of "$FIXTURE_DIR/post-fix.md")"
if [[ "$POST_LABELS" == "$EXPECTED_HOLD_LABELS" ]]; then
    pass "a guard keyed on all three hold labels is recognised"
else
    fail "post-fix fixture: expected the three hold labels, got:"$'\n'"$POST_LABELS"
fi
if has_unpark_check "$FIXTURE_DIR/pre-fix.md"; then
    fail "pre-fix fixture (no timeline read) was accepted as having the un-park check (lint is vacuous)"
else
    pass "a guard with no timeline un-park check is reported"
fi
if has_bot_attribution_probe "$FIXTURE_DIR/post-fix.md"; then
    fail "a fixture with no BOT_UNESCALATED_AT was accepted as having the bot-attribution probe (lint is vacuous)"
else
    pass "a guard with no bot-attribution probe is reported"
fi

# --- Test 2: the shipped guard keys on the widened hold-label set
echo ""
echo "Test 2: the shipped guard's hold check names all three hold labels (#7734's set)"
if [[ ! -f "$PROMO" ]]; then
    fail "champion-issue-promo.md not found at $PROMO"
else
    GUARD_LABELS="$(hold_labels_of "$PROMO")"
    if [[ "$GUARD_LABELS" == "$EXPECTED_HOLD_LABELS" ]]; then
        pass "the guard's HELD_BY check names loom:blocked, loom:operator, loom:operator-only"
    else
        fail "guard hold labels are not the expected three:"$'\n'"$(tr '\n' ' ' <<<"$GUARD_LABELS")"
    fi
    BLOCK="$(guard_block "$PROMO" | cut -f2-)"
    if grep -qE '^ALREADY_ROUTED=\$\(\[ -n "\$HELD_BY" \]' <<<"$BLOCK"; then
        pass "ALREADY_ROUTED is derived from HELD_BY (any hold label), not from a single-label jq select"
    else
        fail "ALREADY_ROUTED is not derived from HELD_BY"
    fi
    # The self-healing un-escalation must stay gated on the ONE label it can
    # remove -- widening its gate would call the script on a loom:blocked park.
    if grep -qE '^if \[ "\$OPERATOR_ONLY_PRESENT" = "yes" \]' <<<"$BLOCK"; then
        pass "the #5664 self-healing un-escalation is gated on OPERATOR_ONLY_PRESENT, not on the widened ALREADY_ROUTED"
    else
        fail "the self-healing un-escalation is not gated on OPERATOR_ONLY_PRESENT"
    fi
    if grep -qE '^ *OPERATOR_RULED=no' <<<"$BLOCK"; then
        pass "OPERATOR_RULED is initialised alongside the other escalation inputs"
    else
        fail "OPERATOR_RULED is not initialised (Step 4 would read an unset variable)"
    fi
fi

# --- Test 3: the un-park read, its fail-open preconditions, and branch order
echo ""
echo "Test 3: the guard reads the timeline un-park, fails open on unknown inputs, and orders its branches correctly"
if [[ -f "$PROMO" ]]; then
    if has_unpark_check "$PROMO"; then
        pass "guard reads unlabeled loom:operator-only from the timeline and compares it to VERDICT_CREATED_AT"
    else
        fail "guard is missing the timeline un-park check"
    fi
    if has_bot_attribution_probe "$PROMO"; then
        pass "guard computes BOT_UNESCALATED_AT from the champion:proposal-unescalated marker comment"
    else
        fail "guard does not compute BOT_UNESCALATED_AT (a bot un-park could masquerade as a human ruling)"
    fi
    COND="$(unpark_condition_of "$PROMO")"
    if [[ -z "$COND" ]]; then
        fail "could not extract the if-condition guarding OPERATOR_RULED=yes"
    else
        if grep -qF -- '-n "$VERDICT_CREATED_AT"' <<<"$COND"; then
            pass "an empty/failed VERDICT_CREATED_AT read fails open rather than matching any historical un-park"
        else
            fail "the OPERATOR_RULED condition does not require a non-empty VERDICT_CREATED_AT:"$'\n'"$COND"
        fi
        if grep -qF 'BOT_UNESCALATED_AT' <<<"$COND"; then
            pass "the bot-attribution comparison sits in the OPERATOR_RULED condition itself"
        else
            fail "the OPERATOR_RULED condition does not consult BOT_UNESCALATED_AT:"$'\n'"$COND"
        fi
    fi
    NUMBERED="$(guard_block "$PROMO")"
    ROUTED_LINE="$(grep -E $'\t *(el)?if \\[ "\\$ALREADY_ROUTED" = "yes" \\]' <<<"$NUMBERED" | cut -f1 | tail -n 1)"
    RULED_LINE="$(grep -E $'\t *elif \\[ "\\$OPERATOR_RULED" = "yes" \\]' <<<"$NUMBERED" | cut -f1)"
    ESC_LINE="$(grep -E $'\t *elif \\[ "\\$UNREVISED_EVALS" -ge ' <<<"$NUMBERED" | cut -f1)"
    RULED_LINE="${RULED_LINE%%$'\n'*}"; ESC_LINE="${ESC_LINE%%$'\n'*}"
    if [[ -n "$ROUTED_LINE" && -n "$RULED_LINE" && -n "$ESC_LINE" ]]; then
        if (( ROUTED_LINE < RULED_LINE && RULED_LINE < ESC_LINE )); then
            pass "branch order is already-held ($ROUTED_LINE) -> operator ruled ($RULED_LINE) -> escalate ($ESC_LINE)"
        else
            fail "branch order wrong: routed=$ROUTED_LINE ruled=$RULED_LINE escalate=$ESC_LINE"
        fi
        RULED_BODY="$(awk -F'\t' -v s="$RULED_LINE" -v e="$ESC_LINE" '$1 > s && $1 < e { print $2 }' <<<"$NUMBERED")"
        if grep -qE 'ESCALATE_UNREVISED=yes|--method PATCH|--add-label|gh issue comment' <<<"$RULED_BODY"; then
            fail "the OPERATOR_RULED branch tallies, comments, labels, or escalates:"$'\n'"$RULED_BODY"
        else
            pass "the OPERATOR_RULED branch only logs and ends the pass (no tally, no comment, no label, no escalation)"
        fi
    else
        fail "could not locate all three branches (routed=$ROUTED_LINE ruled=$RULED_LINE escalate=$ESC_LINE)"
    fi
else
    fail "champion-issue-promo.md not found at $PROMO"
fi

# ---------------------------------------------------------------------------
# Test 4: BEHAVIOUR -- run the shipped guard block against stubbed inputs.
# ---------------------------------------------------------------------------

RUN_DIR="$FIXTURE_DIR/run"
mkdir -p "$RUN_DIR/.loom/scripts" "$RUN_DIR/bin" "$RUN_DIR/fix"

# `gh` stub. Reads the fixture JSON for the two REST paths the guard calls and
# applies the guard's OWN --jq expression to it, so the filter is exercised too.
cat >"$RUN_DIR/bin/gh" <<'STUB'
#!/usr/bin/env bash
# Writes (PATCH of the skip tally, label edits) are no-ops here.
[[ " $* " == *" --method "* ]] && exit 0
[[ "${1:-}" != "api" ]] && exit 0
shift
path=""; expr=""
while [ $# -gt 0 ]; do
  case "$1" in
    --jq) expr="$2"; shift 2 ;;
    --paginate) shift ;;
    -*) shift ;;
    *) path="$1"; shift ;;
  esac
done
case "$path" in
  *timeline*) src="$LOOM_TEST_FIX/timeline.json" ;;
  *comments*) src="$LOOM_TEST_FIX/rest-comments.json" ;;
  *) exit 0 ;;
esac
[ -f "$src" ] || exit 0
if [ -n "$expr" ]; then jq -r "$expr" "$src"; else cat "$src"; fi
STUB

# `$GH_READ` stub: the cached `gh issue view --json ...` read.
cat >"$RUN_DIR/bin/gh-read" <<'STUB'
#!/usr/bin/env bash
cat "$LOOM_TEST_FIX/issue.json"
STUB

# classify-dependency-block.sh stub: exit code comes from the fixture.
cat >"$RUN_DIR/.loom/scripts/classify-dependency-block.sh" <<'STUB'
#!/usr/bin/env bash
exit "$(cat "$LOOM_TEST_FIX/unesc-rc" 2>/dev/null || echo 1)"
STUB
chmod +x "$RUN_DIR/bin/gh" "$RUN_DIR/bin/gh-read" "$RUN_DIR/.loom/scripts/classify-dependency-block.sh"

# Extract the shipped guard block, neutralise its `<number>` placeholder, and
# append a trailer that reports the flags the rest of the file consumes.
extract_runnable() {
    {
        guard_block "$PROMO" | cut -f2- \
            | sed 's|^ISSUE_NUMBER=<number>$|ISSUE_NUMBER="${LOOM_TEST_ISSUE:-1}"|'
        echo ''
        echo 'echo "RESULT BODY_HASH=${BODY_HASH:-}"'
        echo 'echo "RESULT ALREADY_ROUTED=${ALREADY_ROUTED:-}"'
        echo 'echo "RESULT OPERATOR_RULED=${OPERATOR_RULED:-}"'
        echo 'echo "RESULT ESCALATE_UNREVISED=${ESCALATE_UNREVISED:-}"'
        echo 'echo "RESULT FORCE_REEVALUATE=${FORCE_REEVALUATE:-}"'
    } >"$RUN_DIR/guard.sh"
}

# Run the extracted guard with $1 as the fixture dir; echo its RESULT lines.
run_guard() {
    ( cd "$RUN_DIR" \
        && PATH="$RUN_DIR/bin:$PATH" \
           LOOM_TEST_FIX="$1" \
           GH_READ="$RUN_DIR/bin/gh-read" \
           bash ./guard.sh 2>&1 ) | grep '^RESULT '
}

result_of() { sed -n "s/^RESULT $2=//p" <<<"$1"; }

# Build a fixture dir: $1 dir, $2 labels JSON array, $3 comments JSON array,
# $4 timeline JSON array, $5 REST comments JSON array, $6 un-escalate rc.
make_fixture() {
    mkdir -p "$1"
    printf '{"title":"A proposal","body":"Unchanged body text.","labels":%s,"comments":%s}\n' \
        "$2" "$3" >"$1/issue.json"
    printf '%s\n' "$4" >"$1/timeline.json"
    printf '%s\n' "$5" >"$1/rest-comments.json"
    printf '%s\n' "$6" >"$1/unesc-rc"
}

echo ""
echo "Test 4: the shipped guard block, executed against stubbed forge reads"
if [[ ! -f "$PROMO" ]]; then
    fail "champion-issue-promo.md not found at $PROMO"
elif ! command -v jq >/dev/null 2>&1; then
    fail "jq is required by this suite but was not found on PATH"
else
    extract_runnable

    # Stage 1 -- let the code under test derive BODY_HASH for us, so the fixture
    # markers below are never a hand-copied guess that can silently drift.
    PROBE="$RUN_DIR/fix/probe"
    make_fixture "$PROBE" '[]' '[]' '[]' '[]' 1
    PROBE_OUT="$(run_guard "$PROBE")"
    BODY_HASH="$(result_of "$PROBE_OUT" BODY_HASH)"
    if [[ -n "$BODY_HASH" ]]; then
        pass "the extracted guard runs and derives a body hash ($BODY_HASH)"
    else
        fail "the extracted guard produced no BODY_HASH:"$'\n'"$PROBE_OUT"
    fi

    VMARK="<!-- champion:proposal-verdict:body-$BODY_HASH -->"
    # A verdict comment at the cap: 1 posted rejection + 1 recorded skip = 2.
    VBODY="$VMARK\nChampion Review: NEEDS REVISION\n<!-- champion:unrevised-skips:$BODY_HASH:1 -->"
    VERDICT_TS="2026-09-16T12:00:00Z"
    VIEW_COMMENTS="$(jq -nc --arg b "$(printf '%b' "$VBODY")" --arg t "$VERDICT_TS" \
        '[{body:$b, createdAt:$t}]')"
    REST_COMMENTS="$(jq -nc --arg b "$(printf '%b' "$VBODY")" --arg t "$VERDICT_TS" \
        '[{id:99, body:$b, created_at:$t}]')"
    UNPARK_TS="2026-09-16T17:05:00Z"
    TL_UNPARK="$(jq -nc --arg t "$UNPARK_TS" \
        '[{event:"unlabeled", label:{name:"loom:operator-only"}, created_at:$t}]')"

    # --- 4a. Human un-parked this exact body after its rejection -> stand down.
    F="$RUN_DIR/fix/human-ruled"
    make_fixture "$F" '[]' "$VIEW_COMMENTS" "$TL_UNPARK" "$REST_COMMENTS" 1
    OUT="$(run_guard "$F")"
    if [[ "$(result_of "$OUT" OPERATOR_RULED)" == "yes" \
       && "$(result_of "$OUT" ESCALATE_UNREVISED)" == "no" ]]; then
        pass "4a: an operator un-park newer than this revision's verdict sets OPERATOR_RULED=yes and does NOT escalate"
    else
        fail "4a: expected OPERATOR_RULED=yes / ESCALATE_UNREVISED=no, got:"$'\n'"$OUT"
    fi

    # --- 4b. Never touched by a human -> the N=2 ladder still fires.
    F="$RUN_DIR/fix/untouched"
    make_fixture "$F" '[]' "$VIEW_COMMENTS" '[]' "$REST_COMMENTS" 1
    OUT="$(run_guard "$F")"
    if [[ "$(result_of "$OUT" OPERATOR_RULED)" == "no" \
       && "$(result_of "$OUT" ESCALATE_UNREVISED)" == "yes" ]]; then
        pass "4b: an unrevised proposal no human ever touched still escalates once the skip budget is spent"
    else
        fail "4b: expected OPERATOR_RULED=no / ESCALATE_UNREVISED=yes, got:"$'\n'"$OUT"
    fi

    # --- 4c. The bot's OWN un-escalation must not masquerade as a human ruling.
    BOT_COMMENTS="$(jq -nc --arg b "$(printf '%b' "$VBODY")" --arg t "$VERDICT_TS" --arg u "$UNPARK_TS" \
        '[{body:$b, createdAt:$t},
          {body:"<!-- champion:proposal-unescalated:abc123 --> blocker closed", createdAt:$u}]')"
    F="$RUN_DIR/fix/bot-unescalated"
    make_fixture "$F" '[]' "$BOT_COMMENTS" "$TL_UNPARK" "$REST_COMMENTS" 1
    OUT="$(run_guard "$F")"
    if [[ "$(result_of "$OUT" OPERATOR_RULED)" == "no" \
       && "$(result_of "$OUT" ESCALATE_UNREVISED)" == "yes" ]]; then
        pass "4c: an un-park recorded by Champion's own un-escalation marker is NOT read as a human ruling"
    else
        fail "4c: expected OPERATOR_RULED=no / ESCALATE_UNREVISED=yes, got:"$'\n'"$OUT"
    fi

    # --- 4d. A failed verdict re-read (no created_at) fails OPEN, not closed.
    REST_NO_TS="$(jq -nc --arg b "$(printf '%b' "$VBODY")" '[{id:99, body:$b}]')"
    F="$RUN_DIR/fix/no-verdict-ts"
    make_fixture "$F" '[]' "$VIEW_COMMENTS" "$TL_UNPARK" "$REST_NO_TS" 1
    OUT="$(run_guard "$F")"
    if [[ "$(result_of "$OUT" OPERATOR_RULED)" == "no" \
       && "$(result_of "$OUT" ESCALATE_UNREVISED)" == "yes" ]]; then
        pass "4d: an empty VERDICT_CREATED_AT (failed REST re-read) fails open — the ladder is not stood down on an API hiccup"
    else
        fail "4d: expected OPERATOR_RULED=no / ESCALATE_UNREVISED=yes, got:"$'\n'"$OUT"
    fi

    # --- 4e. An operator's loom:blocked park holds the pass before any tally.
    F="$RUN_DIR/fix/blocked-park"
    make_fixture "$F" '[{"name":"loom:blocked"}]' "$VIEW_COMMENTS" '[]' "$REST_COMMENTS" 1
    OUT="$(run_guard "$F")"
    if [[ "$(result_of "$OUT" ALREADY_ROUTED)" == "yes" \
       && "$(result_of "$OUT" ESCALATE_UNREVISED)" == "no" ]]; then
        pass "4e: a loom:blocked park sets ALREADY_ROUTED=yes and stops the pass before the escalation branch"
    else
        fail "4e: expected ALREADY_ROUTED=yes / ESCALATE_UNREVISED=no, got:"$'\n'"$OUT"
    fi

    # --- 4f. POSITIVE direction of the bot-vs-human attribution comparison
    # (4c is the negative-direction control): a human un-park strictly NEWER
    # than a prior bot un-escalation marker must still be read as a human
    # ruling on this revision, reusing BOT_COMMENTS from 4c (verdict comment
    # at VERDICT_TS, bot un-escalation marker at UNPARK_TS).
    HUMAN_AFTER_BOT_TS="2026-09-16T18:00:00Z"
    TL_HUMAN_AFTER_BOT="$(jq -nc --arg t "$HUMAN_AFTER_BOT_TS" \
        '[{event:"unlabeled", label:{name:"loom:operator-only"}, created_at:$t}]')"
    F="$RUN_DIR/fix/human-after-bot"
    make_fixture "$F" '[]' "$BOT_COMMENTS" "$TL_HUMAN_AFTER_BOT" "$REST_COMMENTS" 1
    OUT="$(run_guard "$F")"
    if [[ "$(result_of "$OUT" OPERATOR_RULED)" == "yes" \
       && "$(result_of "$OUT" ESCALATE_UNREVISED)" == "no" ]]; then
        pass "4f: a human un-park strictly newer than a prior bot un-escalation marker sets OPERATOR_RULED=yes and does NOT escalate"
    else
        fail "4f: expected OPERATOR_RULED=yes / ESCALATE_UNREVISED=no, got:"$'\n'"$OUT"
    fi
fi

# --- Test 5: WIRING -- Step 4 and the batch outcome table carry both flags
echo ""
echo "Test 5: Step 4's escalation preconditions and the batch outcome table name both flags"
if [[ -f "$PROMO" ]]; then
    STEP4="$(section_body "$PROMO" 'Step 4: Reject')"
    if [[ -z "$STEP4" ]]; then
        fail "could not locate the 'Step 4: Reject' section"
    else
        if grep -qF 'ALREADY_ROUTED=no' <<<"$STEP4" && grep -qF 'OPERATOR_RULED=no' <<<"$STEP4"; then
            pass "Step 4 names ALREADY_ROUTED=no and OPERATOR_RULED=no as escalation preconditions"
        else
            fail "Step 4's escalation condition does not name both ALREADY_ROUTED=no and OPERATOR_RULED=no"
        fi
        if grep -qE '^# *ALREADY_ROUTED .*loom:blocked' <<<"$STEP4" \
            && grep -qE '^# *OPERATOR_RULED ' <<<"$STEP4"; then
            pass "Step 4's input comment block documents both guard-computed flags"
        else
            fail "Step 4's input comment block does not document OPERATOR_RULED and the widened ALREADY_ROUTED"
        fi
    fi
    RULED_ROW="$(grep -E '^\| *Marker match, `OPERATOR_RULED=yes`' "$PROMO")"
    if [[ -n "$RULED_ROW" ]]; then
        pass "the batch outcome table has a Marker match / OPERATOR_RULED=yes row"
    else
        fail "the batch outcome table lacks an OPERATOR_RULED=yes row"
    fi
    ROUTED_ROW="$(grep -E '^\| *Marker match, `ALREADY_ROUTED=yes`' "$PROMO")"
    if [[ -n "$ROUTED_ROW" ]] && grep -q 'loom:blocked' <<<"$ROUTED_ROW" && grep -q 'loom:operator`' <<<"$ROUTED_ROW"; then
        pass "the ALREADY_ROUTED=yes row names loom:blocked and loom:operator alongside loom:operator-only"
    else
        fail "the ALREADY_ROUTED=yes row does not name all three hold labels"
    fi
else
    fail "champion-issue-promo.md not found at $PROMO"
fi

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
