#!/usr/bin/env bash
# test-champion-epic-verdict-marker-scope.sh - Regression test for issue #7666
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# champion-epic.md's "Idempotency Guard for Unrevised Epics" reads back a
# reserved marker -- `<!-- champion:epic-verdict:body-$BODY_HASH -->` -- to
# decide whether an epic has been rejected repeatedly without revision, and
# escalates to `loom:operator-only` once the tally reaches
# LOOM_MAX_UNREVISED_EVALUATIONS. That marker is documented as
# "written by, and read for, rejections ONLY".
#
# On rjwalters/kicad-tools#4431 a *passing* verdict ("epic passes, already
# decomposed, standing down") borrowed the marker's name, because the file had
# no template for that case. The guard counted the borrowed marker as repeated
# rejection-without-revision and parked a healthy, fully-decomposed epic in the
# operator queue -- which also removed it from Step 0's completion-first check,
# so it could no longer auto-close when its last child landed.
#
# Prose alone did not prevent the misuse, so this suite makes the rule
# statically checkable, modeled on
# defaults/scripts/tests/test-epic-label-preserved-on-escalation.sh:
#
#   1. LINT -- the reserved marker is POSTED from exactly one place in
#      champion-epic.md (Step 4's rejection template), exercised against
#      pass/fail fixtures so it can never pass vacuously.
#   2. LINT -- every literal mention of the marker token lives in an
#      allowlisted section (the guard, Step 0's non-counting carve-out, Step 4).
#   3. WIRING -- the dedicated stand-down path for an already-decomposed epic
#      exists (as the sibling file champion-epic-standdown.md, reached from
#      champion-epic.md's "Step 0.5" stub -- split out so champion-epic.md
#      stays under its markdown-token ratchet, #7725), uses its OWN marker
#      name, is deduped per body hash, and carries no escalation counter /
#      operator-only routing.
#   4. WIRING -- the guard's prose states the single-writer rule and the
#      "only NEEDS REVISION verdicts count" rule as hard constraints, and its
#      skip/escalate branches route through the stand-down first.
#
# Hermetic: pure file reads plus a mktemp -d fixture dir. No forge/network.

set -uo pipefail

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
# Step 0.5's body lives in a sibling prompt file, referenced from the
# "### Step 0.5" stub in champion-epic.md (same on-demand-read pattern
# champion.md uses for champion-epic.md itself).
CHAMPION_EPIC_STANDDOWN="$ROLE_DIR/champion-epic-standdown.md"

RESERVED_TOKEN='champion:epic-verdict:body-'
UMBRELLA_TOKEN='champion:epic-tracking-umbrella:body-'

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Emit "<section heading>|<line number>|<line text>" for every line that POSTS
# the reserved marker -- i.e. a line carrying the marker token (literal or via
# the $VERDICT_MARKER variable) that falls inside the opening lines of a
# `gh issue comment ... --body "..."` template. Markers are always emitted at
# the very top of a comment body in this file, so a 4-line window after the
# `gh issue comment` invocation covers every template shape it uses while
# keeping ordinary prose out of the result.
#
# Section attribution reports "<H2> >> <H3>" so a hit is greppable by either
# its owning `##` section (e.g. the guard, whose body is split across `###`
# sub-headings) or its `###` step (e.g. "Step 4: Reject"). `####` sub-headings
# are deliberately not tracked: a hit inside "#### 0c. ..." is attributed to
# its owning "### Step 0: ..." step, the granularity the allowlists are written
# at.
scan_posts() {
    awk -v tok="$RESERVED_TOKEN" '
        /^## [^#]/  { h2 = $0; h3 = "" }
        /^### [^#]/ { h3 = $0 }
        /gh issue comment/ { pending = 4 }
        {
            if (pending > 0) {
                if (index($0, tok) > 0 || $0 ~ /\$VERDICT_MARKER/) {
                    printf "%s >> %s|%d|%s\n", h2, h3, FNR, $0
                }
                pending--
            }
        }
    ' "$1"
}

# Emit "<H2> >> <H3>|<line number>|<line text>" for every literal mention of the
# reserved marker token, anywhere in the file (prose included).
scan_mentions() {
    awk -v tok="$RESERVED_TOKEN" '
        /^## [^#]/  { h2 = $0; h3 = "" }
        /^### [^#]/ { h3 = $0 }
        index($0, tok) > 0 { printf "%s >> %s|%d|%s\n", h2, h3, FNR, $0 }
    ' "$1"
}

# Extract one section by a substring of its heading, from that heading up to
# the next heading of the SAME OR SHALLOWER level (so a `##` section keeps its
# `###`/`####` sub-headings, and a `###` step keeps its `####` sub-steps).
section_body() {
    awk -v want="$2" '
        # Fenced code blocks contain shell comments ("# ..."), which must NOT
        # be mistaken for Markdown headings -- doing so truncates every section
        # at its first code comment.
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

echo "================================"
echo "test-champion-epic-verdict-marker-scope.sh (#7666)"
echo "================================"

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# --- Test 1: positive control -- the lint flags a non-Step-4 posting site
echo ""
echo "Test 1: lint flags a reserved-marker posting site outside Step 4"
cat >"$FIXTURE_DIR/bad.md" <<'EOF'
### Step 2.9: Stand Down When Already Decomposed

```bash
gh issue comment "$EPIC_NUMBER" --body "$VERDICT_MARKER
**Champion Review: Epic Passes Evaluation — Already Decomposed, No Action Needed**
"
```

### Step 4: Reject (One or More Criteria Fail)

```bash
gh issue comment <number> --body "$VERDICT_MARKER
**Champion Review: Epic Needs Revision**
"
```
EOF
BAD_HITS="$(scan_posts "$FIXTURE_DIR/bad.md" | grep -v 'Step 4: Reject' || true)"
if [[ -n "$BAD_HITS" ]]; then
    pass "a passing/stand-down template embedding \$VERDICT_MARKER is reported"
else
    fail "a passing/stand-down template embedding \$VERDICT_MARKER was NOT reported (lint is vacuous)"
fi

cat >"$FIXTURE_DIR/bad2.md" <<'EOF'
### Step 0.5: Tracking-Umbrella Stand-Down

```bash
gh issue comment "$EPIC_NUMBER" --body "<!-- champion:epic-verdict:body-$BODY_HASH -->
**Champion: Epic Already Decomposed**
"
```
EOF
if [[ -n "$(scan_posts "$FIXTURE_DIR/bad2.md")" ]]; then
    pass "a literal reserved-marker comment outside Step 4 is reported"
else
    fail "a literal reserved-marker comment outside Step 4 was NOT reported"
fi

# --- Test 2: negative control -- compliant forms are accepted
echo ""
echo "Test 2: lint accepts compliant forms (own marker name, prose mentions)"
cat >"$FIXTURE_DIR/good.md" <<'EOF'
### Step 0.5: Tracking-Umbrella Stand-Down

The reserved marker champion:epic-verdict:body-* belongs to Step 4 alone and is
never written here.

```bash
UMBRELLA_MARKER="<!-- champion:epic-tracking-umbrella:body-$BODY_HASH -->"
gh issue comment "$EPIC_NUMBER" --body "$UMBRELLA_MARKER
**Champion: Epic Already Decomposed — Tracking Only**
"
```
EOF
GOOD_HITS="$(scan_posts "$FIXTURE_DIR/good.md")"
if [[ -z "$GOOD_HITS" ]]; then
    pass "a stand-down template using its own marker name is accepted"
else
    fail "false positive on a compliant stand-down template:"$'\n'"$GOOD_HITS"
fi

# --- Test 3: the shipped file posts the reserved marker only from Step 4
echo ""
echo "Test 3: champion-epic.md posts the reserved marker only from Step 4's rejection branch"
if [[ ! -f "$CHAMPION_EPIC" ]]; then
    fail "champion-epic.md not found at $CHAMPION_EPIC"
else
    POST_HITS="$(scan_posts "$CHAMPION_EPIC")"
    # The sibling stand-down file has no Step 4, so ANY posting hit there is
    # an offender -- this is the exact site the #7666 misuse would live in.
    STANDDOWN_POST_HITS=""
    [[ -f "$CHAMPION_EPIC_STANDDOWN" ]] && STANDDOWN_POST_HITS="$(scan_posts "$CHAMPION_EPIC_STANDDOWN")"
    OFFENDERS="$(printf '%s\n%s\n' "$(printf '%s\n' "$POST_HITS" | grep -v 'Step 4: Reject' || true)" "$STANDDOWN_POST_HITS" | grep -v '^$' || true)"
    if [[ -z "$OFFENDERS" ]]; then
        pass "no posting site outside Step 4 (in champion-epic.md or champion-epic-standdown.md) emits the reserved marker"
    else
        fail "reserved marker posted outside Step 4:"$'\n'"$OFFENDERS"
    fi

    # ... and Step 4 really does still post it (the lint must not pass because
    # the marker vanished from the file altogether).
    if printf '%s\n' "$POST_HITS" | grep -q 'Step 4: Reject'; then
        pass "Step 4's rejection template still emits the reserved marker"
    else
        fail "Step 4's rejection template no longer emits the reserved marker (guard is inert)"
    fi
fi

# --- Test 4: every literal mention of the token is in an allowlisted section
echo ""
echo "Test 4: every mention of the reserved token lives in an allowlisted section"
if [[ -f "$CHAMPION_EPIC" ]]; then
    # Allowlist, by heading substring:
    #   - the guard itself (declares/reads the marker)
    #   - Step 0 (explicitly carves the marker OUT of its content-objection test)
    #   - Step 4 (the single writer)
    MENTION_OFFENDERS="$(scan_mentions "$CHAMPION_EPIC" \
        | grep -v 'Idempotency Guard for Unrevised Epics' \
        | grep -v 'Step 0: Completion-First Check' \
        | grep -v 'Step 4: Reject' || true)"
    # No section of the sibling stand-down file is allowlisted.
    STANDDOWN_MENTIONS=""
    [[ -f "$CHAMPION_EPIC_STANDDOWN" ]] && STANDDOWN_MENTIONS="$(scan_mentions "$CHAMPION_EPIC_STANDDOWN")"
    MENTION_OFFENDERS="$(printf '%s\n%s\n' "$MENTION_OFFENDERS" "$STANDDOWN_MENTIONS" | grep -v '^$' || true)"
    if [[ -z "$MENTION_OFFENDERS" ]]; then
        pass "all reserved-token mentions are in the guard / Step 0 / Step 4 (none in champion-epic-standdown.md)"
    else
        fail "reserved token mentioned in an unexpected section:"$'\n'"$MENTION_OFFENDERS"
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 5: the dedicated stand-down path exists and is escalation-free
echo ""
echo "Test 5: the tracking-umbrella stand-down exists, is body-hash deduped, and never escalates"
if [[ -f "$CHAMPION_EPIC" ]]; then
    # Wiring: champion-epic.md's Step 0.5 stub must point at the sibling file
    # by its installed path, the same way champion.md points at champion-epic.md.
    STUB="$(section_body "$CHAMPION_EPIC" 'Step 0.5')"
    if [[ -n "$STUB" ]] && grep -q 'champion-epic-standdown\.md' <<<"$STUB"; then
        pass "champion-epic.md's Step 0.5 stub references champion-epic-standdown.md"
    else
        fail "champion-epic.md has no 'Step 0.5' stub referencing champion-epic-standdown.md"
    fi

    STANDDOWN=""
    [[ -f "$CHAMPION_EPIC_STANDDOWN" ]] && STANDDOWN="$(cat "$CHAMPION_EPIC_STANDDOWN")"
    if [[ -z "$STANDDOWN" ]]; then
        fail "stand-down body not found at $CHAMPION_EPIC_STANDDOWN"
    else
        if grep -qF "$UMBRELLA_TOKEN" <<<"$STANDDOWN"; then
            pass "stand-down uses its own marker name ($UMBRELLA_TOKEN)"
        else
            fail "stand-down does not define its own marker ($UMBRELLA_TOKEN)"
        fi

        if grep -qF "$RESERVED_TOKEN" <<<"$STANDDOWN" || grep -q '\$VERDICT_MARKER' <<<"$STANDDOWN"; then
            fail "stand-down section references the reserved rejection marker"
        else
            pass "stand-down section never references the reserved rejection marker"
        fi

        if grep -q 'BODY_HASH' <<<"$STANDDOWN" && grep -q 'contains($m)' <<<"$STANDDOWN"; then
            pass "stand-down is deduped per body hash (marker match before posting)"
        else
            fail "stand-down is not deduped per body hash"
        fi

        # Wiring, not prose: the section's own invariants legitimately *name*
        # loom:operator-only in order to forbid it, so only an actual label
        # application or an actual counter assignment counts as a violation.
        if grep -qE -- '--add-label.*loom:operator-only' <<<"$STANDDOWN" \
            || grep -qE '(ESCALATE_UNREVISED|UNREVISED_EVALS|SKIP_STREAK|PRIOR_REJECTIONS)=' <<<"$STANDDOWN" \
            || grep -q 'LOOM_MAX_UNREVISED_EVALUATIONS' <<<"$STANDDOWN"; then
            fail "stand-down section wires an escalation counter or operator-only routing"
        else
            pass "stand-down section carries no escalation counter and no operator-only routing"
        fi
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 6: WIRING -- the guard states the constraints in prose
echo ""
echo "Test 6: the guard states the single-writer and rejection-only rules as hard constraints"
if [[ -f "$CHAMPION_EPIC" ]]; then
    GUARD="$(section_body "$CHAMPION_EPIC" 'Idempotency Guard for Unrevised Epics')"
    if [[ -z "$GUARD" ]]; then
        fail "could not locate the 'Idempotency Guard for Unrevised Epics' section"
    else
        if grep -qiE 'hard constraint' <<<"$GUARD" \
            && grep -qiE 'only by Step 4|exactly one writer|single-writer' <<<"$GUARD"; then
            pass "guard states the single-writer rule as a hard constraint"
        else
            fail "guard does not state the single-writer rule as a hard constraint"
        fi

        if grep -qiE 'Epic Needs Revision.*verdicts only|only .*rejections.*(count|feed)|never contribute' <<<"$GUARD"; then
            pass "guard states that only NEEDS REVISION verdicts feed the counter"
        else
            fail "guard does not state that only NEEDS REVISION verdicts feed the counter"
        fi

        if grep -qiE 'passing epic is never routed to the operator' <<<"$GUARD"; then
            pass "guard states that a passing epic is never routed to the operator"
        else
            fail "guard does not state that a passing epic is never routed to the operator"
        fi

        # The stray-marker backstop: a marker match with zero posted rejections
        # must not escalate. This is the mechanism, not just the prose.
        if grep -q 'PRIOR_REJECTIONS" -eq 0' <<<"$GUARD"; then
            pass "guard refuses to tally/escalate a marker match with PRIOR_REJECTIONS == 0"
        else
            fail "guard has no PRIOR_REJECTIONS == 0 stray-marker backstop"
        fi
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 7: WIRING -- skip/escalate branches route through Step 0.5 first
echo ""
echo "Test 7: the guard's marker-match outcomes run Step 0.5 before skipping or escalating"
if [[ -f "$CHAMPION_EPIC" ]]; then
    GUARD="$(section_body "$CHAMPION_EPIC" 'Idempotency Guard for Unrevised Epics')"
    ROWS="$(grep -E '^\| *(Marker match|No marker match)' <<<"$GUARD" || true)"
    if [[ -z "$ROWS" ]]; then
        fail "could not locate the guard's outcome table rows"
    else
        MISSING="$(grep -v 'Step 0\.5' <<<"$ROWS" || true)"
        if [[ -z "$MISSING" ]]; then
            pass "every marker-match outcome routes through Step 0.5"
        else
            fail "outcome row(s) do not route through Step 0.5:"$'\n'"$MISSING"
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
