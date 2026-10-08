#!/usr/bin/env bash
# test-champion-promotion-throughput.sh - Regression test for issue #10753,
# items 1 and 2: merges must not starve promotion, and the tier caps are
# configurable.
#
# THE FAILURE MODES THIS GUARDS AGAINST
#
#   pass order    champion.md ran promotion only "if no PRs remain". A repo
#                 with a held PR queue never reached it (rjwalters/loom on
#                 2026-10-07: 30 of 38 `loom:pr` PRs held by loom:operator).
#   fixed caps    The tier caps were literals (2 / 1 / 5) repeated in five
#                 files, so a fleet could not tune them.
#   self-pinning  The Tier 3 backlog gate counted every open tier:maintenance
#                 issue, including proposals still waiting for promotion, so
#                 waiting Tier 3 proposals blocked each other (19 counted vs
#                 2 promoted in rjwalters/loom; 19 curated issues held in
#                 2AMLogic/2am).
#
# WHAT THIS SUITE DOES
#
#   1. BEHAVIOUR -- the Backlog Balance Check block is EXTRACTED from the
#      shipped champion-issue-promo.md and EXECUTED against a stubbed `gh`:
#      defaults, env overrides, malformed values, and the promoted-only
#      occupant count. A copy with the old occupant filter is run as a
#      negative control, so the occupant assertions cannot pass vacuously.
#   2. WIRING -- the rate-limit prose names the env vars, no shipped file
#      still hardcodes the old literals, and champion.md runs promotion every
#      pass with the two slice knobs.
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

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

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

# Open issues the stub serves. Tier 3: #101-#103 are promoted and unheld (the
# only real occupants); #104/#109 are curated proposals, #105/#110 Hermit
# proposals and #106 a triage issue, all still waiting for promotion; #107
# and #108 are promoted but held (#7613).
cat >"$FIXTURE_DIR/issues.json" <<'EOF'
[
 {"number":101,"labels":[{"name":"tier:maintenance"},{"name":"loom:issue"}]},
 {"number":102,"labels":[{"name":"tier:maintenance"},{"name":"loom:building"}]},
 {"number":103,"labels":[{"name":"tier:maintenance"},{"name":"loom:issue"},{"name":"loom:curated"}]},
 {"number":104,"labels":[{"name":"tier:maintenance"},{"name":"loom:curated"}]},
 {"number":105,"labels":[{"name":"tier:maintenance"},{"name":"loom:hermit"}]},
 {"number":106,"labels":[{"name":"tier:maintenance"},{"name":"loom:triage"}]},
 {"number":107,"labels":[{"name":"tier:maintenance"},{"name":"loom:issue"},{"name":"loom:operator-only"}]},
 {"number":108,"labels":[{"name":"tier:maintenance"},{"name":"loom:building"},{"name":"loom:blocked"}]},
 {"number":109,"labels":[{"name":"tier:maintenance"},{"name":"loom:curated"}]},
 {"number":110,"labels":[{"name":"tier:maintenance"},{"name":"loom:hermit"}]},
 {"number":201,"labels":[{"name":"tier:goal-advancing"},{"name":"loom:issue"}]},
 {"number":301,"labels":[{"name":"tier:goal-supporting"},{"name":"loom:issue"}]},
 {"number":401,"labels":[{"name":"loom:issue"}]}
]
EOF

# `gh issue list --label X ... --jq EXPR`: filter the fixture by label, then
# apply the caller's OWN --jq expression, so the shipped filter is exercised.
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

BLOCK="$(first_bash_block "$PROMO_MD" "Backlog Balance Check")"
printf '%s\n' "$BLOCK" >"$FIXTURE_DIR/balance.sh"

# Run a balance-check script with the given env assignments ("VAR=value" ...).
run_balance() {
    local script="$1"; shift
    ( cd "$FIXTURE_DIR" && env -u LOOM_CHAMPION_TIER2_CAP -u LOOM_CHAMPION_TIER3_CAP \
        -u LOOM_CHAMPION_TIER3_BACKLOG_CAP "$@" PATH="$FIXTURE_DIR/bin:$PATH" \
        LOOM_TEST_ISSUES="$FIXTURE_DIR/issues.json" bash "$script" 2>&1 )
}

# ---------------------------------------------------------------------------
echo ""
echo "Test 1: the shipped Backlog Balance Check, executed against a stubbed gh"
if [[ "$BLOCK" != *"check_backlog_balance"* ]]; then
    fail "could not extract the Backlog Balance Check bash block from $PROMO_MD"
else
    OUT="$(run_balance balance.sh)"
    assert_contains "$OUT" "Caps this pass: TIER2_CAP=2 TIER3_CAP=1 TIER3_BACKLOG_CAP=5" \
        "unset env vars give the defaults 2 / 1 / 5"
    assert_contains "$OUT" "3 promoted (occupants: #101,#102,#103)" \
        "Tier 3 counts promoted, unheld issues only (#101-#103), not waiting proposals or held issues"
    assert_not_contains "$OUT" "TIER3_BACKLOG_FULL" \
        "3 promoted occupants are below the default backlog cap of 5"

    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER2_CAP=4 LOOM_CHAMPION_TIER3_CAP=0 LOOM_CHAMPION_TIER3_BACKLOG_CAP=12)"
    assert_contains "$OUT" "Caps this pass: TIER2_CAP=4 TIER3_CAP=0 TIER3_BACKLOG_CAP=12" \
        "each env var overrides its default, and 0 is honoured"

    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER3_BACKLOG_CAP=3)"
    assert_contains "$OUT" "TIER3_BACKLOG_FULL: 3 >= 3" \
        "a backlog cap at the promoted count blocks Tier 3 this pass"

    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER2_CAP=abc LOOM_CHAMPION_TIER3_CAP=-1 LOOM_CHAMPION_TIER3_BACKLOG_CAP=2.5)"
    assert_contains "$OUT" "Caps this pass: TIER2_CAP=2 TIER3_CAP=1 TIER3_BACKLOG_CAP=5" \
        "non-integer values fall back to the defaults instead of breaking the comparison"
    assert_not_contains "$OUT" "integer expression expected" \
        "a malformed cap never reaches a numeric test"

    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER2_CAP= LOOM_CHAMPION_TIER3_BACKLOG_CAP=)"
    assert_contains "$OUT" "TIER2_CAP=2 TIER3_CAP=1 TIER3_BACKLOG_CAP=5" \
        "an empty value falls back to the default"

    # Negative control: the pre-#10753 occupant filter (every unheld
    # tier:maintenance issue). The same fixture must read as pinned, or the
    # occupant assertions above prove nothing.
    sed 's/select(\[\.labels\[\]\.name\] | any(IN("loom:issue","loom:building"))) |//' \
        "$FIXTURE_DIR/balance.sh" >"$FIXTURE_DIR/balance-old.sh"
    if cmp -s "$FIXTURE_DIR/balance.sh" "$FIXTURE_DIR/balance-old.sh"; then
        fail "negative control: could not strip the promoted-only filter (has the block changed shape?)"
    else
        OUT="$(run_balance balance-old.sh)"
        assert_contains "$OUT" "8 promoted (occupants: #101,#102,#103,#104,#105,#106,#109,#110)" \
            "negative control: the old filter counts the 5 waiting proposals as occupants"
        assert_contains "$OUT" "TIER3_BACKLOG_FULL: 8 >= 5" \
            "negative control: the old filter pins the default cap on this fixture"
    fi
fi

# ---------------------------------------------------------------------------
echo ""
echo "Test 2: the rate-limit prose names the env vars; no shipped file hardcodes the old caps"
RATE="$(section_body "$PROMO_MD" "Tier-Aware Promotion Priority")"
for v in LOOM_CHAMPION_TIER2_CAP LOOM_CHAMPION_TIER3_CAP LOOM_CHAMPION_TIER3_BACKLOG_CAP; do
    assert_contains "$RATE" "$v" "Rate Limiting by Tier names $v"
done
for f in "$PROMO_MD" "$COMMON_MD" "$REF_MD" "$AGENT_MD"; do
    for lit in "fewer than 5" "up to 2 per iteration" "≤2 per iteration" "gated at 5"; do
        assert_doc_lacks "$f" "$lit" "$(basename "$f") no longer hardcodes '$lit'"
    done
done
STEP3C="$(section_body "$PROMO_MD" "Promote (All Criteria Pass)")"
assert_not_contains "$STEP3C" "below 5" "Step 3c's deferral template no longer hardcodes the backlog cap"
for v in LOOM_CHAMPION_PR_SLICE LOOM_CHAMPION_PROMOTION_SLICE LOOM_CHAMPION_TIER2_CAP \
         LOOM_CHAMPION_TIER3_CAP LOOM_CHAMPION_TIER3_BACKLOG_CAP; do
    assert_doc_contains "$THROUGHPUT_DOC" "\`$v\`" "promotion-throughput.md documents $v"
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
# shellcheck disable=SC2016  # the literal, unexpanded text is what the prompt must carry
assert_contains "$AUTO" '${LOOM_CHAMPION_PR_SLICE:-10}' "the PR pass pauses after LOOM_CHAMPION_PR_SLICE rows (default 10)"
# shellcheck disable=SC2016  # literal, as above
assert_contains "$AUTO" '${LOOM_CHAMPION_PROMOTION_SLICE:-3}' "promotion is bounded by LOOM_CHAMPION_PROMOTION_SLICE (default 3) while rows wait"
assert_contains "$AUTO" "whether or not PRs remain" "the promotion step does not depend on the PR queue emptying"
assert_contains "$AUTO" "Resume Priority 1 until every row is visited" "the PR pass still visits every row (no merge cap)"
BATCH="$(section_body "$PR_MERGE_MD" "PR Auto-Merge Batch Processing")"
assert_contains "$BATCH" "LOOM_CHAMPION_PR_SLICE" "champion-pr-merge.md's batch loop names the promotion pause"
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
