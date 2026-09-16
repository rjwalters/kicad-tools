#!/usr/bin/env bash
# test-check-pipefail-early-exit.sh - Tests for scripts/check-pipefail-early-exit.sh (#7790)
#
# The script under test is the ratchet for the `set -o pipefail` +
# early-exit-consumer SIGPIPE class (#7060, #7285, #7540, #7736, #7771 under
# parent #7789). A ratchet that silently stops detecting is worse than no
# ratchet — it reports OK forever while the class keeps growing — so this suite
# pins both halves of the contract: what is a finding, and what is NOT.
#
# Verified behavior:
#   - TRUE POSITIVE: pipefail + `| grep -q` whose status is consumed is flagged
#   - TRUE POSITIVE: pipefail + `set -e` + `| head -1` in an assignment
#   - TRUE NEGATIVE: same pipeline with no pipefail in scope
#   - TRUE NEGATIVE: pipeline guarded with `|| true`
#   - TRUE NEGATIVE: pipeline whose exit status is discarded (no -e, bare)
#   - TRUE NEGATIVE: `| while IFS= read` (reads all input; not early-exit)
#   - TRUE NEGATIVE: the pattern inside a `#` comment
#   - exemption comment on the line, and on the preceding line
#   - ratchet semantics: equal count passes, higher count fails, lower passes
#   - --require-baseline turns a missing ledger into a failure, not a pass
#   - --update writes a ledger that makes the same tree pass
#   - regression guard: defaults/scripts/verify-proposal-refs.sh stays clean
#     (that is the #7771 production site this gate was built around)
#
# NOTE ON FIXTURES: every fixture pipeline is assembled with a `$P` pipe
# variable rather than a literal `|`. Otherwise this file's own source lines
# would be findings of the very gate it tests, and would have to be carried in
# the baseline forever.
#
# Usage:
#   ./.loom/scripts/tests/test-check-pipefail-early-exit.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/check-pipefail-early-exit.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-pipefail-ratchet.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

P='|'   # see NOTE ON FIXTURES above

# findings_for <fixture> -> number of occurrences the scanner reports
findings_for() {
    "$SCRIPT" --list "$1" 2>/dev/null | awk '/occurrence\(s\)/ { print $2 }'
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-pipefail-early-exit.sh is executable"
else
    fail "missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: --help exits 0 and prints usage --------
echo "Test 2: --help"
out=$("$SCRIPT" --help 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"check-pipefail-early-exit.sh"* ]]; then
    pass "--help exits 0 and prints usage"
else
    fail "expected exit 0 with usage text, got rc=$RC"
fi

# -------- Test 3: unknown argument exits 2 --------
echo "Test 3: unknown argument"
"$SCRIPT" --bogus >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 2 ]]; then pass "unknown arg exits 2"; else fail "expected 2, got $RC"; fi

# -------- Test 4: TRUE POSITIVE — pipefail + grep -q in an if condition ------
echo "Test 4: true positive (pipefail + grep -q, status consumed)"
cat > "$WORKDIR/positive-grep.sh" <<EOF
#!/usr/bin/env bash
set -uo pipefail
if printf '%s\n' "\$BIG" $P grep -qFx "\$needle"; then
    echo found
fi
EOF
n="$(findings_for "$WORKDIR/positive-grep.sh")"
if [[ "$n" == "1" ]]; then pass "flags pipefail + grep -q in an if condition"; else fail "expected 1 finding, got '$n'"; fi

# -------- Test 5: TRUE POSITIVE — set -e + head in an assignment -------------
echo "Test 5: true positive (set -euo pipefail + head)"
cat > "$WORKDIR/positive-head.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
first="\$(git log --format=%H $P head -1)"
echo "\$first"
EOF
n="$(findings_for "$WORKDIR/positive-head.sh")"
if [[ "$n" == "1" ]]; then pass "flags head under set -e + pipefail"; else fail "expected 1 finding, got '$n'"; fi

# -------- Test 6: TRUE NEGATIVE — identical pipeline, no pipefail ------------
echo "Test 6: true negative (no pipefail in scope)"
cat > "$WORKDIR/negative-nopipefail.sh" <<EOF
#!/usr/bin/env bash
set -eu
if printf '%s\n' "\$BIG" $P grep -qFx "\$needle"; then
    echo found
fi
first="\$(git log --format=%H $P head -1)"
EOF
n="$(findings_for "$WORKDIR/negative-nopipefail.sh")"
if [[ "$n" == "0" ]]; then pass "same pipelines without pipefail are not findings"; else fail "expected 0 findings, got '$n'"; fi

# -------- Test 7: TRUE NEGATIVE — guarded with || true ----------------------
echo "Test 7: true negative (|| true neutralises the false failure)"
cat > "$WORKDIR/negative-guarded.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
first="\$(git log --format=%H $P head -1 || true)"
EOF
n="$(findings_for "$WORKDIR/negative-guarded.sh")"
if [[ "$n" == "0" ]]; then pass "|| true guarded pipeline is not a finding"; else fail "expected 0 findings, got '$n'"; fi

# -------- Test 8: TRUE NEGATIVE — exit status discarded ---------------------
echo "Test 8: true negative (status discarded)"
cat > "$WORKDIR/negative-discarded.sh" <<EOF
#!/usr/bin/env bash
set -uo pipefail
git log --format=%H $P head -5
EOF
n="$(findings_for "$WORKDIR/negative-discarded.sh")"
if [[ "$n" == "0" ]]; then pass "bare pipeline without set -e is not a finding"; else fail "expected 0 findings, got '$n'"; fi

# -------- Test 9: TRUE NEGATIVE — `| while IFS= read` reads all input -------
echo "Test 9: true negative (while read consumes the whole stream)"
cat > "$WORKDIR/negative-whileread.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
git ls-files $P while IFS= read -r f; do
    echo "\$f"
done
EOF
n="$(findings_for "$WORKDIR/negative-whileread.sh")"
if [[ "$n" == "0" ]]; then pass "| while IFS= read is not an early-exit consumer"; else fail "expected 0 findings, got '$n'"; fi

# -------- Test 10: TRUE NEGATIVE — the pattern inside a comment -------------
echo "Test 10: true negative (commented-out example)"
cat > "$WORKDIR/negative-comment.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
# never write: if printf '%s' "\$x" $P grep -q y; then
echo ok   # nor here: git log $P head -1
EOF
n="$(findings_for "$WORKDIR/negative-comment.sh")"
if [[ "$n" == "0" ]]; then pass "comments are not scanned"; else fail "expected 0 findings, got '$n'"; fi

# -------- Test 11: exemption comment, same line and preceding line ----------
echo "Test 11: exemption comment"
cat > "$WORKDIR/negative-exempt.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
first="\$(printf '%s\n' "\$THREE_LINE_LITERAL" $P head -1)"  # loom-lint: allow-pipefail-early-exit -- fixed 3-line producer
# loom-lint: allow-pipefail-early-exit -- fixed 3-line producer
second="\$(printf '%s\n' "\$THREE_LINE_LITERAL" $P head -2)"
EOF
n="$(findings_for "$WORKDIR/negative-exempt.sh")"
if [[ "$n" == "0" ]]; then pass "exemption honoured on the line and the line above"; else fail "expected 0 findings, got '$n'"; fi

# -------- Test 12: ratchet — equal count passes, higher fails, lower passes --
echo "Test 12: ratchet semantics"
BL="$WORKDIR/baseline.txt"
printf '# ledger\n1 %s\n' "$WORKDIR/positive-grep.sh" > "$BL"
"$SCRIPT" --baseline "$BL" --quiet "$WORKDIR/positive-grep.sh" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 0 ]]; then pass "count equal to baseline passes"; else fail "expected 0, got $RC"; fi

printf '# ledger\n0 %s\n' "$WORKDIR/positive-grep.sh" > "$BL"
out="$("$SCRIPT" --baseline "$BL" --quiet "$WORKDIR/positive-grep.sh" 2>&1)"; RC=$?
if [[ "$RC" -eq 1 && "$out" == *"baseline allows 0"* ]]; then
    pass "count above baseline fails and names the offending line"
else
    fail "expected rc=1 naming the regression, got rc=$RC"
fi

printf '# ledger\n9 %s\n' "$WORKDIR/positive-grep.sh" > "$BL"
"$SCRIPT" --baseline "$BL" --quiet "$WORKDIR/positive-grep.sh" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 0 ]]; then pass "shrinking below baseline is allowed"; else fail "expected 0, got $RC"; fi

# A file absent from the ledger may not gain its first occurrence.
printf '# ledger\n' > "$BL"
"$SCRIPT" --baseline "$BL" --quiet "$WORKDIR/positive-grep.sh" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 1 ]]; then pass "unlisted file gaining its first occurrence fails"; else fail "expected 1, got $RC"; fi

# -------- Test 13: missing baseline --------------------------------------
echo "Test 13: missing baseline"
"$SCRIPT" --baseline "$WORKDIR/absent.txt" --quiet "$WORKDIR/positive-grep.sh" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 0 ]]; then pass "missing baseline alone is advisory"; else fail "expected 0, got $RC"; fi

out="$("$SCRIPT" --baseline "$WORKDIR/absent.txt" --require-baseline --quiet "$WORKDIR/positive-grep.sh" 2>&1)"; RC=$?
if [[ "$RC" -eq 1 && "$out" == *"baseline not found"* ]]; then
    pass "--require-baseline turns a deleted ledger into a failure"
else
    fail "expected rc=1 'baseline not found', got rc=$RC"
fi

# -------- Test 14: --update writes a ledger that makes the tree pass --------
echo "Test 14: --update"
"$SCRIPT" --baseline "$WORKDIR/generated.txt" --update --quiet "$WORKDIR/positive-grep.sh" "$WORKDIR/positive-head.sh" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 0 && -f "$WORKDIR/generated.txt" ]]; then
    "$SCRIPT" --baseline "$WORKDIR/generated.txt" --quiet "$WORKDIR/positive-grep.sh" "$WORKDIR/positive-head.sh" >/dev/null 2>&1; RC=$?
    if [[ "$RC" -eq 0 ]]; then pass "--update produces a passing ledger"; else fail "generated ledger still fails (rc=$RC)"; fi
else
    fail "--update did not write $WORKDIR/generated.txt (rc=$RC)"
fi

# -------- Test 15: regression guard for the #7771 production site ----------
echo "Test 15: verify-proposal-refs.sh stays clean"
VPR="$REPO_ROOT/defaults/scripts/verify-proposal-refs.sh"
if [[ -f "$VPR" ]]; then
    n="$(findings_for "$VPR")"
    if [[ "$n" == "0" ]]; then
        pass "verify-proposal-refs.sh has no pipefail + early-exit pipeline (#7736, #7771)"
    else
        fail "verify-proposal-refs.sh regressed: $n occurrence(s)"
    fi
else
    fail "expected $VPR to exist"
fi

# -------- Summary --------
echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi
echo "All tests passed"
exit 0
