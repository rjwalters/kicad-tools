#!/usr/bin/env bash
# test-champion-terminal-verdicts.sh - Regression test for #11860 (and the
# #10753 loop it reshapes): every Champion evaluation of a proposal is
# TERMINAL, a revise verdict sends the issue back to Curator as loom:triage,
# and loom:needs-revision is retired.
#
# THE FAILURE MODES THIS GUARDS AGAINST
#
#   left curated   Three paths ended an evaluation with the issue still
#                  loom:curated and no verdict label: the Step 3c capacity
#                  deferral, the unchanged-body silent skip (its
#                  champion:unrevised-skips tally), and the dependency defer.
#                  A step dispatcher that draws one item per step would redraw
#                  such an issue forever (#11860).
#   unbounded loop A revise verdict that re-enters triage must stay bounded by
#                  PRIOR_REJECTIONS, which counts posted NEEDS REVISION
#                  comments and never resets. A body-hash counter would never
#                  advance: Curator's revise always changes the hash.
#   live consumer  A retired label some prompt, script or daemon set still
#                  names keeps routing work by it after `labels migrate`.
#
# WHAT THIS SUITE DOES
#
#   1. BEHAVIOUR -- champion.md's Priority 2 query, Priority 3 loop and Priority 4 query are
#      EXTRACTED and EXECUTED against a stubbed `gh`.
#   2. BEHAVIOUR -- curator.md's send-back queue is extracted and executed, and
#      its unanswered-marker check is executed against the hash Champion's OWN
#      idempotency block computes for the same issue (so the two formulas
#      cannot drift apart).
#   3. BEHAVIOUR -- champion-issue-promo.md's idempotency block is executed:
#      an unchanged body sets UNREVISED=yes (no silent skip), and
#      PRIOR_REJECTIONS counts every NEEDS REVISION verdict across revisions.
#   4. WIRING -- Step 4's revise edit, the bound table, the batch outcome table
#      and the --issue verdict set name only the four terminal verdicts.
#   5. RETIREMENT -- the registry retires loom:needs-revision onto loom:triage
#      (also removing loom:curated) and no shipped consumer names it.
#
# Hermetic: file reads plus a mktemp -d fixture dir with local stubs. No forge,
# no network. Requires jq. No `set -o pipefail` on purpose (#7790).

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
TARGETED_MD="$ROLE_DIR/champion-targeted.md"
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
echo "test-champion-terminal-verdicts.sh (#11860)"
echo "================================"

for f in "$CHAMPION_MD" "$PROMO_MD" "$TARGETED_MD" "$CURATOR_MD" "$THROUGHPUT_DOC" "$STATE_DOC" "$LABELS_YML"; do
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
mkdir -p "$FIXTURE_DIR/bin" "$FIXTURE_DIR/view/bin" "$FIXTURE_DIR/view/.loom/scripts"

cat >"$FIXTURE_DIR/issues.json" <<'EOF'
[
 {"number":1,"title":"ready to evaluate","createdAt":"2026-10-03T00:00:00Z","labels":[{"name":"loom:curated"}]},
 {"number":3,"title":"already promoted","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:issue"}]},
 {"number":4,"title":"architect proposal","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:architect"}]},
 {"number":5,"title":"hermit blocked","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:hermit"},{"name":"loom:blocked"}]},
 {"number":6,"title":"auditor report","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:auditor"}]},
 {"number":7,"title":"being evaluated","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:evaluating"}]},
 {"number":8,"title":"blocked verdict","createdAt":"2026-09-30T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:blocked"}]},
 {"number":9,"title":"escalated","createdAt":"2026-09-30T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:operator"}]}
]
EOF

# `gh issue list --label X ... --jq EXPR`: filter the fixture by label, then
# apply the caller's OWN --jq expression. `--search` is the forge's shortlist
# and is ignored here: the jq filter is what this suite pins.
# `gh api --paginate ".../issues?labels=A,B&..."` (Champion's REST discovery,
# #12242): the fixture rows carrying every label, in REST shape (`created_at`),
# as one page; the caller's own `| jq` does the rest.
cat >"$FIXTURE_DIR/bin/gh" <<'STUB'
#!/usr/bin/env bash
if [ "$1" = api ]; then
  url=""; for a in "$@"; do case "$a" in repos/*) url="$a" ;; esac; done
  labels=$(printf '%s' "$url" | sed -n 's/.*[?&]labels=\([^&]*\).*/\1/p')
  jq -c --arg l "$labels" '($l | split(",")) as $want | [.[] | select([.labels[].name] as $have | all($want[]; . as $w | $have | index($w))) | . + {created_at: .createdAt} | del(.createdAt)]' "$LOOM_TEST_ISSUES"
  exit 0
fi
label=""; expr="."; limit=30
while [ $# -gt 0 ]; do
  case "$1" in
    --label=*) label="${1#--label=}"; shift ;;
    --label) label="$2"; shift 2 ;;
    --jq) expr="$2"; shift 2 ;;
    --limit) limit="$2"; shift 2 ;;
    --search) shift 2 ;;
    *) shift ;;
  esac
done
jq --arg l "$label" --argjson n "$limit" '[.[] | select([.labels[].name] | index($l))] | . as $a | ($a | sort_by(.createdAt) | reverse | .[:$n] | map(.number)) as $keep | $a | map(select(.number as $x | $keep | index($x)))' "$LOOM_TEST_ISSUES" | jq -r "$expr"
STUB
chmod +x "$FIXTURE_DIR/bin/gh"

run_query() {
    ( cd "$FIXTURE_DIR" && PATH="$FIXTURE_DIR/bin:$PATH" \
        LOOM_TEST_ISSUES="${2:-$FIXTURE_DIR/issues.json}" bash "$1" 2>&1 )
}
numbers_of() { grep -oE '^#[0-9]+' | tr '\n' ' ' | sed 's/ $//'; }

# ---------------------------------------------------------------------------
echo ""
echo "Test 1: Champion discovery hands only unheld, unclaimed proposals to evaluation"
section_body "Priority 2: Quality Issues Ready to Promote" <"$CHAMPION_MD" \
    | bash_block_with 'labels=loom:curated&' >"$FIXTURE_DIR/p2.sh"
section_body "Priority 3:" <"$CHAMPION_MD" \
    | bash_block_with 'for P in architect hermit auditor' >"$FIXTURE_DIR/p3.sh"
if [[ ! -s "$FIXTURE_DIR/p2.sh" || ! -s "$FIXTURE_DIR/p3.sh" ]]; then
    fail "could not extract champion.md's Priority 2 query and Priority 3 loop"
else
    assert_eq "#1" "$(run_query p2.sh | numbers_of)" \
        "Priority 2 returns only the evaluable curated issue (skips issue, evaluating, blocked, operator)"
    assert_eq "#4 #6" "$(run_query p3.sh | numbers_of)" \
        "Priority 3 returns the architect and auditor proposals and skips the blocked hermit one"
    assert_not_contains "$(cat "$FIXTURE_DIR/p2.sh" "$FIXTURE_DIR/p3.sh")" "needs-revision" \
        "neither query names the retired loom:needs-revision"
fi

echo ""
echo "Test 1b: Champion discovery orders proposals by level, then oldest first (#11103)"
cat >"$FIXTURE_DIR/levels.json" <<'EOF'
[
 {"number":31,"title":"plain oldest","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:curated"}]},
 {"number":32,"title":"important newer","createdAt":"2026-10-03T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:important"}]},
 {"number":33,"title":"important older","createdAt":"2026-10-02T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:important"}]},
 {"number":34,"title":"very important newest","createdAt":"2026-10-04T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:very-important"}]},
 {"number":35,"title":"retired star","createdAt":"2026-09-30T00:00:00Z","labels":[{"name":"loom:curated"},{"name":"loom:operator-priority"}]}
]
EOF
if [[ -s "$FIXTURE_DIR/p2.sh" ]]; then
    assert_eq "#34 #33 #32 #35 #31" "$(run_query p2.sh "$FIXTURE_DIR/levels.json" | numbers_of)" \
        "very-important, then important (oldest first), then the rest oldest first; a retired star carries no level"
fi

echo ""
echo "Test 1c: every curated proposal and epic has exactly one owner in non-item mode (#11922)"
# `--non-item` runs Priority 3 and 4 but never Priority 2. Under step dispatch
# a curated architect/hermit/auditor proposal is a `--issue` Champion step, so
# Priority 3 must skip it (else two owners). A curated EPIC is never a step
# (the classifier routes `loom:epic` before `loom:curated`), so Priority 4 must
# keep it (else no owner in non-item mode).
cat >"$FIXTURE_DIR/curated-proposals.json" <<'EOF2'
[
 {"number":40,"title":"plain architect","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:architect"}]},
 {"number":41,"title":"curated architect","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:architect"},{"name":"loom:curated"}]},
 {"number":42,"title":"curated hermit","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:hermit"},{"name":"loom:curated"}]},
 {"number":43,"title":"curated auditor","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:auditor"},{"name":"loom:curated"}]},
 {"number":44,"title":"plain epic","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:epic"}]},
 {"number":45,"title":"curated epic","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:epic"},{"name":"loom:curated"}]}
]
EOF2
section_body "Priority 4: Epic Proposals" <"$CHAMPION_MD" \
    | bash_block_with 'labels=loom:epic&' >"$FIXTURE_DIR/p4.sh"
NONITEM_RUNS="$(awk '/^## `--non-item`/{on=1;next} on&&/^## /{exit} on' "$TARGETED_MD")"
if [[ ! -s "$FIXTURE_DIR/p3.sh" || ! -s "$FIXTURE_DIR/p4.sh" ]]; then
    fail "could not extract champion.md's Priority 3 loop and Priority 4 query"
else
    assert_eq "#40" "$(run_query p3.sh "$FIXTURE_DIR/curated-proposals.json" | numbers_of)" \
        "Priority 3 skips curated architect, hermit and auditor proposals"
    assert_eq "#44 #45" "$(run_query p4.sh "$FIXTURE_DIR/curated-proposals.json" | numbers_of)" \
        "Priority 4 keeps a curated epic"
    # The non-item pass's queues, from champion-targeted.md's own table.
    assert_contains "$NONITEM_RUNS" "| Priority 4: epic proposals |" "--non-item runs Priority 4"
    assert_contains "$NONITEM_RUNS" "and Priority 2 (the curated query)" "--non-item skips Priority 2"
    NONITEM_SEEN=" $( { run_query p3.sh "$FIXTURE_DIR/curated-proposals.json"; run_query p4.sh "$FIXTURE_DIR/curated-proposals.json"; } | numbers_of) "
    for n in 41 42 43; do
        assert_not_contains "$NONITEM_SEEN" " #$n " "--non-item never evaluates curated proposal #$n (its --issue step owns it)"
    done
    assert_contains "$NONITEM_SEEN" " #45 " "--non-item still evaluates the curated epic #45 (no --issue step owns it)"
    assert_eq "#41 #42 #43 #45" "$(run_query p2.sh "$FIXTURE_DIR/curated-proposals.json" | numbers_of)" \
        "batch Priority 2 still sees every curated proposal and epic"
fi

# ---------------------------------------------------------------------------
echo ""
echo "Test 2: Curator's send-back queue (loom:triage), level then oldest, skipping claimed and frozen"
cat >"$FIXTURE_DIR/sendbacks.json" <<'EOF'
[
 {"number":20,"title":"important send-back","createdAt":"2026-10-06T00:00:00Z","labels":[{"name":"loom:important"},{"name":"loom:triage"}]},
 {"number":21,"title":"very important send-back","createdAt":"2026-10-07T00:00:00Z","labels":[{"name":"loom:very-important"},{"name":"loom:triage"}]},
 {"number":22,"title":"plain newer","createdAt":"2026-10-08T00:00:00Z","labels":[{"name":"loom:triage"}]},
 {"number":24,"title":"plain older","createdAt":"2026-10-01T00:00:00Z","labels":[{"name":"loom:triage"},{"name":"loom:architect"}]},
 {"number":25,"title":"claimed","createdAt":"2026-10-02T00:00:00Z","labels":[{"name":"loom:triage"},{"name":"loom:curating"}]},
 {"number":26,"title":"frozen","createdAt":"2026-10-02T00:00:00Z","labels":[{"name":"loom:triage"},{"name":"loom:frozen"}]},
 {"number":27,"title":"curated, not triage","createdAt":"2026-10-02T00:00:00Z","labels":[{"name":"loom:curated"}]}
]
EOF
section_body "Finding Work" <"$CURATOR_MD" \
    | bash_block_with 'Champion Review: NEEDS REVISION' >"$FIXTURE_DIR/curator-q.sh"
if [[ ! -s "$FIXTURE_DIR/curator-q.sh" ]]; then
    fail "could not extract curator.md's send-back queue from Finding Work"
else
    assert_contains "$(cat "$FIXTURE_DIR/curator-q.sh")" "--label loom:triage" "the send-back queue selects loom:triage"
    assert_eq "#21 #20 #24 #22" "$(run_query curator-q.sh "$FIXTURE_DIR/sendbacks.json" | numbers_of)" \
        "very-important, important, then the rest oldest first; claimed and frozen skipped"
fi
WORKFLOW="$(section_body "Priority 2: Triage queue" <"$CURATOR_MD")"
assert_contains "$WORKFLOW" "then Champion send-backs) first" \
    "Curator's workflow takes send-backs before Priority 1"

# ---------------------------------------------------------------------------
echo ""
echo "Test 3: Champion's idempotency block -- no silent skip, PRIOR_REJECTIONS counts every round"
VIEW="$FIXTURE_DIR/view"
cat >"$VIEW/bin/gh" <<'STUB'
#!/usr/bin/env bash
# REST reads answer from the fixture's REST comments; writes are no-ops;
# `gh issue view ... --jq EXPR` answers from the issue fixture.
[[ " $* " == *" --method "* ]] && exit 0
expr=""; path=""; sub="${1:-}"
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --jq) expr="$2"; shift 2 ;;
    -*) shift ;;
    *) [ -z "$path" ] && path="$1"; shift ;;
  esac
done
if [ "$sub" = "api" ]; then
  case "$path" in
    *comments*) src="$LOOM_TEST_FIX/rest-comments.json" ;;
    *) printf '[]\n' >"$LOOM_TEST_FIX/empty.json"; src="$LOOM_TEST_FIX/empty.json" ;;
  esac
else
  src="$LOOM_TEST_FIX/issue.json"
fi
if [ -n "$expr" ]; then jq -r "$expr" "$src"; else cat "$src"; fi
STUB
cat >"$VIEW/bin/gh-read" <<'STUB'
#!/usr/bin/env bash
cat "$LOOM_TEST_FIX/issue.json"
STUB
# `loom-daemon forge trusted-comments --fetch N [--gh-shape]`: the fixture's
# comments minus any whose author association is NONE (an outsider, #9548).
# Anything else (labels list) fails like an older daemon, so the literal
# fallbacks run.
cat >"$VIEW/bin/loom-daemon" <<'STUB'
#!/usr/bin/env bash
[ "${1:-} ${2:-}" = "forge trusted-comments" ] || exit 2
echo fetch >>"$LOOM_TEST_FIX/tc-calls"
# LOOM_TEST_REFETCH_FAIL: every fetch after the first fails (#11902's brief outage).
[ -n "${LOOM_TEST_REFETCH_FAIL:-}" ] && [ "$(wc -l <"$LOOM_TEST_FIX/tc-calls")" -gt 1 ] && exit 1
if [[ " $* " == *" --gh-shape "* ]]; then
  jq '[.comments[] | select((.authorAssociation // "MEMBER") != "NONE")]' "$LOOM_TEST_FIX/issue.json"
else
  jq '[.[] | select((.author_association // "MEMBER") != "NONE")]' "$LOOM_TEST_FIX/rest-comments.json"
fi
STUB
printf '#!/usr/bin/env bash\nexit 1\n' >"$VIEW/.loom/scripts/classify-dependency-block.sh"
chmod +x "$VIEW/bin/gh" "$VIEW/bin/gh-read" "$VIEW/bin/loom-daemon" "$VIEW/.loom/scripts/classify-dependency-block.sh"

section_body "Idempotency check (run BEFORE claiming" <"$PROMO_MD" \
    | bash_block_with 'BODY_HASH=' \
    | sed 's|^ISSUE_NUMBER=<number>$|ISSUE_NUMBER=7|' >"$VIEW/guard.sh"
cat >>"$VIEW/guard.sh" <<'EOF'
echo "RESULT BODY_HASH=${BODY_HASH:-}"
echo "RESULT REVISE_MARKER=${REVISE_MARKER:-}"
echo "RESULT UNREVISED=${UNREVISED:-}"
echo "RESULT PRIOR_REJECTIONS=${PRIOR_REJECTIONS:-}"
echo "RESULT SKIP_STREAK=${SKIP_STREAK:-unset}"
echo "RESULT FINDINGS=$(printf '%s\n' "${COMMENT_BODY:-}" | grep -c '^- ')"
echo "RESULT VERDICT_CREATED_AT=${VERDICT_CREATED_AT:-}"
EOF
# $1 fixture dir, $2 title, $3 body, $4 comments JSON array (gh issue view
# shape), $5 REST comments JSON array.
make_fixture() {
    mkdir -p "$1"
    jq -n --arg t "$2" --arg b "$3" --argjson c "$4" \
        '{title:$t, body:$b, labels:[{name:"loom:curated"}], comments:$c}' >"$1/issue.json"
    printf '%s\n' "$5" >"$1/rest-comments.json"
}
run_guard() {
    ( cd "$VIEW" && PATH="$VIEW/bin:$PATH" LOOM_TEST_FIX="$1" GH_READ="$VIEW/bin/gh-read" \
        bash ./guard.sh 2>&1 ) | grep '^RESULT '
}
result_of() { sed -n "s/^RESULT $2=//p" <<<"$1"; }

if ! grep -q 'BODY_HASH=' "$VIEW/guard.sh"; then
    fail "could not extract champion-issue-promo.md's idempotency block"
else
    TITLE="A proposal"
    BODY=$'Body text.\n\n## Revision (2026-10-14)\n- answered\n'
    F="$FIXTURE_DIR/fix/probe"
    make_fixture "$F" "$TITLE" "$BODY" '[]' '[]'
    OUT="$(run_guard "$F")"
    H="$(result_of "$OUT" BODY_HASH)"
    if [[ "$H" =~ ^[0-9a-f]{16}$ ]]; then pass "the block derives a 16-hex body hash ($H)"; else fail "no body hash: $OUT"; fi
    assert_eq "<!-- champion:revise:body-$H -->" "$(result_of "$OUT" REVISE_MARKER)" \
        "the revise marker is keyed on the same body hash"
    assert_eq "no" "$(result_of "$OUT" UNREVISED)" "a never-evaluated body is evaluated (UNREVISED=no)"
    assert_eq "unset" "$(result_of "$OUT" SKIP_STREAK)" "no silent-skip tally exists any more"

    # Three verdicts on three earlier bodies, plus one on THIS body.
    NR="Champion Review: NEEDS REVISION"
    COMMENTS="$(jq -nc --arg h "$H" --arg nr "$NR" '[
        {body: ("<!-- champion:proposal-verdict:body-1111111111111111 -->\n" + $nr), createdAt:"2026-10-01T00:00:00Z"},
        {body: ("<!-- champion:proposal-verdict:body-2222222222222222 -->\n" + $nr), createdAt:"2026-10-02T00:00:00Z"},
        {body: "an unrelated comment", createdAt:"2026-10-02T01:00:00Z"},
        {body: ("<!-- champion:proposal-verdict:body-" + $h + " -->\n<!-- champion:revise:body-" + $h + " -->\n" + $nr), createdAt:"2026-10-03T00:00:00Z"}]')"
    to_rest() { jq -c '[.[] | {id: 1, body, created_at: .createdAt, author_association: (.authorAssociation // "MEMBER")}]'; }
    REST="$(to_rest <<<"$COMMENTS")"
    F="$FIXTURE_DIR/fix/unrevised"
    make_fixture "$F" "$TITLE" "$BODY" "$COMMENTS" "$REST"
    OUT="$(run_guard "$F")"
    assert_eq "3" "$(result_of "$OUT" PRIOR_REJECTIONS)" \
        "PRIOR_REJECTIONS counts every NEEDS REVISION verdict across revisions (never reset by a new body)"
    assert_eq "yes" "$(result_of "$OUT" UNREVISED)" \
        "an unchanged, already-rejected body goes straight to Step 4 (UNREVISED=yes), never a silent skip"

    # --- #11902: the unrevised route lifts its findings from the trusted
    # comments the block already holds; a failed second fetch must not empty them.
    FINDINGS_C="$(jq -c --arg h "$H" '.[3].body += "\n- criterion 2: the scope names no file\n- criterion 5: no test plan"' <<<"$COMMENTS")"
    F7="$FIXTURE_DIR/fix/refetch-fails"
    make_fixture "$F7" "$TITLE" "$BODY" "$FINDINGS_C" "$(to_rest <<<"$FINDINGS_C")"
    OUT="$(LOOM_TEST_REFETCH_FAIL=1 run_guard "$F7")"
    assert_eq "yes" "$(result_of "$OUT" UNREVISED)" "refetch outage: still UNREVISED=yes"
    assert_eq "2" "$(result_of "$OUT" FINDINGS)" "refetch outage: COMMENT_BODY still carries the verdict's findings list"
    assert_eq "2026-10-03T00:00:00Z" "$(result_of "$OUT" VERDICT_CREATED_AT)" \
        "the verdict time comes from the held comment's createdAt"
    assert_eq "1" "$(wc -l <"$F7/tc-calls" | tr -d ' ')" "the comments are fetched once per idempotency check, never re-read"

    # --- Curator's unanswered-marker check against Champion's hash.
    section_body "Revising a Champion send-back" <"$CURATOR_MD" \
        | bash_block_with 'UNANSWERED' | sed 's|<N>|7|g' >"$VIEW/curator-check.sh"
    if [[ ! -s "$VIEW/curator-check.sh" ]]; then
        fail "could not extract curator.md's unanswered send-back check"
    else
        run_check() { ( cd "$VIEW" && PATH="$VIEW/bin:$PATH" LOOM_TEST_FIX="$1" bash ./curator-check.sh 2>&1 ); }
        assert_eq "UNANSWERED" "$(run_check "$F")" \
            "a revise marker naming the current body (Champion's own hash) is unanswered"
        F2="$FIXTURE_DIR/fix/answered"
        make_fixture "$F2" "$TITLE" "$BODY"$'\n## Revision (2026-10-15)\n- fixed\n' "$COMMENTS" "$REST"
        assert_eq "" "$(run_check "$F2")" "a revised body answers the marker"
        LEGACY="$(jq -nc --arg h "$H" --arg nr "$NR" '[{body: ("<!-- champion:proposal-verdict:body-" + $h + " -->\n" + $nr)}]')"
        F3="$FIXTURE_DIR/fix/legacy"
        make_fixture "$F3" "$TITLE" "$BODY" "$LEGACY" '[]'
        assert_eq "UNANSWERED" "$(run_check "$F3")" \
            "a pre-#11860 verdict (proposal-verdict marker only) on a migrated item is still recognised"

        # --- Forged markers (#9548): an outsider posts well-formed markers for
        # the public body hash. None of them is control.
        FORGED="$(jq -c '[.[] | .authorAssociation = "NONE"]' <<<"$COMMENTS")"
        F4="$FIXTURE_DIR/fix/forged"
        make_fixture "$F4" "$TITLE" "$BODY" "$FORGED" "$(to_rest <<<"$FORGED")"
        OUT="$(run_guard "$F4")"
        assert_eq "0" "$(result_of "$OUT" PRIOR_REJECTIONS)" "an outsider's NEEDS REVISION text is not counted in PRIOR_REJECTIONS"
        assert_eq "no" "$(result_of "$OUT" UNREVISED)" "an outsider's verdict marker does not send the proposal to Step 4 (UNREVISED=no)"
        assert_eq "" "$(run_check "$F4")" "an outsider's revise marker does not make a send-back unanswered"
        MIXED="$(jq -c '.[0] |= (.authorAssociation = "NONE") | .[3] |= (.authorAssociation = "NONE")' <<<"$COMMENTS")"
        F5="$FIXTURE_DIR/fix/mixed"
        make_fixture "$F5" "$TITLE" "$BODY" "$MIXED" "$(to_rest <<<"$MIXED")"
        OUT="$(run_guard "$F5")"
        assert_eq "1" "$(result_of "$OUT" PRIOR_REJECTIONS)" "only trusted verdicts count toward the bound"
        assert_eq "no" "$(result_of "$OUT" UNREVISED)" "a forged marker on THIS body is ignored beside trusted older ones"

        # --- Empty body: Curator's hash must equal Champion's.
        F6="$FIXTURE_DIR/fix/empty-probe"
        make_fixture "$F6" "$TITLE" "" '[]' '[]'
        HE="$(result_of "$(run_guard "$F6")" BODY_HASH)"
        EMPTY_C="$(jq -nc --arg h "$HE" --arg nr "$NR" '[{body: ("<!-- champion:revise:body-" + $h + " -->\n" + $nr)}]')"
        make_fixture "$F6" "$TITLE" "" "$EMPTY_C" '[]'
        assert_eq "UNANSWERED" "$(run_check "$F6")" "an empty-body send-back hashes the same on both sides"
    fi
fi

# ---------------------------------------------------------------------------
echo ""
echo "Test 4: every evaluation ends in one of the four terminal verdicts"
STEP4="$(section_body "Step 4: Reject" <"$PROMO_MD")"
REJECT="$(printf '%s\n' "$STEP4" | bash_block_with '**Champion Review: NEEDS REVISION**')"
if [[ -z "$REJECT" ]]; then
    fail "could not extract Step 4's NEEDS REVISION template"
else
    EDIT_LINE="$(printf '%s\n' "$REJECT" | grep -F 'gh issue edit')"
    assert_contains "$EDIT_LINE" '--remove-label "loom:evaluating"' "revise releases the claim"
    assert_contains "$EDIT_LINE" '--remove-label "loom:curated"' "revise removes loom:curated in the same edit"
    assert_contains "$EDIT_LINE" '--add-label "loom:triage"' "revise sets loom:triage in the same edit"
    # shellcheck disable=SC2016  # the literal variable names are what the template must carry
    assert_contains "$REJECT" '$VERDICT_MARKER' "the verdict carries the body-hash verdict marker"
    # shellcheck disable=SC2016
    assert_contains "$REJECT" '$REVISE_MARKER' "the verdict carries the revise marker Curator keys on"
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
assert_contains "$STEP4" "independently identified preference or authority question" \
    "operator routing after exhausted rounds requires a named preference/authority question"
LAST_ROW="$(printf '%s\n' "$STEP4" | grep -F 'disposition round already granted' || true)"
assert_contains "$LAST_ROW" "File a ranked decision" "after the disposition round the verdict is a decision, never a stand-down"
assert_not_contains "$LAST_ROW" "Stand down" "the non-terminal stand-down row is gone"
assert_contains "$STEP4" "loom-daemon operator-decision apply" "an escalation is filed as a ranked decision"
assert_not_contains "$STEP4" '--add-label "loom:operator' "Step 4 never hand-applies loom:operator"
DEFER_ROW="$(printf '%s\n' "$STEP4" | grep -E '^\| `0` \| `DEFER`' || true)"
assert_contains "$DEFER_ROW" "park-record apply" "the dependency-timing DEFER is a block verdict (park record)"
FAST="$(section_body "Dependency-Defer Fast Path" <"$PROMO_MD")"
assert_contains "$FAST" "park-record apply" "the Dependency-Defer Fast Path blocks instead of skipping"
# The batch outcome table: no row may end an evaluation without a verdict.
OUTCOMES="$(section_body "Issue Promotion Batch Processing" <"$PROMO_MD" | grep -E '^\| ' | grep -vE '^\| (Idempotency outcome|---)')"
assert_not_contains "$OUTCOMES" "Tally the skip" "no outcome row tallies a silent skip"
assert_not_contains "$OUTCOMES" "no tally" "no outcome row leaves the issue untouched for a tally-free skip"
assert_contains "$(section_body "Issue Promotion Batch Processing" <"$PROMO_MD")" "Every evaluation ends in exactly one verdict" \
    "the batch section states the terminal rule"
for retired in 'unrevised-skips:$BODY_HASH' 'SKIP_STREAK' 'Step 3c: Capacity Deferral' 'classify-capacity-defer.sh --issue'; do
    assert_doc_lacks "$PROMO_MD" "$retired" "champion-issue-promo.md no longer carries '$retired'"
done
VERDICTS="$(awk '/^### Promotion verdict set/{on=1;next} on&&/^## /{exit} on&&/^\| `/{print $2}' "$TARGETED_MD" | tr -d '`' | tr '\n' ' ')"
assert_eq "promoted revise blocked operator closed not-eligible " "$VERDICTS" \
    "--issue N: the four terminal verdicts, the premise-false close, and not-eligible (no deferred, no skipped)"

# ---------------------------------------------------------------------------
echo ""
echo "Test 5: the revise round trip is bounded by PRIOR_REJECTIONS"
# shellcheck disable=SC2016  # literal prompt text
assert_contains "$STEP4" 'If `PRIOR_REJECTIONS >= ${LOOM_MAX_UNREVISED_EVALUATIONS:-2}` or `UNREVISED=yes`' \
    "Step 4's bound is PRIOR_REJECTIONS (or an unrevised body), not a per-skip tally"
assert_contains "$STEP4" "ALREADY_ROUTED=no" "a held proposal never reaches the bound"
assert_doc_contains "$THROUGHPUT_DOC" "At most three Curator rounds" "the doc states the bound"
assert_doc_contains "$THROUGHPUT_DOC" "| 5th | 4 | Ranked decision |" "the doc's trace ends in a decision"
assert_doc_contains "$THROUGHPUT_DOC" "Exhausted rounds alone never make a factual finding a human call" \
    "factual findings still get agent rounds first"

# ---------------------------------------------------------------------------
echo ""
echo "Test 6: loom:needs-revision is retired, with no live consumer"
REVISE_SECTION="$(section_body "Revising a Champion send-back" <"$CURATOR_MD")"
assert_contains "$REVISE_SECTION" "## Revision" "Curator records each finding in a dated ## Revision section"
assert_contains "$REVISE_SECTION" "never only comment" "a comment alone is ruled out"
assert_contains "$REVISE_SECTION" "add \`loom:curated\`" "a revised issue goes back to loom:curated"
assert_contains "$REVISE_SECTION" "operator-decision" "a real PO-level call goes through operator-decision apply"
assert_contains "$REVISE_SECTION" "revision-exhausted" "Curator recognises the final round"
assert_contains "$REVISE_SECTION" "park-record apply" "a split parent is parked on its children"
assert_not_contains "$REVISE_SECTION" "\`loom:issue\`" "revision completion never promotes (#11103)"
assert_doc_lacks "$LABELS_YML" "loom:needs-revision" ".github/labels.yml no longer declares loom:needs-revision"
if [[ -f "$REGISTRY_JSON" ]]; then
    assert_eq "0" "$(jq '[.labels[] | select(.name == "loom:needs-revision")] | length' "$REGISTRY_JSON")" \
        "the registry no longer declares loom:needs-revision"
    assert_eq '["loom:triage",true,["loom:curated"]]' \
        "$(jq -c '.retired[] | select(.name == "loom:needs-revision") | [.replaced_by, .owned, .also_remove]' "$REGISTRY_JSON")" \
        "it is retired onto loom:triage, owned, and the migration also strips loom:curated"
    # No live consumer: shipped prompts, docs, scripts, skills and daemon
    # source. Tests, the registry's retired entry and the two docs that record
    # the retirement are the only places the name may appear.
    LIVE="$(cd "$REPO_ROOT" && grep -rlF 'loom:needs-revision' defaults loom-daemon/src .github 2>/dev/null \
        | grep -vE '^defaults/scripts/tests/|^defaults/labels\.json$|^defaults/docs/(promotion-throughput|label-state-machine)\.md$|_tests\.rs$|/tests\.rs$|/tests/' || true)"
    assert_eq "" "$LIVE" "no shipped consumer names loom:needs-revision"
else
    echo "  (registry and consumer checks skipped: $REGISTRY_JSON is source-tree only)"
fi
assert_doc_contains "$STATE_DOC" "is retired onto" "label-state-machine.md records the retirement"

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
