#!/usr/bin/env bash
# test-champion-epic-escalation-respects-human-hold.sh - Regression test for
# issues #7734, #7921 and #7965
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# champion-epic.md's "Idempotency Guard for Unrevised Epics" escalates an
# unrevised, repeatedly-rejected epic to `loom:operator-only` once
# LOOM_MAX_UNREVISED_EVALUATIONS is spent. Two incidents showed that ladder
# re-fighting a ruling a human had already made on the same, unchanged epic:
#
#   #7734  The only "a human owns this" signal the guard read was
#          `loom:operator-only` itself. An epic an operator had parked on
#          `loom:blocked` (or held with `loom:operator`) was tallied and
#          escalated like an untouched one, re-applying `loom:operator-only`
#          within hours of the park -- even though Step 0c's close branch
#          already refuses to act past exactly those three labels.
#   #7921  An operator un-parked an epic (removed `loom:operator-only`, added
#          no `loom:blocked`). Its label state was then byte-identical to
#          "never escalated", the body hash was unchanged, the tally was at
#          the cap -- so the very next pass re-escalated. Three times in one
#          day. No label can distinguish that case, so the fix reads the
#          issue TIMELINE: an `unlabeled loom:operator-only` event newer than
#          the current revision's own verdict comment is a ruling on this
#          exact body, and the guard stands down until the body is revised.
#   #7965  That un-park read is deliberately actor-blind, which is only safe
#          while NOTHING automated can remove `loom:operator-only` from an
#          epic -- a fact owned by a different file
#          (classify-dependency-block.sh gates every un-escalation on a
#          `champion:proposal-escalated` comment, which this ladder never
#          writes; it writes `champion:epic-escalated`). The guard now also
#          requires that marker's ABSENCE, and requires a non-empty
#          VERDICT_CREATED_AT -- an empty one meant a failed REST re-read was
#          silently treated as "every historical un-park is newer", standing
#          the ladder down on an API hiccup. Both unknown inputs now fail
#          OPEN (escalate), like the no-un-park-event case already did.
#
# Prose alone did not prevent either, so this suite makes both mechanisms
# statically checkable, modeled on test-champion-epic-verdict-marker-scope.sh:
#
#   1. LINT -- the guard's hold check names the SAME label set as Step 0c's
#      close-branch criterion 3, exercised against pass/fail fixtures so it
#      can never pass vacuously.
#   2. LINT -- the guard reads the timeline for `unlabeled loom:operator-only`,
#      compares it to the verdict comment's `created_at`, and sets
#      OPERATOR_RULED -- again with a fixture that lacks it.
#   3. WIRING -- the OPERATOR_RULED branch sits after the stray-marker check
#      and BEFORE the escalate branch, never tallies, never escalates; Step 4's
#      escalation condition requires both ALREADY_ROUTED=no and
#      OPERATOR_RULED=no; the outcome table carries both rows and routes the
#      ruled-on row through Step 0.5.
#   4. WIRING -- the escalation edit stays add-only (#6715), and the
#      maintainer notes the guard points at record both invariants.
#
# Hermetic: pure file reads plus a mktemp -d fixture dir. No forge/network.
# No `set -o pipefail` on purpose (#7790): nothing here needs it, and the
# early-exit consumers below must never turn a producer's SIGPIPE into a fail.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Role prompts are shipped (installed at .claude/commands/loom), so resolve the
# way each layout actually lays it out: the installed path first (consumer
# repos, and Loom's own dogfooded checkout), falling back to the defaults/
# source-tree path (a bare source checkout with no installed copy yet). See
# issue #6194 / #6241.
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi

CHAMPION_EPIC="$ROLE_DIR/champion-epic.md"
GUARD_NOTES="$ROLE_DIR/champion-epic-guard-invariants.md"

# The three labels Step 0c's close-branch criterion 3 refuses to close past.
# The guard must key on exactly this set -- one source of truth, checked
# against the shipped 0c line itself in Test 2 rather than trusted here.
EXPECTED_HOLD_LABELS=$'loom:blocked\nloom:operator\nloom:operator-only'

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Extract one section by a substring of its heading, from that heading up to
# the next heading of the SAME OR SHALLOWER level. Fenced code blocks are
# skipped when looking for headings so a shell comment is never mistaken for
# one. (Same helper as test-champion-epic-verdict-marker-scope.sh.)
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
# "Idempotency Guard for Unrevised Epics" section, with line numbers
# relative to the file so ordering can be asserted.
guard_block() {
    awk '
        /^## [^#]/ { in_guard = index($0, "Idempotency Guard for Unrevised Epics") > 0 }
        in_guard && /^```bash/ && !done { fence = 1; next }
        in_guard && fence && /^```$/ { done = 1; fence = 0 }
        in_guard && fence { printf "%d\t%s\n", FNR, $0 }
    ' "$1"
}

# Every `loom:*` label literally named in the line(s) that derive the guard's
# hold decision: the HELD_BY assignment and its jq continuation line, or -- on
# the pre-#7734 form -- the single-label ALREADY_ROUTED assignment. Sorted,
# unique, one per line.
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

# Every `loom:*` label named on Step 0c's criterion-3 line ("carries none of").
criterion3_labels_of() {
    section_body "$1" '0c. Close, or ask the operator to' \
        | grep -E 'carries none of' \
        | grep -oE 'loom:[a-z-]+' \
        | LC_ALL=C sort -u
}

# 0 if the guard block reads the timeline for an `unlabeled loom:operator-only`
# event, compares it against a verdict `created_at`, and derives OPERATOR_RULED
# from that comparison; 1 otherwise.
has_unpark_check() {
    local block
    block="$(guard_block "$1" | cut -f2-)"
    grep -qE 'issues/\$EPIC_NUMBER/timeline' <<<"$block" \
        && grep -qE '\.event == "unlabeled"' <<<"$block" \
        && grep -qE '\.label\.name == "loom:operator-only"' <<<"$block" \
        && grep -qE 'VERDICT_CREATED_AT=.*created_at' <<<"$block" \
        && grep -qE 'UNPARKED_AT.*>.*VERDICT_CREATED_AT' <<<"$block" \
        && grep -qE '^ *OPERATOR_RULED=yes' <<<"$block"
}

# The whole (possibly line-continued) `if` condition that guards
# `OPERATOR_RULED=yes`, flattened onto one line: the nearest preceding `if` line
# plus every line between it and the assignment. Used to assert #7965's two
# fail-open preconditions sit in THAT condition, not merely somewhere in the
# block.
unpark_condition_of() {
    guard_block "$1" | cut -f2- | awk '
        /^ *if / { cond = $0; collecting = 1; next }
        collecting && /^ *OPERATOR_RULED=yes/ { print cond; exit }
        collecting { cond = cond " " $0 }
    '
}

# 0 if the guard computes BOT_UNESCALATABLE from the proposal-escalation marker
# that classify-dependency-block.sh requires before it may un-escalate anything
# (#7965); 1 otherwise.
has_bot_unescalatable_probe() {
    local block
    block="$(guard_block "$1" | cut -f2-)"
    grep -qE '^ *BOT_UNESCALATABLE=' <<<"$block" \
        && grep -qF 'champion:proposal-escalated' <<<"$block"
}

echo "================================"
echo "test-champion-epic-escalation-respects-human-hold.sh (#7734 / #7921 / #7965)"
echo "================================"

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# A minimal guard in the PRE-fix shape: one label, no timeline read.
cat >"$FIXTURE_DIR/pre-fix.md" <<'EOF'
## Idempotency Guard for Unrevised Epics (`champion:epic-verdict:body-*`)

```bash
PRIOR_REJECTIONS=$(printf '%s\n' "$EPIC_JSON" | jq '[.comments[]] | length')
ALREADY_ROUTED=$(printf '%s\n' "$EPIC_JSON" | jq -e '.labels[] | select(.name=="loom:operator-only")' >/dev/null && echo yes || echo no)
SKIP_STREAK=0
if [ "$ALREADY_ROUTED" = "yes" ]; then
  echo "already routed"
elif [ "$PRIOR_REJECTIONS" -eq 0 ]; then
  echo "stray"
elif [ "$UNREVISED_EVALS" -ge "${LOOM_MAX_UNREVISED_EVALUATIONS:-2}" ]; then
  ESCALATE_UNREVISED=yes
fi
```

## Epic Approval Workflow

#### 0c. Close, or ask the operator to

3. The epic carries none of `loom:blocked`, `loom:operator-only`, `loom:operator`.
EOF

# The same guard in the POST-fix shape.
cat >"$FIXTURE_DIR/post-fix.md" <<'EOF'
## Idempotency Guard for Unrevised Epics (`champion:epic-verdict:body-*`)

```bash
PRIOR_REJECTIONS=$(printf '%s\n' "$EPIC_JSON" | jq '[.comments[]] | length')
HELD_BY=$(printf '%s\n' "$EPIC_JSON" | jq -r \
  '[.labels[].name | select(. == "loom:operator-only" or . == "loom:blocked" or . == "loom:operator")] | join(",")')
ALREADY_ROUTED=$([ -n "$HELD_BY" ] && echo yes || echo no)
OPERATOR_RULED=no
if [ "$ALREADY_ROUTED" = "yes" ]; then
  echo "held"
elif true; then
  VERDICT_CREATED_AT=$(printf '%s\n' "$VERDICT_COMMENT" | jq -r '.created_at // ""')
  UNPARKED_AT=$(gh api "repos/{owner}/{repo}/issues/$EPIC_NUMBER/timeline" --paginate \
    --jq '.[] | select(.event == "unlabeled" and .label.name == "loom:operator-only") | .created_at' \
    | sort | tail -n 1)
  if [ -n "$UNPARKED_AT" ] && [[ "$UNPARKED_AT" > "$VERDICT_CREATED_AT" ]]; then
    OPERATOR_RULED=yes
  fi
  if [ "$PRIOR_REJECTIONS" -eq 0 ]; then
    echo "stray"
  elif [ "$OPERATOR_RULED" = "yes" ]; then
    echo "ruled"
  elif [ "$UNREVISED_EVALS" -ge "${LOOM_MAX_UNREVISED_EVALUATIONS:-2}" ]; then
    ESCALATE_UNREVISED=yes
  fi
fi
```

## Epic Approval Workflow

#### 0c. Close, or ask the operator to

3. The epic carries none of `loom:blocked`, `loom:operator-only`, `loom:operator`.
EOF

# The #7965 shape: the same guard, with both fail-open preconditions added.
cat >"$FIXTURE_DIR/fail-open.md" <<'EOF'
## Idempotency Guard for Unrevised Epics (`champion:epic-verdict:body-*`)

```bash
OPERATOR_RULED=no
if true; then
  VERDICT_CREATED_AT=$(printf '%s\n' "$VERDICT_COMMENT" | jq -r '.created_at // ""')
  BOT_UNESCALATABLE=$(printf '%s\n' "$EPIC_JSON" | jq -e \
    '.comments[] | select(.body | contains("<!-- champion:proposal-escalated -->"))' >/dev/null && echo yes || echo no)
  UNPARKED_AT=$(gh api "repos/{owner}/{repo}/issues/$EPIC_NUMBER/timeline" --paginate \
    --jq '.[] | select(.event == "unlabeled" and .label.name == "loom:operator-only") | .created_at' \
    | sort | tail -n 1)
  if [ -n "$UNPARKED_AT" ] && [ -n "$VERDICT_CREATED_AT" ] && [ "$BOT_UNESCALATABLE" = "no" ] \
     && [[ "$UNPARKED_AT" > "$VERDICT_CREATED_AT" ]]; then
    OPERATOR_RULED=yes
  fi
fi
```
EOF

# --- Test 1: controls -- the lints discriminate the pre-fix shape from the fix
echo ""
echo "Test 1: lints flag the pre-#7734/#7921 guard shape and accept the fixed one"
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
if has_unpark_check "$FIXTURE_DIR/post-fix.md"; then
    pass "a guard with the timeline un-park check is accepted"
else
    fail "post-fix fixture was NOT accepted as having the un-park check"
fi

# --- Test 2: the shipped guard keys on exactly 0c criterion 3's label set (#7734)
echo ""
echo "Test 2: the guard's hold check names exactly the labels Step 0c criterion 3 names"
if [[ ! -f "$CHAMPION_EPIC" ]]; then
    fail "champion-epic.md not found at $CHAMPION_EPIC"
else
    GUARD_LABELS="$(hold_labels_of "$CHAMPION_EPIC")"
    OC_LABELS="$(criterion3_labels_of "$CHAMPION_EPIC")"
    if [[ -z "$OC_LABELS" ]]; then
        fail "could not read Step 0c criterion 3's 'carries none of' label set"
    elif [[ "$OC_LABELS" != "$EXPECTED_HOLD_LABELS" ]]; then
        fail "Step 0c criterion 3 no longer names the expected three labels:"$'\n'"$OC_LABELS"
    else
        pass "Step 0c criterion 3 names loom:blocked, loom:operator, loom:operator-only"
    fi
    if [[ "$GUARD_LABELS" == "$OC_LABELS" ]] && [[ -n "$GUARD_LABELS" ]]; then
        pass "the guard's HELD_BY / ALREADY_ROUTED check names the same set as 0c criterion 3"
    else
        fail "guard hold labels differ from 0c criterion 3's:"$'\n'"guard: $(tr '\n' ' ' <<<"$GUARD_LABELS")"$'\n'"0c:    $(tr '\n' ' ' <<<"$OC_LABELS")"
    fi
    if grep -qE '^ALREADY_ROUTED=\$\(\[ -n "\$HELD_BY" \]' <<<"$(guard_block "$CHAMPION_EPIC" | cut -f2-)"; then
        pass "ALREADY_ROUTED is derived from HELD_BY (any hold label), not from a single-label jq select"
    else
        fail "ALREADY_ROUTED is not derived from HELD_BY"
    fi
fi

# --- Test 3: the shipped guard reads the timeline un-park signal (#7921)
echo ""
echo "Test 3: the guard reads the timeline for an un-park newer than this revision's verdict"
if [[ -f "$CHAMPION_EPIC" ]]; then
    if has_unpark_check "$CHAMPION_EPIC"; then
        pass "guard reads unlabeled loom:operator-only from the timeline and compares it to VERDICT_CREATED_AT"
    else
        fail "guard is missing the timeline un-park check (timeline read / unlabeled / created_at comparison / OPERATOR_RULED=yes)"
    fi
    if grep -qE '^OPERATOR_RULED=no' <<<"$(guard_block "$CHAMPION_EPIC" | cut -f2-)"; then
        pass "OPERATOR_RULED is initialised to no alongside the other escalation inputs"
    else
        fail "OPERATOR_RULED is not initialised (Step 4 would read an unset variable)"
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 4: WIRING -- branch order: stray marker -> operator ruled -> escalate
echo ""
echo "Test 4: the OPERATOR_RULED branch sits between the stray-marker check and the escalate branch, and neither tallies nor escalates"
if [[ -f "$CHAMPION_EPIC" ]]; then
    BLOCK="$(guard_block "$CHAMPION_EPIC")"
    STRAY_LINE="$(grep -E $'\t *(el)?if \\[ "\\$PRIOR_REJECTIONS" -eq 0 \\]' <<<"$BLOCK" | cut -f1)"
    RULED_LINE="$(grep -E $'\t *elif \\[ "\\$OPERATOR_RULED" = "yes" \\]' <<<"$BLOCK" | cut -f1)"
    ESC_LINE="$(grep -E $'\t *elif \\[ "\\$UNREVISED_EVALS" -ge ' <<<"$BLOCK" | cut -f1)"
    STRAY_LINE="${STRAY_LINE%%$'\n'*}"; RULED_LINE="${RULED_LINE%%$'\n'*}"; ESC_LINE="${ESC_LINE%%$'\n'*}"
    if [[ -n "$STRAY_LINE" && -n "$RULED_LINE" && -n "$ESC_LINE" ]]; then
        if (( STRAY_LINE < RULED_LINE && RULED_LINE < ESC_LINE )); then
            pass "branch order is stray marker (line $STRAY_LINE) -> operator ruled ($RULED_LINE) -> escalate ($ESC_LINE)"
        else
            fail "branch order wrong: stray=$STRAY_LINE ruled=$RULED_LINE escalate=$ESC_LINE"
        fi
        # The ruled branch's own body: from its elif to the escalate elif.
        RULED_BODY="$(awk -F'\t' -v s="$RULED_LINE" -v e="$ESC_LINE" '$1 > s && $1 < e { print $2 }' <<<"$BLOCK")"
        if grep -qE 'ESCALATE_UNREVISED=yes|--method PATCH|--add-label|gh issue comment' <<<"$RULED_BODY"; then
            fail "the OPERATOR_RULED branch tallies, comments, labels, or escalates:"$'\n'"$RULED_BODY"
        else
            pass "the OPERATOR_RULED branch only logs and ends the pass (no tally, no comment, no label, no escalation)"
        fi
    else
        fail "could not locate all three branches (stray=$STRAY_LINE ruled=$RULED_LINE escalate=$ESC_LINE)"
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 5: WIRING -- Step 4's escalation condition requires both flags
echo ""
echo "Test 5: Step 4's escalation condition requires ALREADY_ROUTED=no and OPERATOR_RULED=no"
if [[ -f "$CHAMPION_EPIC" ]]; then
    STEP4="$(section_body "$CHAMPION_EPIC" 'Step 4: Reject')"
    if [[ -z "$STEP4" ]]; then
        fail "could not locate the 'Step 4: Reject' section"
    else
        if grep -qE 'ALREADY_ROUTED=no' <<<"$STEP4" && grep -qE 'OPERATOR_RULED=no' <<<"$STEP4" \
            && grep -qE 'PRIOR_REJECTIONS >= 1' <<<"$STEP4"; then
            pass "Step 4 names PRIOR_REJECTIONS >= 1, ALREADY_ROUTED=no and OPERATOR_RULED=no as escalation preconditions"
        else
            fail "Step 4's escalation condition does not name all of PRIOR_REJECTIONS >= 1 / ALREADY_ROUTED=no / OPERATOR_RULED=no"
        fi
        if grep -qE '^# *OPERATOR_RULED +—' <<<"$STEP4" && grep -qE '^# *ALREADY_ROUTED +—.*loom:blocked' <<<"$STEP4"; then
            pass "Step 4's input comment block documents both guard-computed flags"
        else
            fail "Step 4's input comment block does not document OPERATOR_RULED and the widened ALREADY_ROUTED"
        fi
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 6: WIRING -- the outcome table carries both rows
echo ""
echo "Test 6: the guard's outcome table has a ruled-on row (via Step 0.5) and names the hold labels on the routed row"
if [[ -f "$CHAMPION_EPIC" ]]; then
    GUARD="$(section_body "$CHAMPION_EPIC" 'Idempotency Guard for Unrevised Epics')"
    RULED_ROW="$(grep -E '^\| *Marker match, `OPERATOR_RULED=yes`' <<<"$GUARD")"
    if [[ -n "$RULED_ROW" ]] && grep -q 'Step 0\.5' <<<"$RULED_ROW"; then
        pass "outcome table has an OPERATOR_RULED=yes row that routes through Step 0.5"
    else
        fail "outcome table lacks an OPERATOR_RULED=yes row routed through Step 0.5"
    fi
    ROUTED_ROW="$(grep -E '^\| *`ALREADY_ROUTED=yes`' <<<"$GUARD")"
    if [[ -n "$ROUTED_ROW" ]] && grep -q 'loom:blocked' <<<"$ROUTED_ROW" && grep -q 'loom:operator`' <<<"$ROUTED_ROW"; then
        pass "outcome table's ALREADY_ROUTED=yes row names loom:blocked and loom:operator alongside loom:operator-only"
    else
        fail "outcome table's ALREADY_ROUTED=yes row does not name all three hold labels"
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 7: the escalation edit is still add-only (#6715, belt and braces)
echo ""
echo "Test 7: the escalation gh issue edit is still add-only"
if [[ -f "$CHAMPION_EPIC" ]]; then
    ESCALATION_BLOCK="$(awk '/ESCALATE_MARKER=/{flag=1} flag{print} flag && /^```$/ && NR>1 && seen{exit} flag && /^```$/{seen=1}' "$CHAMPION_EPIC")"
    if [[ -z "$ESCALATION_BLOCK" ]]; then
        fail "could not locate the escalation code block (ESCALATE_MARKER=... fence)"
    elif grep -q -- '--remove-label' <<<"$ESCALATION_BLOCK"; then
        fail "the escalation block now removes a label -- must stay add-only (#6715)"
    else
        pass "the escalation block is add-only"
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 8: the maintainer notes record both invariants and are linked
echo ""
echo "Test 8: champion-epic-guard-invariants.md records the #7734 and #7921 invariants and is linked from the guard"
if [[ ! -f "$GUARD_NOTES" ]]; then
    fail "maintainer notes not found at $GUARD_NOTES"
else
    NOTES="$(cat "$GUARD_NOTES")"
    if grep -q '#7734' <<<"$NOTES" && grep -q 'loom:blocked' <<<"$NOTES" && grep -q '`loom:operator`' <<<"$NOTES"; then
        pass "notes record the human-hold invariant (#7734) with all three labels"
    else
        fail "notes do not record the #7734 human-hold invariant"
    fi
    if grep -q '#7921' <<<"$NOTES" && grep -q 'OPERATOR_RULED' <<<"$NOTES" && grep -q 'created_at' <<<"$NOTES"; then
        pass "notes record the un-park invariant (#7921) keyed on the verdict's created_at"
    else
        fail "notes do not record the #7921 un-park invariant"
    fi
    if grep -q '#7965' <<<"$NOTES" && grep -q 'classify-dependency-block\.sh' <<<"$NOTES" \
        && grep -qF 'champion:proposal-escalated' <<<"$NOTES" \
        && grep -qF 'champion:epic-escalated' <<<"$NOTES"; then
        pass "notes record the cross-file un-escalation invariant (#7965) naming both markers and classify-dependency-block.sh"
    else
        fail "notes do not record the #7965 cross-file invariant (classify-dependency-block.sh gating un-escalation on champion:proposal-escalated, while this ladder writes champion:epic-escalated)"
    fi
    if [[ -f "$CHAMPION_EPIC" ]] && grep -q 'champion-epic-guard-invariants\.md' <<<"$(section_body "$CHAMPION_EPIC" 'Idempotency Guard for Unrevised Epics')"; then
        pass "the guard section links to the maintainer notes"
    else
        fail "the guard section does not link to champion-epic-guard-invariants.md"
    fi
fi

# --- Test 9: both unknown inputs fail OPEN, and a bot un-escalation cannot
#             masquerade as a human ruling (#7965)
echo ""
echo "Test 9: OPERATOR_RULED=yes additionally requires a non-empty VERDICT_CREATED_AT and BOT_UNESCALATABLE=no"
# Controls first, so a vacuous lint cannot pass: the #7921-era shape must be
# REJECTED, the #7965 shape ACCEPTED.
if has_bot_unescalatable_probe "$FIXTURE_DIR/post-fix.md"; then
    fail "pre-#7965 fixture (no BOT_UNESCALATABLE) was accepted as having the marker probe (lint is vacuous)"
else
    pass "a guard with no BOT_UNESCALATABLE probe is reported"
fi
if has_bot_unescalatable_probe "$FIXTURE_DIR/fail-open.md"; then
    pass "a guard that probes for the champion:proposal-escalated marker is recognised"
else
    fail "fail-open fixture was NOT accepted as having the BOT_UNESCALATABLE probe"
fi
PRE_COND="$(unpark_condition_of "$FIXTURE_DIR/post-fix.md")"
if grep -qF -- '-n "$VERDICT_CREATED_AT"' <<<"$PRE_COND"; then
    fail "pre-#7965 fixture's condition was read as requiring a non-empty VERDICT_CREATED_AT (lint is vacuous)"
else
    pass "the pre-#7965 condition is reported as lacking the non-empty VERDICT_CREATED_AT requirement"
fi

if [[ -f "$CHAMPION_EPIC" ]]; then
    if has_bot_unescalatable_probe "$CHAMPION_EPIC"; then
        pass "guard computes BOT_UNESCALATABLE from the champion:proposal-escalated marker"
    else
        fail "guard does not compute BOT_UNESCALATABLE from the champion:proposal-escalated marker (#7965)"
    fi
    COND="$(unpark_condition_of "$CHAMPION_EPIC")"
    if [[ -z "$COND" ]]; then
        fail "could not extract the if-condition guarding OPERATOR_RULED=yes"
    else
        if grep -qF -- '-n "$VERDICT_CREATED_AT"' <<<"$COND"; then
            pass "an empty/failed VERDICT_CREATED_AT read fails open (no stand-down) rather than matching any historical un-park"
        else
            fail "the OPERATOR_RULED condition does not require a non-empty VERDICT_CREATED_AT:"$'\n'"$COND"
        fi
        if grep -qF '"$BOT_UNESCALATABLE" = "no"' <<<"$COND"; then
            pass "a proposal-escalated epic (bot-un-escalatable) cannot set OPERATOR_RULED=yes"
        else
            fail "the OPERATOR_RULED condition does not require BOT_UNESCALATABLE=no:"$'\n'"$COND"
        fi
        if grep -qF -- '-n "$UNPARKED_AT"' <<<"$COND" \
            && grep -qF '"$UNPARKED_AT" > "$VERDICT_CREATED_AT"' <<<"$COND"; then
            pass "the original #7921 timestamp comparison is still part of the same condition"
        else
            fail "the #7921 un-park timestamp comparison left the OPERATOR_RULED condition:"$'\n'"$COND"
        fi
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
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
