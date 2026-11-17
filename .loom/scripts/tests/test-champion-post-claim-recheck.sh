#!/usr/bin/env bash
# test-champion-post-claim-recheck.sh - Regression test for #11989: a Champion
# that claims an issue another Champion already decided steps back.
#
# THE FAILURE THIS GUARDS AGAINST
#
#   example-org/tool-repo#205, 2026-10-15: one host's Champion claimed, promoted and
#   released the issue at 00:01:58-59Z; a second host's Champion, working from
#   a candidate list read before that, found no claim at 00:02:00Z, claimed it
#   and promoted it again, posting a second approval comment. The claim step
#   checked only for a concurrent claim, never whether the issue was still a
#   proposal awaiting a verdict.
#
# WHAT THIS SUITE DOES
#
#   1. BEHAVIOUR -- champion-issue-promo.md's post-claim recheck block is
#      EXTRACTED and EXECUTED against a stubbed `gh`: an issue already
#      promoted, building, escalated, closed or no longer a proposal has its
#      claim released and is skipped; a proposal still awaiting a verdict is
#      kept.
#   2. WIRING -- the recheck reads the forge uncached (plain `gh`), and the
#      verdict-time recheck aborts on a verdict label too.
#
# Hermetic: file reads plus a mktemp -d fixture dir with a stub `gh`. No forge,
# no network. No `set -o pipefail` on purpose (#7790).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
pick_dir() { if [[ -d "$REPO_ROOT/$1" ]]; then echo "$REPO_ROOT/$1"; else echo "$REPO_ROOT/$2"; fi; }
ROLE_DIR="$(pick_dir .claude/commands/loom defaults/.claude/commands/loom)"
PROMO_MD="$ROLE_DIR/champion-issue-promo.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

echo "== test-champion-post-claim-recheck.sh (#11989) =="

# The first ```bash block after the "Post-claim candidacy recheck" heading.
BLOCK=$(awk '
  /Post-claim candidacy recheck/ { seen = 1 }
  seen && /^```bash$/ { inside = 1; next }
  inside && /^```$/ { exit }
  inside { print }
' "$PROMO_MD")
if [[ -z "$BLOCK" ]]; then
  fail "the post-claim recheck block is present in champion-issue-promo.md"
  echo "Results: $TESTS_RUN run, $TESTS_FAILED failed"; exit 1
fi
pass "the post-claim recheck block is present in champion-issue-promo.md"

if [[ "$BLOCK" == *'$GH_READ'* ]]; then
  fail "the recheck reads the forge uncached (plain gh)"
else
  pass "the recheck reads the forge uncached (plain gh)"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
# Stub gh: `issue view` prints the fixture's state<TAB>labels line (what the
# block's own --jq would produce); `issue edit` is logged.
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
case "$1 $2" in
  "issue view") cat "$FIXTURE" ;;
  "issue edit") echo "$*" >> "$EDITS" ;;
esac
STUB
chmod +x "$TMP/bin/gh"

# run_case <name> <state> <labels> <expect: keep|release>
run_case() {
  local name="$1" state="$2" labels="$3" expect="$4"
  printf '%s\t%s\n' "$state" "$labels" > "$TMP/fixture"
  : > "$TMP/edits"
  local out
  out=$(PATH="$TMP/bin:$PATH" FIXTURE="$TMP/fixture" EDITS="$TMP/edits" \
    ISSUE_NUMBER=205 bash -c "$BLOCK" 2>&1)
  local edits; edits=$(cat "$TMP/edits")
  if [[ "$expect" == release ]]; then
    if [[ "$edits" == *'--remove-label loom:evaluating'* && "$out" == *'skipping'* ]]; then
      pass "$name: claim released and skipped"
    else
      fail "$name: claim released and skipped (edits='$edits' out='$out')"
    fi
  else
    if [[ -z "$edits" && "$out" != *'skipping'* ]]; then
      pass "$name: kept for evaluation"
    else
      fail "$name: kept for evaluation (edits='$edits' out='$out')"
    fi
  fi
}

run_case "already promoted (example-org/tool-repo#205)" OPEN "loom:issue,loom:evaluating" release
run_case "promoted, proposal label still on" OPEN "loom:curated,loom:issue,loom:evaluating" release
run_case "already building" OPEN "loom:building,loom:evaluating" release
run_case "escalated to the operator" OPEN "loom:architect,loom:operator,loom:evaluating" release
run_case "sent back to Curator" OPEN "loom:triage,loom:evaluating" release
run_case "closed" CLOSED "loom:architect,loom:evaluating" release
run_case "blocked since discovery" OPEN "loom:blocked,loom:evaluating" release
run_case "blocked, proposal label still on" OPEN "loom:curated,loom:blocked,loom:evaluating" release
run_case "frozen since discovery" OPEN "loom:frozen,loom:evaluating" release
run_case "frozen, proposal label still on" OPEN "loom:architect,loom:frozen,loom:evaluating" release
run_case "curated proposal awaiting a verdict" OPEN "loom:curated,loom:evaluating" keep
run_case "architect proposal awaiting a verdict" OPEN "loom:architect,loom:evaluating,priority:p2" keep
run_case "a lookalike label is not a verdict" OPEN "loom:hermit,loom:issue-template,loom:evaluating" keep
run_case "a blocked/frozen lookalike is not a hold" OPEN "loom:hermit,loom:blocked-unnamed,loom:frozen-ish,loom:evaluating" keep

VERDICT=$(awk '/^### Verdict-time recheck/,/^## Promotion Workflow/' "$PROMO_MD")
if [[ "$VERDICT" == *'`loom:issue`, `loom:building`, `loom:operator`, `loom:blocked` or `loom:frozen`'* ]]; then
  pass "the verdict-time recheck also aborts on a verdict already written"
else
  fail "the verdict-time recheck also aborts on a verdict already written"
fi

echo "Results: $TESTS_RUN run, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
