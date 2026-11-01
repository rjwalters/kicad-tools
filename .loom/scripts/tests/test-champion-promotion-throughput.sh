#!/usr/bin/env bash
# test-champion-promotion-throughput.sh - Regression test for issue #10753
# (merges must not starve promotion; NEEDS REVISION goes to Curator). The
# per-pass tier caps and promotion slices it once covered were retired by step
# dispatch (#11894) and deleted (#13272).
#
# THE FAILURE MODES THIS GUARDS AGAINST
#
#   pass order    champion.md ran promotion only "if no PRs remain". A repo
#                 with a held PR queue never reached it (rjwalters/loom on
#                 2026-10-07: 30 of 38 `loom:pr` PRs held by loom:operator).
#   self-pinning  The informational Backlog Balance Check once counted issues
#                 that cannot free a slot (waiting proposals, held issues: 19
#                 counted vs 2 promoted in rjwalters/loom).
#
# WHAT THIS SUITE DOES
#
#   1. BEHAVIOUR -- the Backlog Balance Check block is EXTRACTED from the
#      shipped champion-issue-promo.md and EXECUTED against a stubbed `gh`:
#      the per-level occupant counts. A copy without the held-issue filter is
#      run as a negative control, so the assertions cannot pass vacuously.
#   2. WIRING -- Promotion Order ranks the levels, no Champion file reads a
#      retired tier label, and the deleted cap/slice text stays deleted.
#   3. champion.md runs promotion every pass.
#
# Hermetic: file reads plus a mktemp -d fixture dir with a local `gh` stub.
# No forge, no network. Requires jq. No `set -o pipefail` on purpose (#7790).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Shipped files: resolve the installed layout first (consumer repos, and
# Loom's own dogfooded checkout), falling back to the defaults/ source tree
# (#6194 / #6241).
pick_dir() { if [[ -d "$REPO_ROOT/$1" ]]; then echo "$REPO_ROOT/$1"; else echo "$REPO_ROOT/$2"; fi; }
ROLE_DIR="$(pick_dir .claude/commands/loom defaults/.claude/commands/loom)"
AGENT_DIR="$(pick_dir .claude/agents defaults/.claude/agents)"
DOCS_DIR="$(pick_dir .loom/docs defaults/docs)"

CHAMPION_MD="$ROLE_DIR/champion.md"
PROMO_MD="$ROLE_DIR/champion-issue-promo.md"
COMMON_MD="$ROLE_DIR/champion-common.md"
REF_MD="$ROLE_DIR/champion-reference.md"
PR_MERGE_MD="$ROLE_DIR/champion-pr-merge.md"
AGENT_MD="$AGENT_DIR/loom-champion.md"
THROUGHPUT_DOC="$DOCS_DIR/promotion-throughput.md"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3"; echo "    Expected to contain: '$2'"; echo "    Actual: '$1'"; fi
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

# Print one section, from a heading containing $2 up to the next heading of
# the same or shallower level. Fenced code is skipped when looking for
# headings, so a shell comment is never mistaken for one.
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

# The first ```bash block of the section named $2.
first_bash_block() {
    section_body "$1" "$2" | awk '
        /^```bash/ && !done { fence = 1; next }
        fence && /^```$/ { done = 1; fence = 0 }
        fence { print }
    '
}

echo "================================"
echo "test-champion-promotion-throughput.sh (#10753)"
echo "================================"

for f in "$CHAMPION_MD" "$PROMO_MD" "$COMMON_MD" "$REF_MD" "$PR_MERGE_MD" "$AGENT_MD" "$THROUGHPUT_DOC"; do
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

# Open issues the stub serves. No priority level: #101-#103 are promoted and
# unheld (the only real default-level occupants); #104 (curated) and #105
# (Hermit) still wait for promotion; #107/#108 are promoted but held (#7613);
# #109 is held by a retired name `labels migrate` has not moved yet (#11795).
# #201 is very important; #301/#302 are important.
cat >"$FIXTURE_DIR/issues.json" <<'JSON'
[
 {"number":101,"labels":[{"name":"loom:issue"}]},
 {"number":102,"labels":[{"name":"loom:building"}]},
 {"number":103,"labels":[{"name":"loom:issue"},{"name":"loom:curated"}]},
 {"number":104,"labels":[{"name":"loom:curated"}]},
 {"number":105,"labels":[{"name":"loom:hermit"}]},
 {"number":107,"labels":[{"name":"loom:issue"},{"name":"loom:operator"}]},
 {"number":108,"labels":[{"name":"loom:building"},{"name":"loom:blocked"}]},
 {"number":109,"labels":[{"name":"loom:issue"},{"name":"loom:operator-only"}]},
 {"number":201,"labels":[{"name":"loom:issue"},{"name":"loom:very-important"}]},
 {"number":301,"labels":[{"name":"loom:issue"},{"name":"loom:important"}]},
 {"number":302,"labels":[{"name":"loom:building"},{"name":"loom:important"}]}
]
JSON

# `gh issue list --label X ... [--jq EXPR]`: filter the fixture by label, then
# apply the caller's OWN --jq expression (if any), so the shipped filter runs.
cat >"$FIXTURE_DIR/bin/gh" <<'STUB'
#!/usr/bin/env bash
label=""; expr="."
while [ $# -gt 0 ]; do
  case "$1" in
    --label=*) label="${1#--label=}"; shift ;;
    --label) label="$2"; shift 2 ;;
    --jq) expr="$2"; shift 2 ;;
    *) shift ;;
  esac
done
jq --arg l "$label" '[.[] | select([.labels[].name] | index($l))]' "$LOOM_TEST_ISSUES" | jq -r "$expr"
STUB
chmod +x "$FIXTURE_DIR/bin/gh"

# `loom-daemon labels list --property hold --with-retired`: the registry's hold
# set, retired names included (#11795) -- the real daemon's answer when one is
# built, else that set spelled out.
REAL_DAEMON="${LOOM_TEST_REAL_DAEMON:-${LOOM_DAEMON_BIN:-}}"
if [[ -n "$REAL_DAEMON" && -x "$REAL_DAEMON" ]]; then
    "$REAL_DAEMON" labels list --property hold --with-retired >"$FIXTURE_DIR/hold-labels"
else
    printf '%s\n' loom:blocked loom:operator loom:frozen loom:operator-only loom:operator-decision \
        loom:operator-mechanical loom:operator-blocked loom:needs-capability >"$FIXTURE_DIR/hold-labels"
fi
printf '#!/usr/bin/env bash\n[ "$*" = "labels list --property hold --with-retired" ] || exit 2\ncat "%s"\n' \
    "$FIXTURE_DIR/hold-labels" >"$FIXTURE_DIR/bin/loom-daemon"
chmod +x "$FIXTURE_DIR/bin/loom-daemon"

BLOCK="$(first_bash_block "$PROMO_MD" "Backlog Balance Check")"
printf '%s\n' "$BLOCK" >"$FIXTURE_DIR/balance.sh"

# Run a balance-check script with the given env assignments ("VAR=value" ...).
run_balance() {
    local script="$1"; shift
    ( cd "$FIXTURE_DIR" && env "$@" PATH="$FIXTURE_DIR/bin:$PATH" \
        LOOM_TEST_ISSUES="$FIXTURE_DIR/issues.json" bash "$script" 2>&1 )
}

# ---------------------------------------------------------------------------
echo ""
echo "Test 1: the shipped Backlog Balance Check, executed against a stubbed gh"
if [[ "$BLOCK" != *"check_backlog_balance"* ]]; then
    fail "could not extract the Backlog Balance Check bash block from $PROMO_MD"
else
    OUT="$(run_balance balance.sh)"
    assert_contains "$OUT" "default (no level):  3 promoted (occupants: #101,#102,#103)" \
        "the default level counts promoted, unheld, unlevelled issues only (#101-#103)"
    assert_contains "$OUT" "loom:important:      2 promoted (occupants: #301,#302)" \
        "loom:important issues are counted under their own level"
    assert_contains "$OUT" "loom:very-important: 1 promoted" \
        "loom:very-important issues are counted under their own level"
    assert_not_contains "$OUT" "Caps this pass" "the balance check prints no cap line"

    # Negative control: drop the held-issue exclusion (#7613). The same
    # fixture must then count the two held issues, or the occupant
    # assertions above prove nothing.
    sed 's/select(\[\.labels\[\]\.name\] | any(IN(\$h\[\])) | not) |//' \
        "$FIXTURE_DIR/balance.sh" >"$FIXTURE_DIR/balance-old.sh"
    if cmp -s "$FIXTURE_DIR/balance.sh" "$FIXTURE_DIR/balance-old.sh"; then
        fail "negative control: could not strip the held-issue filter (has the block changed shape?)"
    else
        OUT="$(run_balance balance-old.sh)"
        assert_contains "$OUT" "default (no level):  6 promoted (occupants: #101,#102,#103,#107,#108,#109)" \
            "negative control: without the filter the held issues count as occupants"
    fi
fi

# ---------------------------------------------------------------------------
echo ""
echo "Test 2: promotion order is wired; no Champion file reads a retired tier label"
RATE="$(section_body "$PROMO_MD" "Promotion Order")"
assert_contains "$RATE" "\`loom:very-important\`, then \`loom:important\`" \
    "Promotion Order ranks very-important, then important"
assert_contains "$RATE" "oldest first within a level" "Promotion Order breaks ties oldest first"
assert_not_contains "$(section_body "$PROMO_MD" "Backlog Balance Check")" "LOOM_CHAMPION_TIER2_CAP" \
    "the Backlog Balance Check no longer reads a tier cap"
assert_contains "$(section_body "$PROMO_MD" "Setting Priority")" "label the blocker too" \
    "Champion labels a priority proposal's blocker itself (no daemon inheritance)"
assert_contains "$(section_body "$PROMO_MD" "Setting Priority")" "never set a level just to get past a capacity cap" \
    "a priority level is never set to dodge a cap"
STEP3="$(section_body "$PROMO_MD" "Promote (All Criteria Pass)")"
assert_not_contains "$STEP3" "Step 3c: Capacity Deferral" "the Step 3c capacity deferral is gone: a verdict is terminal (#11860)"
for f in "$PROMO_MD" "$COMMON_MD" "$REF_MD" "$AGENT_MD"; do
    for lit in "fewer than 5" "up to 2 per iteration" "≤2 per iteration" "gated at 5"; do
        assert_doc_lacks "$f" "$lit" "$(basename "$f") does not hardcode '$lit'"
    done
done
for f in "$CHAMPION_MD" "$PROMO_MD" "$COMMON_MD" "$REF_MD" "$AGENT_MD"; do
    for lit in "tier:goal" "tier:maintenance" "Rate Limiting by Tier"; do
        assert_doc_lacks "$f" "$lit" "$(basename "$f") no longer names '$lit'"
    done
done
for f in "$CHAMPION_MD" "$PROMO_MD" "$COMMON_MD" "$PR_MERGE_MD" "$AGENT_MD" "$THROUGHPUT_DOC"; do
    for lit in LOOM_CHAMPION_PR_SLICE LOOM_CHAMPION_PROMOTION_SLICE LOOM_CHAMPION_TIER2_CAP; do
        assert_doc_lacks "$f" "$lit" "$(basename "$f") no longer names $lit (#13272)"
    done
done
assert_doc_contains "$THROUGHPUT_DOC" "loom-daemon-start.sh" \
    "promotion-throughput.md says where a fleet sets the knobs"

# ---------------------------------------------------------------------------
echo ""
echo "Test 3: promotion runs every pass, even while PRs remain"
assert_doc_lacks "$CHAMPION_MD" "If no PRs need merging" "champion.md Priority 2 no longer waits for an empty PR queue"
assert_doc_lacks "$CHAMPION_MD" "If no PRs remain" "champion.md's autonomous steps no longer wait for an empty PR queue"
assert_doc_lacks "$COMMON_MD" "If no PRs, check" "champion-common.md no longer waits for an empty PR queue"
AUTO="$(section_body "$CHAMPION_MD" "Autonomous Operation")"
assert_contains "$AUTO" "whether or not PRs remain" "the promotion step does not depend on the PR queue emptying"
assert_contains "$AUTO" "until every row is visited (drain the full queue)" "the PR pass still visits every row (no merge cap)"
BATCH="$(section_body "$PR_MERGE_MD" "PR Auto-Merge Batch Processing")"
assert_contains "$BATCH" "drain the full queue" "champion-pr-merge.md still drains the full queue"

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
