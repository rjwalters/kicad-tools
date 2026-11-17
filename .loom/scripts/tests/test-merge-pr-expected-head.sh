#!/usr/bin/env bash
# test-merge-pr-expected-head.sh - merge-pr.sh --expected-head <sha> (#11859).
#
# A targeted Champion (`/loom:champion --pr N`) evaluates ONE head and then
# merges. Before #11859 merge-pr.sh took no caller input for the head: its
# optimistic-concurrency precondition ($MERGE_PRECONDITION_SHA) came from the
# script's own uncached read, so a push that landed between Champion's
# evaluation and the merge call was merged under a verdict about another tree.
# --expected-head pins the caller's SHA, fail-closed, with a distinct exit 8.
#
# Extracted from the shipped script (same strategy as
# test-merge-pr-pr-state.sh / test-merge-pr-head-mismatch.sh), so the suite
# stays in lockstep with merge-pr.sh:
#   1. _check_expected_head: unset / match pass, mismatch and unreadable
#      refuse with exit 8, the sentinel line and a stderr reason.
#   2. Argument parsing: full 40/64-hex SHAs only; anything else exits 1;
#      no flag leaves the pin empty (batch callers unchanged).
#   3. The precondition read: the caller's SHA wins and is compared; with no
#      flag the script's own fresh read is used exactly as before.
#   4. Wiring: the first check runs right after the terminal-state gate and
#      before every gate that can write; the merge call names
#      $MERGE_PRECONDITION_SHA.
#
# Hermetic: no forge, no network, no daemon binary.
#
# Usage: ./.loom/scripts/tests/test-merge-pr-expected-head.sh

# shellcheck disable=SC2034
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGE_PR_SRC="$(cd "$TEST_DIR/.." && pwd)/merge-pr.sh"

TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0
ok()   { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; [[ -z "${2:-}" ]] || echo "    $2"; }
assert_eq() { if [[ "$1" == "$2" ]]; then ok "$3"; else fail "$3" "expected '$1' got '$2'"; fi; }
assert_contains() { if grep -qF -- "$2" <<<"$1"; then ok "$3"; else fail "$3" "missing '$2' in: $1"; fi; }
assert_not_contains() { if grep -qF -- "$2" <<<"$1"; then fail "$3" "unexpected '$2' in: $1"; else ok "$3"; fi; }

RED=''; NC=''
error() { echo "ERROR: $*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-expected-head.XXXXXX")"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT

SHA_A="$(printf 'a%.0s' {1..40})"
SHA_B="$(printf 'b%.0s' {1..40})"
SHA_256="$(printf 'c%.0s' {1..64})"

# ---------------------------------------------------------------------------
echo "Part 1: _check_expected_head"
FUNC="$WORK/func.sh"
grep -E '^_check_expected_head\(\) \{' "$MERGE_PR_SRC" > "$FUNC" || true
if [[ ! -s "$FUNC" ]]; then echo "FATAL: could not extract _check_expected_head" >&2; exit 2; fi
# shellcheck disable=SC1090
source "$FUNC"

PR_NUMBER=42
LAST_OUT=""; LAST_ERR=""; LAST_RC=0
run_check() { # <expected> <actual>
    EXPECTED_HEAD_SHA="$1"
    set +e
    LAST_OUT="$( (_check_expected_head "$2"; echo "REACHED-AFTER-CHECK") 2>"$WORK/stderr")"; LAST_RC=$?
    set -e
    LAST_ERR="$(cat "$WORK/stderr")"
}

run_check "" "$SHA_B"
assert_eq 0 "$LAST_RC" "no pin: any head passes (batch behaviour unchanged)"
assert_contains "$LAST_OUT" "REACHED-AFTER-CHECK" "no pin: the merge flow continues"
assert_not_contains "$LAST_OUT" "LOOM-EXPECTED-HEAD-MISMATCH" "no pin: no sentinel"

run_check "$SHA_A" "$SHA_A"
assert_eq 0 "$LAST_RC" "pinned head == current head: passes"
assert_contains "$LAST_OUT" "REACHED-AFTER-CHECK" "match: the merge flow continues"

run_check "$SHA_A" "$SHA_B"
assert_eq 8 "$LAST_RC" "head moved after evaluation: exit 8 (distinct from 1 and 3)"
assert_not_contains "$LAST_OUT" "REACHED-AFTER-CHECK" "mismatch: nothing after the check runs"
assert_contains "$LAST_OUT" "LOOM-EXPECTED-HEAD-MISMATCH pr=42 expected=$SHA_A actual=$SHA_B" "mismatch: machine-readable sentinel on stdout"
assert_contains "$LAST_ERR" "Merge refused: PR #42 head is $SHA_B" "mismatch: the reason names the current head"
assert_contains "$LAST_ERR" "re-evaluate the current head" "mismatch: the reason says what to do"

run_check "$SHA_A" ""
assert_eq 8 "$LAST_RC" "unreadable head under a pin: fail closed (exit 8)"
assert_contains "$LAST_OUT" "actual=unknown" "unreadable head: sentinel says unknown"

run_check "$SHA_A" "${SHA_A:0:12}"
assert_eq 8 "$LAST_RC" "a prefix is not the evaluated head: exact match only"

# ---------------------------------------------------------------------------
echo "Part 2: argument parsing"
PARSE="$WORK/parse.sh"
awk '/^while \[\[ \$# -gt 0 \]\]; do$/ { on=1 } on { print } on && /^done$/ { exit }' "$MERGE_PR_SRC" > "$PARSE"
if ! grep -q -- '--expected-head)' "$PARSE"; then echo "FATAL: could not extract the argument loop" >&2; exit 2; fi
run_parse() {
    set +e
    LAST_OUT="$( (PR_NUMBER=""; EXPECTED_HEAD_SHA=""; ALLOW_RED_REQUIRED=()
        warning() { echo "WARN: $*" >&2; }
        # shellcheck disable=SC1090
        source "$PARSE"; echo "PIN=$EXPECTED_HEAD_SHA PR=$PR_NUMBER") 2>&1 )"; LAST_RC=$?
    set -e
}
set -- 7 --expected-head "$SHA_A"; run_parse "$@"
assert_eq 0 "$LAST_RC" "40-hex SHA is accepted"
assert_contains "$LAST_OUT" "PIN=$SHA_A PR=7" "the pin and the PR number are both parsed"
set -- --expected-head "$SHA_256" 7 --auto; run_parse "$@"
assert_contains "$LAST_OUT" "PIN=$SHA_256 PR=7" "64-hex (SHA-256 repo) is accepted, in any position"
set -- 7 --auto; run_parse "$@"
assert_contains "$LAST_OUT" "PIN= PR=7" "no flag: no pin"
# --redate-stale-checks was removed outright (#12063): an unresynced caller
# that still passes it is refused, never silently merged.
set -- 7 --auto --redate-stale-checks --expected-head "$SHA_A"; run_parse "$@"
assert_eq 1 "$LAST_RC" "--redate-stale-checks is refused (removed, #12063)"
assert_contains "$LAST_OUT" "Unknown option: --redate-stale-checks" "--redate-stale-checks: says why"
for bad in "${SHA_A:0:12}" "$(tr a A <<<"$SHA_A")" "${SHA_A}0" "--auto"; do
    set -- 7 --expected-head "$bad"; run_parse "$@"
    assert_eq 1 "$LAST_RC" "rejected value '${bad:0:16}': exit 1"
    assert_contains "$LAST_OUT" "--expected-head requires a full lowercase hex commit SHA" "rejected value '${bad:0:16}': says why"
done
set -- 7 --expected-head; run_parse "$@"
assert_eq 1 "$LAST_RC" "missing value: exit 1 (no unbound-variable crash)"
assert_contains "$LAST_OUT" "--expected-head requires" "missing value: says why"

# ---------------------------------------------------------------------------
echo "Part 3: the merge precondition read"
PRE="$WORK/pre.sh"
awk '/^MERGE_PRECONDITION_SHA="\$PR_HEAD_SHA"$/ { on=1 } on { print } on && /_check_expected_head "\$MERGE_PRECONDITION_SHA"/ { exit }' "$MERGE_PR_SRC" > "$PRE"
if ! grep -q '_check_expected_head "\$MERGE_PRECONDITION_SHA"' "$PRE"; then echo "FATAL: could not extract the precondition read" >&2; exit 2; fi
REPO_NWO=o/r; GH=gh
run_pre() { # <pin> <cached head> <fresh head or "">
    EXPECTED_HEAD_SHA="$1"; PR_HEAD_SHA="$2"; FRESH="$3"
    forge_get_pr_nocache() { if [[ -n "$FRESH" ]]; then printf '{"head":{"sha":"%s"}}' "$FRESH"; else return 1; fi; }
    set +e
    # shellcheck disable=SC1090
    LAST_OUT="$( (source "$PRE"; echo "PRE=$MERGE_PRECONDITION_SHA") 2>&1 )"; LAST_RC=$?
    set -e
}
run_pre "" "$SHA_A" "$SHA_B"
assert_contains "$LAST_OUT" "PRE=$SHA_B" "no pin: the script's own fresh read is the precondition (unchanged)"
run_pre "$SHA_A" "$SHA_A" "$SHA_A"
assert_contains "$LAST_OUT" "PRE=$SHA_A" "pin == fresh head: the caller's SHA is the precondition"
run_pre "$SHA_A" "$SHA_A" "$SHA_B"
assert_eq 8 "$LAST_RC" "head moved between the first read and the merge: exit 8"
assert_not_contains "$LAST_OUT" "PRE=" "moved head: never reaches the merge call"
run_pre "$SHA_A" "$SHA_A" ""
assert_contains "$LAST_OUT" "PRE=$SHA_A" "fresh read failed: falls back to the checked head, and the forge is gated on the caller's SHA"

# ---------------------------------------------------------------------------
echo "Part 4: wiring"
line_of() { grep -nF -- "$1" "$MERGE_PR_SRC" | awk -F: 'NR == 1 { print $1 }'; }
GATE_LINE="$(grep -n 'merge-pr pr-state --state' "$MERGE_PR_SRC" | awk -F: 'NR == 1 { print $1 }')"
FIRST_CHECK="$(line_of '_check_expected_head "$PR_HEAD_SHA"')"
# The first EXECUTABLE line of the merge-ordering guard (comments excluded).
FIRST_GUARD="$(grep -nE 'merge-pr stacked-children' "$MERGE_PR_SRC" | grep -vE '^[0-9]+:[[:space:]]*#' | awk -F: -v g="$GATE_LINE" '$1 > g { print $1; exit }')"
MERGE_CALL="$(line_of 'forge_merge_pr "$REPO_NWO" "$PR_NUMBER" "$MERGE_PRECONDITION_SHA"')"
PRE_LINE="$(line_of '_check_expected_head "$MERGE_PRECONDITION_SHA"')"
[[ -n "$FIRST_CHECK" && -n "$GATE_LINE" && "$FIRST_CHECK" -gt "$GATE_LINE" ]] && ok "first check runs after the terminal-state gate (a merged PR still exits 0)" || fail "first check placement" "gate=$GATE_LINE check=$FIRST_CHECK"
[[ -n "$FIRST_GUARD" && "$FIRST_CHECK" -lt "$FIRST_GUARD" ]] && ok "first check runs before the merge-ordering guard (no write for an unseen tree)" || fail "first check precedes guards" "check=$FIRST_CHECK guard=$FIRST_GUARD"
[[ -n "$MERGE_CALL" && -n "$PRE_LINE" && "$PRE_LINE" -lt "$MERGE_CALL" ]] && ok "precondition check runs before the forge merge call" || fail "precondition placement" "pre=$PRE_LINE merge=$MERGE_CALL"
assert_contains "$(sed -n "${PRE_LINE}p" "$MERGE_PR_SRC")" 'MERGE_PRECONDITION_SHA="${EXPECTED_HEAD_SHA:-$MERGE_PRECONDITION_SHA}"' "the caller's SHA wins the precondition"

echo
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
