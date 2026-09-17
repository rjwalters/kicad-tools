#!/usr/bin/env bash
# test-classify-dependency-block-premise-false.sh - premise-false interaction
# with classify-dependency-block.sh's --check-defer gate (#7904).
#
# Extracted as a sibling module from test-classify-dependency-block.sh
# (#7758 file-size ratchet: that file was already at the 1000-code-line
# threshold, and this scenario is a self-contained addition) per
# .loom/docs/file-size-policy.md's "extracting a test module to a sibling
# file is a real improvement" guidance.
#
# #7657 added a separate premise-false close gate to champion-issue-promo.md;
# its own acceptance criteria require that a MIXED premise-false + open-
# dependency finding set still defer via THIS gate, not fall through to
# escalation. A [premise-false]-tagged bullet is not itself dependency-
# attributable, so it must be excluded from the "every finding must be
# dependency-shaped" check rather than disqualifying the whole set.
#
# Hermetic: no network, no live forge, no tokens. Every read goes to the stub.
#
# Usage:
#   ./.loom/scripts/tests/test-classify-dependency-block-premise-false.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
CDB="$SCRIPTS_DIR/classify-dependency-block.sh"

# Pin the loom-daemon this suite tests against — the subject is a thin stub over
# `loom-daemon classify-dependency-block` now (epic #7810 PR 3). FATAL, not SKIP: see the helper.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "classify-dependency-block"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

# =====================================================================
# Black-box: stub `gh` on PATH, run the real script
# =====================================================================

STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cdb-pf-test.XXXXXX")"
cleanup() { rm -rf "$STUB_DIR"; }
trap cleanup EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Minimal `gh` stub: serves fixtures from issue-<owner>_<repo>_<N>.json.
STUB_DIR="$(cd "$(dirname "$0")" && pwd)"
printf '%s\n' "$*" >> "$STUB_DIR/calls.log"

[[ "${1:-}" == "--version" ]] && { echo "gh version 0.0.0 (stub)"; exit 0; }
kind="${1:-}"
case "$kind" in issue|pr) ;; *) echo "stub gh: unhandled args: $*" >&2; exit 3 ;; esac

action="$2"; num="$3"; shift 3
repo=""; jqexpr=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)   repo="$2"; shift 2 ;;
    --json)   shift 2 ;;
    --jq|-q)  jqexpr="$2"; shift 2 ;;
    *)        shift ;;
  esac
done

key="$(printf '%s' "$repo#$num" | tr '/#' '__')"
prefix="issue"; [[ "$kind" == "pr" ]] && prefix="pr"
f="$STUB_DIR/$prefix-$key.json"

case "$action" in
  view)
    [[ -f "$f" ]] || { echo "gh: not found" >&2; exit 1; }
    if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" < "$f"; else cat "$f"; fi
    ;;
  *)
    echo "stub gh: unhandled action: $action" >&2; exit 3 ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

export PATH="$STUB_DIR:$PATH"
export GH_CACHE_DISABLE=1

# issue_fixture <owner/repo#N> <STATE> <body> [labels csv] [comment...]
issue_fixture() {
    local node="$1" state="$2" body="$3" labels="${4:-}"
    shift 4 2>/dev/null || shift $#
    local key; key="$(printf '%s' "$node" | tr '/#' '__')"
    local comments_json="[]" c
    for c in "$@"; do
        comments_json="$(jq -n --argjson a "$comments_json" --arg b "$c" '$a + [{"body":$b}]')"
    done
    jq -n --arg s "$state" --arg b "$body" --arg l "$labels" --argjson c "$comments_json" \
        '{state:$s, body:$b, comments:$c,
          labels: ($l | if . == "" then [] else split(",") | map({name:.}) end)}' \
        > "$STUB_DIR/issue-$key.json"
}

reset_state() {
    rm -f "$STUB_DIR"/issue-*.json "$STUB_DIR"/pr-*.json "$STUB_DIR/calls.log"
}

# run_cdb <args...> -> sets OUT / RC
run_cdb() {
    OUT="$("$CDB" --no-cache "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
}

echo
echo "--- #7904: a premise-false finding mixed with a genuine open dependency still DEFERS ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect' \
    '**Champion Review: NEEDS REVISION**

- [premise-false] Criterion 8: `src/does/not/exist.rs` is not on `origin/main` -- verified via: `git ls-tree -r origin/main --name-only | grep -Fx src/does/not/exist.rs` (no output).
- Technical Feasibility: depends on #3, still open.
'
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "0" "$RC" "exit 0 - a premise-false bullet does not disqualify an otherwise-deferrable set (#7904)"
assert_contains "$OUT" "DEFER" "DEFER marker present"
assert_contains "$OUT" "OPEN_BLOCKERS: o/r#3" "the open blocker is still named with the premise-false bullet stripped"

echo
echo "--- #7904: EVERY finding premise-false -> NO_DEFER premise-false-only (falls through to the close gate) ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect' \
    '**Champion Review: NEEDS REVISION**

- [premise-false] Criterion 6: the cited test file does not exist.
- [premise-false] Criterion 8: the cited line range does not exist.
'
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "exit 1 - nothing left to defer on once premise-false bullets are stripped"
assert_contains "$OUT" "NO_DEFER" "NO_DEFER marker present"
assert_contains "$OUT" "REASON: premise-false-only" \
    "a distinct reason from merits-finding, so the caller can route to the close gate instead of escalation"

echo
echo "--- REGRESSION GUARD (#7904): premise-false + an ORDINARY merits finding still escalates ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect' \
    '**Champion Review: NEEDS REVISION**

- [premise-false] Criterion 8: the cited path does not exist.
- Scope Appropriateness: this is three issues in one.
'
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "exit 1 - an ordinary merits finding alongside premise-false still escalates"
assert_contains "$OUT" "REASON: merits-finding" \
    "reason is merits-finding, not premise-false-only -- a real merits finding is never masked by a co-occurring premise-false bullet"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
