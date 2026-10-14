#!/usr/bin/env bash
# test-champion-promotion-throughput.sh - Regression test for issue #10753,
# items 1 and 2 (merges must not starve promotion; the promotion caps are
# configurable), with the caps mapped onto priority level by #11512.
#
# THE FAILURE MODES THIS GUARDS AGAINST
#
#   pass order    champion.md ran promotion only "if no PRs remain". A repo
#                 with a held PR queue never reached it (rjwalters/loom on
#                 2026-10-07: 30 of 38 `loom:pr` PRs held by loom:operator).
#   fixed caps    The caps were literals (2 / 1 / 5) repeated in five files,
#                 so a fleet could not tune them.
#   self-pinning  The backlog gate once counted issues that cannot free a slot
#                 (waiting proposals, held issues: 19 counted vs 2 promoted in
#                 rjwalters/loom), so the cap pinned itself.
#   stale caps    #11103 retired the `tier:*` labels the caps keyed on; #11512
#                 maps them by population: very-important and important are
#                 uncapped, no level = tier2Cap per pass with NO backlog gate.
#                 Keyed on no level, the old Tier 3 backlog gate counted 46
#                 promoted issues against a cap of 5 and froze promotion.
#
# WHAT THIS SUITE DOES
#
#   1. BEHAVIOUR -- the Backlog Balance Check block is EXTRACTED from the
#      shipped champion-issue-promo.md and EXECUTED against a stubbed `gh`:
#      defaults, env overrides, malformed values, and the per-level occupant
#      counts. A copy without the held-issue filter is run as a negative
#      control, so the occupant assertions cannot pass vacuously.
#   2. WIRING -- the rate-limit prose maps each level to its cap and names the
#      env vars, no Champion file reads a retired tier label, and the docs
#      record the knobs.
#   3. champion.md runs promotion every pass with the two slice knobs.
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
    assert_contains "$OUT" "Caps this pass: DEFAULT_CAP=2 " \
        "unset env var gives the no-level per-pass default of 2 (the old Tier 2 cap)"
    assert_contains "$OUT" "default (no level):  3 promoted (occupants: #101,#102,#103)" \
        "the default level counts promoted, unheld, unlevelled issues only (#101-#103)"
    assert_contains "$OUT" "loom:important:      2 promoted (occupants: #301,#302)" \
        "loom:important issues are counted under their own level"
    assert_contains "$OUT" "loom:very-important: 1 promoted" \
        "loom:very-important issues are counted under their own level"
    assert_not_contains "$OUT" "BACKLOG" \
        "no backlog gate: the old Tier 3 gate has no population under the level mapping"

    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER2_CAP=0)"
    assert_contains "$OUT" "Caps this pass: DEFAULT_CAP=0 " "the env var overrides the default, and 0 is honoured"

    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER3_CAP=0 LOOM_CHAMPION_TIER3_BACKLOG_CAP=1)"
    assert_contains "$OUT" "Caps this pass: DEFAULT_CAP=2 " \
        "the unused tier3 knobs change nothing (a backlog of 3 vs a cap of 1 does not gate)"
    assert_not_contains "$OUT" "BACKLOG" "a tiny tier3BacklogCap never gates no-level promotion"

    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER2_CAP=abc)"
    assert_contains "$OUT" "Caps this pass: DEFAULT_CAP=2 " \
        "a non-integer value falls back to the default"
    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER2_CAP=-1)"
    assert_contains "$OUT" "Caps this pass: DEFAULT_CAP=2 " "a negative value falls back to the default"

    OUT="$(run_balance balance.sh LOOM_CHAMPION_TIER2_CAP=)"
    assert_contains "$OUT" "Caps this pass: DEFAULT_CAP=2 " "an empty value falls back to the default"

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
echo "Test 2: the per-level rate limits are wired; no Champion file reads a retired tier label"
RATE="$(section_body "$PROMO_MD" "Promotion Order")"
assert_contains "$RATE" "\`loom:very-important\`, then \`loom:important\`" \
    "Promotion Order ranks very-important, then important"
assert_contains "$RATE" "oldest first within a level" "Promotion Order breaks ties oldest first"
for v in LOOM_CHAMPION_TIER2_CAP LOOM_CHAMPION_TIER3_CAP LOOM_CHAMPION_TIER3_BACKLOG_CAP; do
    assert_contains "$RATE" "$v" "Rate Limiting by Level names $v"
done
assert_contains "$RATE" "\`LOOM_CHAMPION_TIER3_BACKLOG_CAP\` are currently
unused" "the tier3 knobs are documented as unused by the level mapping"
assert_contains "$RATE" "\`loom:very-important\` / \`loom:important\`: promote all qualifying proposals (no limit)" \
    "both priority levels take the old Tier 1 slot (uncapped)"
assert_contains "$RATE" "No priority level: promote up to \`\$DEFAULT_CAP\`" \
    "an unlevelled proposal takes the old Tier 2 per-pass cap"
assert_not_contains "$RATE" "DEFAULT_BACKLOG_CAP" "no-level promotion has no backlog gate"
assert_contains "$(section_body "$PROMO_MD" "Setting Priority")" "label the blocker too" \
    "Champion labels a priority proposal's blocker itself (no daemon inheritance)"
assert_contains "$(section_body "$PROMO_MD" "Setting Priority")" "never set a level just to get past a capacity cap" \
    "a priority level is never set to dodge a cap"
STEP3C="$(section_body "$PROMO_MD" "Promote (All Criteria Pass)")"
assert_contains "$STEP3C" "Step 3c: Capacity Deferral" "Step 3c capacity deferral is present"
assert_not_contains "$STEP3C" "below 5" "Step 3c's deferral template does not hardcode the backlog cap"
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
for v in LOOM_CHAMPION_PR_SLICE LOOM_CHAMPION_PROMOTION_SLICE LOOM_CHAMPION_TIER2_CAP \
         LOOM_CHAMPION_TIER3_CAP LOOM_CHAMPION_TIER3_BACKLOG_CAP; do
    assert_doc_contains "$THROUGHPUT_DOC" "\`$v\`" "promotion-throughput.md documents $v"
done
assert_doc_contains "$THROUGHPUT_DOC" "mapped by population" "promotion-throughput.md records the level mapping of the caps"
assert_doc_contains "$THROUGHPUT_DOC" "46" "promotion-throughput.md cites the measurement behind dropping the backlog gate"
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
