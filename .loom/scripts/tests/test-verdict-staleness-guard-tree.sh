#!/usr/bin/env bash
# test-verdict-staleness-guard-tree.sh - verdict-staleness-guard.sh's
# verdict-equivalence exemption (#9576, #9416) driven through the REAL
# `loom-daemon forge verdict-equivalent`, not a stub of it.
#
# test-verdict-staleness-guard.sh stubs the verb (it runs in the toolchain-free
# shell-suite job), so it proves only the guard's reading of the
# EQUIVALENCE_KIND= line. The properties below live in the Rust behind the verb,
# and asserting them against a stub would test the stub:
#
#   1. A rewind is not "unchanged". `compare/{base}...{head}` is a three-dot
#      compare: when the head was force-pushed BACK to an ancestor of the
#      reviewed commit, it reports `status: "behind"` with `files: []` although
#      the trees differ. Only `identical`/`ahead` with no files proves equality.
#      (PR #9581's review found this missing.)
#   2. The kill switches cover both paths. `LOOM_VERDICT_TREE_CARVEOUT=0` must
#      make THIS guard invalidate exactly as it makes the daemon pass
#      invalidate, and `LOOM_VERDICT_EQUIVALENCE=0` must drop #9416's two new
#      kinds while leaving #9124's tree kind in place — both switches are
#      evaluated inside the shared verb, not in shell.
#   3. The rebase-patch-identical kind (#9416) is proven by comparing the two
#      merge-base-relative diffs field by field, and REFUSES to answer on two
#      empty diffs or on a file entry with no `patch` text — the fail-closed
#      arms that keep a binary change or a coincidence from reading as proof.
#
# `gh` is stubbed on PATH (the real binary shells out to it for every compare and
# for the PR's base ref); the guard and the daemon are both real. Every case runs
# report-only (no --clear), so the only daemon verbs reached are the two reads:
# `forge trusted-comments` and `forge verdict-equivalent`.
#
# The guard is run from $STUB_DIR, which is deliberately NOT a git repository:
# that pins the clean-merge kind at Indeterminate here (it needs real objects, and
# never fetches them) so these cases exercise exactly the two forge-computed
# kinds. The clean-merge kind's own evidence is covered by the Rust tests in
# loom-daemon/src/verdict_equivalence/tests.rs, which build real git fixtures.
#
# Needs a built loom-daemon — wired in CI's "Native Port Suites" job, excluded
# from run-ci-suites.sh (ci-excluded.txt). It FAILS, never SKIPs, without one.
#
# Usage:
#   ./.loom/scripts/tests/test-verdict-staleness-guard-tree.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
GUARD="$SCRIPTS_DIR/verdict-staleness-guard.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- The real binary ---------------------------------------------------------
# --self-only: LOOM_DAEMON_BIN is set per invocation below, to the pinned binary.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin --self-only "$SCRIPTS_DIR" forge
REAL_DAEMON_BIN="$LOOM_DAEMON_SELF_BIN"
if ! "$REAL_DAEMON_BIN" forge verdict-equivalent --help >/dev/null 2>&1; then
    echo -e "${RED}FATAL${NC}: $REAL_DAEMON_BIN has no 'forge verdict-equivalent' verb (predates #9416)" >&2
    exit 1
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------------
#   gh pr view 9581 ...                        -> cat $STUB_DIR/pr.json
#   gh api repos/.../issues/9581/comments ...  -> cat $STUB_DIR/comments.json
#   gh api repos/.../compare/<refs>            -> cat $STUB_DIR/compare-<refs>.json
#                                                 if present, else compare.json;
#                                                 argv logged to compare-calls.log
#   anything else                              -> logged to writes.log, exit 3
#
# The per-route file is what lets a rebase case give the two merge-base-relative
# diffs DIFFERENT answers: `main...<reviewed>` and `main...<head>` are distinct
# routes, and the patch-identity kind's whole question is whether they agree.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "pr" && "$2" == "view" ]]; then cat "$D/pr.json"; exit 0; fi
if [[ "$1" == "api" && "$2" == repos/*/issues/*/comments ]]; then cat "$D/comments.json"; exit 0; fi
if [[ "$1" == "api" && "$2" == repos/*/compare/* ]]; then
  printf '%s\n' "$*" >> "$D/compare-calls.log"
  REFS="${2##*/compare/}"
  if [[ -f "$D/compare-$REFS.json" ]]; then cat "$D/compare-$REFS.json"; else cat "$D/compare.json"; fi
  exit 0
fi
printf '%s\n' "$*" >> "$D/writes.log"
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
unset LOOM_GH_BIN LOOM_VERDICT_TREE_CARVEOUT LOOM_VERDICT_EQUIVALENCE

SHA_A="1111111111111111111111111111111111111111"
SHA_B="2222222222222222222222222222222222222222"

# An approved PR whose verdict was rendered at SHA_A; head is now SHA_B.
# `baseRefName` is what the two #9416 kinds resolve their merge-base-relative
# diffs against (`forge verdict-equivalent` reads it with `gh pr view`).
printf '{"headRefOid":"%s","state":"OPEN","merged":false,"baseRefName":"main","labels":[{"name":"loom:pr"}]}' \
    "$SHA_B" > "$STUB_DIR/pr.json"
printf '[{"user":{"login":"rjwalters"},"author_association":"OWNER","created_at":"2026-09-30T00:00:00Z","body":"<!-- loom:verdict-sha sha=%s verdict=approved -->"}]' \
    "$SHA_A" > "$STUB_DIR/comments.json"

# One changed file, with every field the patch-identity kind compares.
one_file() {
    # one_file <status> <blob-sha> <patch>
    printf '{"status":"ahead","files":[{"filename":"src/lib.rs","status":"%s","sha":"%s","patch":"%s"}]}' \
        "$1" "$2" "$3"
}

reset_routes() { rm -f "$STUB_DIR"/compare-*...*.json; }

run_guard() {
    # run_guard <default-compare-json> [env assignments...]
    printf '%s' "$1" > "$STUB_DIR/compare.json"
    shift
    rm -f "$STUB_DIR/compare-calls.log" "$STUB_DIR/writes.log"
    # Run from $STUB_DIR: not a git repository, so the clean-merge kind is
    # deterministically Indeterminate here (see the header).
    OUT="$(cd "$STUB_DIR" && env "$@" LOOM_DAEMON_BIN="$REAL_DAEMON_BIN" "$GUARD" 9581 2>"$STUB_DIR/stderr.log")"
    RC=$?
    COMPARE_CALLS="$(cat "$STUB_DIR/compare-calls.log" 2>/dev/null || true)"
    WRITES="$(cat "$STUB_DIR/writes.log" 2>/dev/null || true)"
}

decision() { printf '%s\n' "$OUT" | sed -n 's/^DECISION=//p' | head -n1; }
reason() { printf '%s\n' "$OUT" | sed -n 's/^REASON=//p' | head -n1; }

echo "Testing verdict-staleness-guard.sh against the real 'forge verdict-equivalent'..."

# (r1) The re-date shape: a tree-identical commit appended on top -> FRESH.
run_guard '{"status":"ahead","files":[]}'
assert_eq "0" "$RC" "(r1) ahead + no files -> exit 0"
assert_eq "FRESH" "$(decision)" "(r1) DECISION=FRESH"
assert_contains "$COMPARE_CALLS" "compare/$SHA_A...$SHA_B" "(r1) compare asked marker...head"
assert_eq "" "$WRITES" "(r1) no unexpected gh calls"

# (r2) identical -> FRESH.
run_guard '{"status":"identical","files":[]}'
assert_eq "FRESH" "$(decision)" "(r2) identical -> FRESH"

# (r3) THE REVIEW HOLE: head rewound to an ancestor -> 'behind', no files,
#      trees differ. Must be STALE.
run_guard '{"status":"behind","files":[]}'
assert_eq "12" "$RC" "(r3) behind + no files -> exit 12"
assert_eq "STALE" "$(decision)" "(r3) DECISION=STALE (a rewind is not tree-identical)"

# (r4) diverged, no files -> STALE.
run_guard '{"status":"diverged","files":[]}'
assert_eq "STALE" "$(decision)" "(r4) diverged + no files -> STALE"

# (r5) ahead WITH files -> STALE.
run_guard '{"status":"ahead","files":[{"filename":"src/lib.rs"}]}'
assert_eq "STALE" "$(decision)" "(r5) ahead + files -> STALE"

# (r6) no status key -> no answer -> STALE (fail closed).
run_guard '{"files":[]}'
assert_eq "STALE" "$(decision)" "(r6) missing status -> STALE"

# (r7) KILL SWITCH PARITY: the carve-out switched off makes the shell path
#      invalidate a head move that would otherwise be exempt, and no compare
#      call is made at all.
for off in 0 false no off; do
    run_guard '{"status":"ahead","files":[]}' LOOM_VERDICT_TREE_CARVEOUT="$off"
    assert_eq "STALE" "$(decision)" "(r7) LOOM_VERDICT_TREE_CARVEOUT=$off -> STALE"
    assert_eq "" "$COMPARE_CALLS" "(r7) LOOM_VERDICT_TREE_CARVEOUT=$off -> no compare call"
done

# (r8) ...and an explicit ON value keeps the exemption.
run_guard '{"status":"ahead","files":[]}' LOOM_VERDICT_TREE_CARVEOUT=1
assert_eq "FRESH" "$(decision)" "(r8) LOOM_VERDICT_TREE_CARVEOUT=1 -> FRESH"
assert_contains "$(reason)" "equivalence kind: tree" "(r8) the REASON names the kind that carried it"

# --- #9416: the rebase-patch-identical kind ----------------------------------
#
# The head moved to a commit whose own merge-base-relative patch is byte-for-byte
# what was reviewed — the rebase-onto-a-newer-base shape, two of the six moved
# heads in #9416's 16-PR audit. The default compare route (SHA_A...SHA_B) is a
# real content difference, so the tree kind refutes first and the patch-identity
# kind is the one that answers.
PATCH='@@ -1 +1 @@\\n-old\\n+new\\n'
TREE_DIFFERS='{"status":"ahead","files":[{"filename":"src/lib.rs"}]}'

# (r9) The two merge-base-relative diffs agree in every compared field -> FRESH
#      by `rebase-patch-identical`, and CI is not exempted (the guard says so).
reset_routes
one_file modified aaaaaaa1 "$PATCH" > "$STUB_DIR/compare-main...$SHA_A.json"
one_file modified aaaaaaa1 "$PATCH" > "$STUB_DIR/compare-main...$SHA_B.json"
run_guard "$TREE_DIFFERS"
assert_eq "0" "$RC" "(r9) byte-identical PR patch across the move -> exit 0"
assert_eq "FRESH" "$(decision)" "(r9) DECISION=FRESH"
assert_contains "$(reason)" "equivalence kind: rebase-patch-identical" \
    "(r9) the REASON names the rebase kind, not the tree one"
assert_contains "$(reason)" "CI still re-runs" "(r9) the REASON states that CI is not exempted"
assert_contains "$COMPARE_CALLS" "compare/main...$SHA_A" "(r9) the reviewed side is asked base...reviewed"
assert_contains "$COMPARE_CALLS" "compare/main...$SHA_B" "(r9) the new side is asked base...head"
assert_eq "" "$WRITES" "(r9) no unexpected gh calls"

# (r10) A DIFFERENT resulting blob id with the same patch text is still a
#       different change — the field-by-field comparison, not a text-only one.
reset_routes
one_file modified aaaaaaa1 "$PATCH" > "$STUB_DIR/compare-main...$SHA_A.json"
one_file modified bbbbbbb2 "$PATCH" > "$STUB_DIR/compare-main...$SHA_B.json"
run_guard "$TREE_DIFFERS"
assert_eq "12" "$RC" "(r10) differing result blob id -> exit 12"
assert_eq "STALE" "$(decision)" "(r10) DECISION=STALE"

# (r11) A changed file whose `patch` the endpoint omitted (binary content, or a
#       diff too large to serialize) is NO byte evidence either way. Identical
#       metadata must not read as proof.
reset_routes
printf '{"status":"ahead","files":[{"filename":"img.png","status":"modified","sha":"aaaaaaa1"}]}' \
    > "$STUB_DIR/compare-main...$SHA_A.json"
printf '{"status":"ahead","files":[{"filename":"img.png","status":"modified","sha":"aaaaaaa1"}]}' \
    > "$STUB_DIR/compare-main...$SHA_B.json"
run_guard "$TREE_DIFFERS"
assert_eq "STALE" "$(decision)" "(r11) a patch-less file entry proves nothing -> STALE"

# (r12) Two EMPTY merge-base-relative diffs prove only that each head equals its
#       own merge base, and those bases can differ. Must not read as proof.
reset_routes
printf '{"status":"ahead","files":[]}' > "$STUB_DIR/compare-main...$SHA_A.json"
printf '{"status":"ahead","files":[]}' > "$STUB_DIR/compare-main...$SHA_B.json"
run_guard "$TREE_DIFFERS"
assert_eq "STALE" "$(decision)" "(r12) two empty diffs -> STALE (fail closed)"

# (r13) THE INNER KILL SWITCH: LOOM_VERDICT_EQUIVALENCE=0 drops #9416's two new
#       kinds while leaving #9124's tree kind in place.
reset_routes
one_file modified aaaaaaa1 "$PATCH" > "$STUB_DIR/compare-main...$SHA_A.json"
one_file modified aaaaaaa1 "$PATCH" > "$STUB_DIR/compare-main...$SHA_B.json"
for off in 0 false no off; do
    run_guard "$TREE_DIFFERS" LOOM_VERDICT_EQUIVALENCE="$off"
    assert_eq "STALE" "$(decision)" "(r13) LOOM_VERDICT_EQUIVALENCE=$off -> STALE"
done
reset_routes
run_guard '{"status":"ahead","files":[]}' LOOM_VERDICT_EQUIVALENCE=0
assert_eq "FRESH" "$(decision)" "(r13) ...while the #9124 tree kind still applies with the inner switch off"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
