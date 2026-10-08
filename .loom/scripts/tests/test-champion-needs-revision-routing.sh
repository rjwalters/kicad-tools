#!/usr/bin/env bash
# test-champion-needs-revision-routing.sh - Regression test for issue #10753,
# item 3: a Champion "NEEDS REVISION" verdict goes to Curator, not to the
# operator.
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# A NEEDS REVISION verdict used to wait for someone to edit the body, and one
# unchanged re-check later Champion escalated with a bare loom:operator-only
# hold. example-org/tool-repo#202 was escalated an hour after its first verdict
# on findings ("split per leaf", "finish the registry audit") that are agent
# work. Under #10001 the operator is for product-level calls only.
#
# WHAT THIS SUITE DOES
#
#   1. BEHAVIOUR -- champion.md's Priority 2 query and Priority 3 loop are
#      EXTRACTED and EXECUTED against a stubbed `gh`: an issue carrying
#      loom:needs-revision is never handed to evaluation. A copy without the
#      exclusion is run as a negative control.
#   2. BEHAVIOUR -- curator.md's loom:needs-revision queue is extracted and
#      executed: oldest first, skipping an issue another Curator has claimed.
#   3. WIRING -- Step 4 routes in the same edit that releases the claim, keeps
#      the findings list the first bullet list of the verdict (the shape
#      classify-dependency-block.sh reads), and at the bound files a ranked
#      decision through `operator-decision apply`, never a bare hold.
#   4. WIRING -- Curator's procedure, the label registry and the docs.
#
# Hermetic: file reads plus a mktemp -d fixture dir with a local `gh` stub.
# No forge, no network. Requires jq. No `set -o pipefail` on purpose (#7790).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Shipped files: installed layout first, then the defaults/ source tree
# (#6194 / #6241).
pick_dir() { if [[ -d "$REPO_ROOT/$1" ]]; then echo "$REPO_ROOT/$1"; else echo "$REPO_ROOT/$2"; fi; }
ROLE_DIR="$(pick_dir .claude/commands/loom defaults/.claude/commands/loom)"
DOCS_DIR="$(pick_dir .loom/docs defaults/docs)"

CHAMPION_MD="$ROLE_DIR/champion.md"
PROMO_MD="$ROLE_DIR/champion-issue-promo.md"
CURATOR_MD="$ROLE_DIR/curator.md"
THROUGHPUT_DOC="$DOCS_DIR/promotion-throughput.md"
STATE_DOC="$DOCS_DIR/label-state-machine.md"
LABELS_YML="$REPO_ROOT/.github/labels.yml"
# The registry is source-tree only; consumer repos carry the generated yml.
REGISTRY_JSON="$REPO_ROOT/defaults/labels.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3"; echo "    Expected: '$1'"; echo "    Actual:   '$2'"; fi
}
assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3"; echo "    Expected to contain: '$2'"; fi
}
assert_not_contains() {
    if [[ "$1" != *"$2"* ]]; then pass "$3"; else fail "$3"; echo "    Expected NOT to contain: '$2'"; fi
}
assert_doc_contains() {
    if grep -qF -- "$2" "$1"; then pass "$3"; else fail "$3 (missing literal in $1: $2)"; fi
}
assert_doc_lacks() {
    if grep -qF -- "$2" "$1"; then fail "$3 (found literal in $1: $2)"; else pass "$3"; fi
}

# One section (read from stdin), from a heading containing $1 to the next
# heading of the same or shallower level; fenced code is skipped when looking
# for headings.
section_body() {
    awk -v want="$1" '
        /^```/ { fence = !fence }
        !fence && /^#+ / {
            n = 0
            while (substr($0, n + 1, 1) == "#") n++
            if (inside && n <= lvl) inside = 0
            if (!inside && index($0, want) > 0) { inside = 1; lvl = n }
        }
        inside { print }
    '
}

# The first ```bash block (read from stdin) that contains the literal $1.
bash_block_with() {
    awk -v want="$1" '
        /^```bash/ && !done { fence = 1; buf = ""; next }
        fence && /^```$/ { fence = 0; if (index(buf, want) > 0) { printf "%s", buf; done = 1 } ; next }
        fence { buf = buf $0 "\n" }
    '
}

echo "================================"
echo "test-champion-needs-revision-routing.sh (#10753)"
echo "================================"

for f in "$CHAMPION_MD" "$PROMO_MD" "$CURATOR_MD" "$THROUGHPUT_DOC" "$STATE_DOC" "$LABELS_YML"; do
    if [[ ! -f "$f" ]]; then
        echo "FATAL: shipped file not found: $f" >&2
        exit 1
    fi
done
if ! command -v jq >/dev/null 2>&1; then
    echo "FATAL: jq is required by this suite but was not found on PATH" >&2
    exit 1
fi

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
mkdir -p "$FIXTURE_DIR/bin"

cat >"$FIXTURE_DIR/issues.json" <<'EOF'
[
 {"number":1,"title":"ready to evaluate","createdAt":"2026-10-03T00:00:00Z","labels":[{"name":"loom:curated"}]},
 {"number":2,"title":"out at Curator","createdAt":"2026-10-02T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:needs-revision"}]},
 {"number":3,"title":"already promoted","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:issue"}]},
 {"number":4,"title":"architect proposal","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:architect"}]},
 {"number":5,"title":"hermit out at Curator","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:hermit"},{"name":"loom:needs-revision"}]},
 {"number":6,"title":"auditor report","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:auditor"}]},
 {"number":7,"title":"being evaluated","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:evaluating"}]},
 {"number":11,"title":"newer revision","createdAt":"2026-10-05T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:needs-revision"}]},
 {"number":12,"title":"claimed revision","createdAt":"2026-09-30T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:needs-revision"},{"name":"loom:curating"}]}
]
EOF

# `gh issue list --label X ... --jq EXPR`: filter the fixture by label, then
# apply the caller's OWN --jq expression.
cat >"$FIXTURE_DIR/bin/gh" <<'STUB'
#!/usr/bin/env bash
label=""; expr="."; limit=30
while [ $# -gt 0 ]; do
  case "$1" in
    --label=*) label="${1#--label=}"; shift ;;
    --label) label="$2"; shift 2 ;;
    --jq) expr="$2"; shift 2 ;;
    --limit) limit="$2"; shift 2 ;;
    *) shift ;;
  esac
done
jq --arg l "$label" --argjson n "$limit" '[.[] | select([.labels[].name] | index($l))] | . as $a | ($a | sort_by(.createdAt) | reverse | .[:$n] | map(.number)) as $keep | $a | map(select(.number as $x | $keep | index($x)))' "$LOOM_TEST_ISSUES" | jq -r "$expr"
STUB
chmod +x "$FIXTURE_DIR/bin/gh"

run_query() {
    ( cd "$FIXTURE_DIR" && PATH="$FIXTURE_DIR/bin:$PATH" \
        LOOM_TEST_ISSUES="$FIXTURE_DIR/issues.json" bash "$1" 2>&1 )
}
numbers_of() { grep -oE '^#[0-9]+' | tr '\n' ' ' | sed 's/ $//'; }

# ---------------------------------------------------------------------------
echo ""
echo "Test 1: Champion discovery never hands a loom:needs-revision issue to evaluation"
section_body "Priority 2: Quality Issues Ready to Promote" <"$CHAMPION_MD" \
    | bash_block_with '--label="loom:curated"' >"$FIXTURE_DIR/p2.sh"
section_body "Priority 3:" <"$CHAMPION_MD" \
    | bash_block_with 'for P in architect hermit auditor' >"$FIXTURE_DIR/p3.sh"
if [[ ! -s "$FIXTURE_DIR/p2.sh" || ! -s "$FIXTURE_DIR/p3.sh" ]]; then
    fail "could not extract champion.md's Priority 2 query and Priority 3 loop"
else
    assert_eq "#1" "$(run_query p2.sh | numbers_of)" \
        "Priority 2 returns only the evaluable curated issue (skips needs-revision, issue, evaluating)"
    P3="$(run_query p3.sh)"
    assert_eq "#4 #6" "$(printf '%s\n' "$P3" | numbers_of)" \
        "Priority 3 returns the architect and auditor proposals and skips the hermit one out at Curator"
    assert_contains "$P3" "#4 architect proposal [architect]" "the loop tags each row with its proposal label"
    sed 's/,"loom:needs-revision"//' "$FIXTURE_DIR/p2.sh" >"$FIXTURE_DIR/p2-old.sh"
    if cmp -s "$FIXTURE_DIR/p2.sh" "$FIXTURE_DIR/p2-old.sh"; then
        fail "negative control: Priority 2's filter does not name loom:needs-revision at all"
    else
        assert_eq "#1 #2 #11 #12" "$(run_query p2-old.sh | numbers_of)" \
            "negative control: without the exclusion, every curated issue out at Curator is re-evaluated"
    fi
fi

# ---------------------------------------------------------------------------
echo ""
echo "Test 2: Curator drains loom:needs-revision, oldest first, skipping a claimed one"
section_body "Finding Work" <"$CURATOR_MD" \
    | bash_block_with 'label loom:needs-revision' >"$FIXTURE_DIR/curator-q.sh"
if [[ ! -s "$FIXTURE_DIR/curator-q.sh" ]]; then
    fail "could not extract curator.md's loom:needs-revision queue from Finding Work"
else
    assert_eq "#5 #2 #11" "$(run_query curator-q.sh | numbers_of)" \
        "the queue lists every unclaimed loom:needs-revision issue, oldest first (#12 is claimed)"
fi
WORKFLOW="$(section_body "Priority 2: Triage queue" <"$CURATOR_MD")"
assert_contains "$WORKFLOW" "then \`loom:needs-revision\`) first" \
    "Curator's workflow takes the revision queue before Priority 1"

# ---------------------------------------------------------------------------
echo ""
echo "Test 2b: a starred loom:needs-revision issue takes the revision path, never the star promotion"
cat >"$FIXTURE_DIR/stars.json" <<'EOF'
[
 {"number":20,"title":"starred rejected","createdAt":"2026-10-06T00:00:00Z","labels":[{"name":"loom:operator-priority"},{"name":"loom:curated"},{"name":"loom:needs-revision"}]},
 {"number":21,"title":"level 2 rejected","createdAt":"2026-10-07T00:00:00Z","labels":[{"name":"loom:operator-high-priority"},{"name":"loom:curated"},{"name":"loom:needs-revision"}]},
 {"number":22,"title":"inherited rejected","createdAt":"2026-10-08T00:00:00Z","labels":[{"name":"loom:high-priority-inherited"},{"name":"loom:curated"},{"name":"loom:needs-revision"}]},
 {"number":23,"title":"starred fresh","createdAt":"2026-10-09T00:00:00Z","labels":[{"name":"loom:operator-priority"}]},
 {"number":24,"title":"unstarred revision","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:needs-revision"}]},
 {"number":25,"title":"starred disposition round","createdAt":"2026-10-02T00:00:00Z","labels":[{"name":"loom:operator-priority"},{"name":"loom:curated"},{"name":"loom:needs-revision"},{"name":"loom:curating"}]}
]
EOF
section_body "Priority 0: Starred" <"$CURATOR_MD" \
    | bash_block_with 'operator-high-priority' >"$FIXTURE_DIR/curator-star.sh"
run_stars() {
    ( cd "$FIXTURE_DIR" && PATH="$FIXTURE_DIR/bin:$PATH" \
        LOOM_TEST_ISSUES="$FIXTURE_DIR/stars.json" bash "$1" 2>&1 )
}
if [[ ! -s "$FIXTURE_DIR/curator-star.sh" || ! -s "$FIXTURE_DIR/curator-q.sh" ]]; then
    fail "could not extract curator.md's star query or revision queue"
else
    assert_eq "#23" "$(run_stars curator-star.sh | numbers_of)" \
        "the star query lists only the fresh starred issue, never a starred loom:needs-revision one (all three priority labels)"
    sed 's/,"loom:needs-revision"//' "$FIXTURE_DIR/curator-star.sh" >"$FIXTURE_DIR/curator-star-old.sh"
    assert_eq "#21 #22 #20 #23" "$(run_stars curator-star-old.sh | numbers_of)" \
        "negative control: without the exclusion, starred rejected proposals reach promotion"
    assert_eq "#21 #22 #20 #24" "$(run_stars curator-q.sh | numbers_of)" \
        "the revision queue lists level 2 (direct, inherited), then the star, then the rest, skipping the claimed #25"
fi
# Backlog > 30 (gh default page) and priority levels: the queue must see every row, level 2 first.
jq -n '
  [range(0; 31) | {number: (100 + .), title: "rev \(.)",
                   createdAt: "2026-09-\(. + 1 | tostring | if length < 2 then "0" + . else . end)T00:00:00Z",
                   labels: [{name: "loom:curated"}, {name: "loom:needs-revision"}]}]
  | (.[0].labels += [{name: "loom:operator-priority"}])   # oldest, ordinary star, outside newest 30
  + [{number: 200, title: "newer direct level 2", createdAt: "2026-10-05T00:00:00Z",
      labels: [{name: "loom:operator-high-priority"}, {name: "loom:needs-revision"}]},
     {number: 201, title: "newest inherited level 2", createdAt: "2026-10-06T00:00:00Z",
      labels: [{name: "loom:high-priority-inherited"}, {name: "loom:needs-revision"}]}]
' >"$FIXTURE_DIR/backlog.json"
if [[ -s "$FIXTURE_DIR/curator-q.sh" ]]; then
    BACKLOG="$( cd "$FIXTURE_DIR" && PATH="$FIXTURE_DIR/bin:$PATH" LOOM_TEST_ISSUES="$FIXTURE_DIR/backlog.json" bash curator-q.sh 2>&1 | numbers_of )"
    assert_eq "#200 #201 #100 #101" "$(printf '%s\n' "$BACKLOG" | cut -d' ' -f1-4)" \
        "level 2 (direct and inherited) precedes an older ordinary star, which precedes the rest oldest-first"
    assert_contains " $BACKLOG " " #100 " "an old starred request outside the newest 30 is still listed (explicit --limit)"
    sed 's/ --limit 500//' "$FIXTURE_DIR/curator-q.sh" >"$FIXTURE_DIR/curator-q-nolimit.sh"
    NOLIMIT="$( cd "$FIXTURE_DIR" && PATH="$FIXTURE_DIR/bin:$PATH" LOOM_TEST_ISSUES="$FIXTURE_DIR/backlog.json" bash curator-q-nolimit.sh 2>&1 | numbers_of )"
    assert_not_contains " $NOLIMIT " " #100 " "negative control: without --limit the oldest request falls off the default 30-row page"
fi
assert_contains "$(section_body "Revising" <"$CURATOR_MD")" "a starred non-epic issue also gets \`loom:issue\`" \
    "revision completion defines the post-revision star behaviour in the same edit that clears loom:needs-revision"

# ---------------------------------------------------------------------------
echo ""
echo "Test 3: Step 4 routes to Curator and never applies a bare operator hold"
STEP4="$(section_body "Step 4: Reject" <"$PROMO_MD")"
REJECT="$(printf '%s\n' "$STEP4" | bash_block_with '**Champion Review: NEEDS REVISION**')"
if [[ -z "$REJECT" ]]; then
    fail "could not extract Step 4's NEEDS REVISION template"
else
    EDIT_LINE="$(printf '%s\n' "$REJECT" | grep -F 'gh issue edit')"
    assert_contains "$EDIT_LINE" '--remove-label "loom:evaluating"' "the verdict releases the claim"
    assert_contains "$EDIT_LINE" '--add-label "loom:needs-revision"' \
        "the same edit routes the issue to Curator"
    HEAD_PART="$(printf '%s\n' "$REJECT" | awk '/Champion Review: NEEDS REVISION/ { exit } { print }')"
    if printf '%s\n' "$HEAD_PART" | grep -qE '^[[:space:]]*[-*][[:space:]]'; then
        fail "a bullet precedes the verdict header, so it would be read as a finding"
    else
        pass "no bullet precedes the verdict header (markers stay HTML comments)"
    fi
    AFTER_ACTIONS="$(printf '%s\n' "$REJECT" | awk '/^\*\*Recommended actions/ { on = 1 } on { print }')"
    assert_contains "$AFTER_ACTIONS" "Routed to Curator" \
        "the routing note follows **Recommended actions, outside the findings list"
fi
assert_contains "$STEP4" "<!-- champion:revision-exhausted -->" "the bound grants one final Curator round, keyed on a marker"
assert_contains "$STEP4" "<!-- champion:revision-disposition -->" \
    "exhausted rounds with only factual findings get an agent-owned disposition round"
assert_contains "$STEP4" "every remaining finding is factual" \
    "the exhausted-round row distinguishes factual findings from a preference call"
assert_contains "$STEP4" "independently identified preference or authority question" \
    "operator routing after exhausted rounds requires a named preference/authority question"
assert_doc_contains "$THROUGHPUT_DOC" "Exhausted rounds alone never make a factual finding a human call" \
    "the doc keeps factual exhausted rounds with agents"
STANDDOWN_ROW="$(printf '%s\n' "$STEP4" | grep -F 'disposition round already granted' || true)"
assert_contains "$STANDDOWN_ROW" '--remove-label "loom:evaluating"' \
    "the exhausted stand-down releases the loom:evaluating claim it took"
assert_not_contains "$STANDDOWN_ROW" "no label change" \
    "the exhausted stand-down no longer leaves the claim behind"
assert_doc_contains "$THROUGHPUT_DOC" "Stand down: release the \`loom:evaluating\` claim" \
    "the doc's stand-down row matches the claim release"
assert_contains "$STEP4" "loom-daemon operator-decision apply" "an escalation is filed as a ranked decision"
assert_contains "$STEP4" "--also-label loom:operator-only" "the decision keeps the operator-only skip"
assert_not_contains "$STEP4" '--add-label "loom:operator-only' "Step 4 never hand-applies loom:operator-only"
assert_not_contains "$STEP4" 'SUB_KIND' "the hand-picked sub-kind is gone"
ESCALATE="$(printf '%s\n' "$STEP4" | bash_block_with 'operator-decision apply')"
# shellcheck disable=SC2016  # the literal variable name is what the template must carry
assert_contains "$ESCALATE" '$VERDICT_MARKER' \
    "the escalation comment stamps the post-apply verdict marker (keeps OPERATOR_RULED working)"
# shellcheck disable=SC2016  # literal, as above
assert_contains "$ESCALATE" '$ESCALATE_MARKER' "the escalation keeps the marker Pass 0 and #7650 read"
NOT_PROMOTE="$(section_body "When NOT to Promote" <"$PROMO_MD")"
assert_contains "$NOT_PROMOTE" "loom:needs-revision" "a handed-in issue at Curator is not promoted or evaluated"

# ---------------------------------------------------------------------------
echo ""
echo "Test 4: Curator's procedure, the label registry and the docs"
REVISE="$(section_body "Revising \`loom:needs-revision\`" <"$CURATOR_MD")"
if [[ -z "$REVISE" ]]; then
    fail "curator.md has no 'Revising \`loom:needs-revision\`' section"
else
    assert_contains "$REVISE" "## Revision" "Curator records each finding in a dated ## Revision section"
    assert_contains "$REVISE" "never only comment" "a comment alone is ruled out (it leaves the body hash unchanged)"
    assert_contains "$REVISE" "operator-decision" "a real PO-level call goes through operator-decision apply"
    assert_contains "$REVISE" "revision-exhausted" "Curator recognises the final round"
    assert_contains "$REVISE" "park-record apply" "a split parent is parked on its children, not returned to Champion as a tracker"
fi
assert_doc_lacks "$CURATOR_MD" "Choose the sub-kind before posting" \
    "curator.md no longer points at the removed sub-kind step"
assert_doc_contains "$LABELS_YML" "- name: loom:needs-revision" ".github/labels.yml carries loom:needs-revision"
if [[ -f "$REGISTRY_JSON" ]]; then
    ENTRY="$(jq -c '.labels[] | select(.name == "loom:needs-revision")
        | [.applied_by, .removed_by, .park, .skip, .hold, .operator_gate, .human_gated]' "$REGISTRY_JSON")"
    assert_eq '["Champion","Curator",false,false,false,false,false]' "$ENTRY" \
        "the registry entry is Champion-applied, Curator-removed, and changes no derived label set"
else
    echo "  (registry checks skipped: $REGISTRY_JSON is source-tree only)"
fi
assert_doc_contains "$STATE_DOC" "loom:needs-revision" "label-state-machine.md documents the label"
assert_doc_contains "$THROUGHPUT_DOC" "At most three Curator rounds" "the doc states the bound"

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed."
