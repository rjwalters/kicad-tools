#!/usr/bin/env bash
# test-champion-epic-child-discovery.sh - Regression test for issue #10935
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# Champion's "epic looks complete, close?" check sized an epic from
# discover_epic_children, which (1) never read the `<!-- loom:parent #N -->`
# marker or the epic's park record, (2) turned a failed read into "no children"
# (`|| echo '[]'`), and (3) cut the task list at 40 refs silently. An open child
# was missed (sky130-sar-adc#23 / #103) and the epic was escalated with a bare
# loom:operator. This extracts the real function from champion-common.md and
# runs it against a stubbed `gh`.
#
# Hermetic: no forge/network.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi
COMMON="$ROLE_DIR/champion-common.md"
EPIC="$ROLE_DIR/champion-epic.md"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }
eq() { if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1 (got '$2', want '$3')"; fi; }
check() { if grep -qF -- "$3" "$2"; then pass "$1"; else fail "$1 (missing: $3)"; fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Pull the function body out of the first bash block that defines it.
awk '/^discover_epic_children\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$COMMON" > "$TMP/fn.sh"
[[ -s "$TMP/fn.sh" ]] || { echo "FAIL: discover_epic_children not found in $COMMON"; exit 1; }

# Stub gh. Scenario knobs (env): FAIL_LIST=1 (issue list fails), FAIL_VIEW=<n>
# (view of that issue fails), BIG_TASKLIST=1 (epic body has 120 task refs).
cat > "$TMP/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
jqexpr=""; prev=""
for a in "$@"; do [[ "$prev" == "--jq" ]] && jqexpr="$a"; prev="$a"; done
emit() { if [[ -n "$jqexpr" ]]; then printf '%s' "$1" | jq -rc "$jqexpr"; else printf '%s' "$1"; fi; }
case "$1 $2" in
  "api repos/o/r/issues/23/sub_issues") echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  "issue list")
    [[ -n "${FAIL_LIST:-}" ]] && { echo "rate limited" >&2; exit 1; }
    case "$args" in
      *'loom:epic-phase'*)
        if [[ -n "${BIG_PAGE:-}" ]]; then   # full 500-row page, none carrying the marker
          emit "$(jq -nc '[range(500) | {number: (1000 + .), state: "CLOSED", body: "unrelated"}]')"
        else emit '[]'; fi ;;
      *'Epic #23'*) [[ -n "${BIG_PAGE_D:-}" ]] && emit "$(jq -nc '[range(200) | {number: (2000 + .), state: "CLOSED"}]')" || emit '[]' ;;
      *'loom:parent #23'*) emit '[{"number":103,"state":"OPEN","body":"x\n<!-- loom:parent #23 -->"},{"number":104,"state":"OPEN","body":"<!-- loom:parent #230 -->"}]' ;;
      *) emit '[]' ;;
    esac ;;
  "issue view")
    n="$3"
    if [[ "$n" == "23" ]]; then
      if [[ -n "${BIG_TASKLIST:-}" ]]; then
        body=""; for i in $(seq 200 320); do body="$body- [x] #$i\n"; done
        emit "{\"body\":\"$body\"}"
      else
        emit '{"body":"- [x] #24\n<!-- loom:park Blocked by: #50 by=curator at=2026-10-01T00:00:00Z -->\n| item | #99 |"}'
      fi
    else
      [[ "${FAIL_VIEW:-}" == "$n" ]] && { echo "boom" >&2; exit 1; }
      printf '%s' 'CLOSED'
    fi ;;
esac
STUB
chmod +x "$TMP/gh"

run() { # run <env assignments...> -> prints "open closed complete sources"
    env PATH="$TMP:$PATH" "$@" bash -c '
        source "$1"; discover_epic_children o/r 23
        echo "$EPIC_CHILD_STRONG_OPEN $EPIC_CHILD_STRONG_CLOSED $EPIC_CHILD_DISCOVERY_COMPLETE $EPIC_CHILD_SOURCES"
    ' _ "$TMP/fn.sh" 2>/dev/null
}

echo "Test 1: parent-marker child and park-record child are strong"
out="$(run X=1)"
read -r open closed complete srcs <<<"$out"
eq "open parent-marker child #103 counted (not #104 of epic #230)" "$open" "1"
eq "task-list #24 + park-record #50 counted closed" "$closed" "2"
eq "discovery complete" "$complete" "yes"
[[ "$srcs" == *parent-marker* ]] && pass "parent-marker listed as source" || fail "parent-marker not in sources ($srcs)"
[[ "$out" != *99* ]] && pass "table-cell #99 not promoted" || fail "table-cell ref promoted"

echo "Test 2: failed reads are not 'no children'"
read -r _ _ complete _ <<<"$(run FAIL_LIST=1)"
eq "failed issue-list read marks discovery incomplete" "$complete" "no"
read -r _ closed complete _ <<<"$(run FAIL_VIEW=24)"
eq "failed per-ref read marks discovery incomplete" "$complete" "no"

echo "Test 3: long task list is not truncated silently"
read -r _ _ complete _ <<<"$(run BIG_TASKLIST=1)"
eq ">100 task refs marks discovery incomplete" "$complete" "no"

echo "Test 3b: a full raw page trips the flag even when the marker filter leaves few rows"
read -r _ _ complete _ <<<"$(run BIG_PAGE=1)"
eq "full phase-marker page marks discovery incomplete" "$complete" "no"
read -r _ _ complete _ <<<"$(run BIG_PAGE_D=1)"
eq "full weak-reference page marks discovery incomplete" "$complete" "no"

echo "Test 4: role docs wire the flag and the new routes"
check "0a refuses a completion candidate on incomplete discovery" "$EPIC" 'EPIC_CHILD_DISCOVERY_COMPLETE=no'
check "0c files a verification child under the epic" "$EPIC" 'create-issue.sh --parent "$EPIC_NUMBER" --label loom:triage'
check "0c routes judgement calls through operator-decision" "$EPIC" 'loom-daemon operator-decision apply "$EPIC_NUMBER"'
if grep -qF 'gh issue edit "$EPIC_NUMBER" --add-label "loom:operator"' "$EPIC"; then
    fail "0c still adds a bare loom:operator"
else pass "0c no longer adds a bare loom:operator"; fi
if grep -F "head -40" "$COMMON" | grep -vq '^\s*#'; then fail "silent head -40 cap remains"; else pass "no silent head -40 cap"; fi
check "0c assigns RESIDUE_KIND explicitly" "$EPIC" 'RESIDUE_KIND=judgement'
check "0c reads the verification child outcome" "$EPIC" 'VERIFY_STATE=$(gh issue list'
check "0c builds the decision JSON with jq --arg" "$EPIC" 'jq -n --arg n "$EPIC_NUMBER"'
check "0d table documents the new rows" "$EPIC" 'sky130-sar-adc#23 shape'

echo
echo "Ran $TESTS_RUN, failed $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
