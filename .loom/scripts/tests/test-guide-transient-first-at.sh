#!/usr/bin/env bash
# test-guide-transient-first-at.sh - Regression test for issue #13615
#
# release_due_transient_parks must measure park age from the start of the
# TRAILING transient chain (last attempt==1 record for the cause), not from the
# first record ever written: apply.rs resets the chain after a >24h gap, so an
# old chain would otherwise trigger a false "escalated" comment. The first_at
# assignment is extracted VERBATIM from each shipped copy and run on fixtures.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }
rec() { printf '{"at":"%s","transient":{"cause":"%s","attempt":%s}}' "$1" "$2" "$3"; }

for copy in "defaults/.claude/commands/loom/guide.md" "defaults/.agents/skills/loom-guide/SKILL.md"; do
    f="$REPO_ROOT/$copy"; echo "Copy: $copy"
    [[ -f "$f" ]] || { fail "$copy exists"; continue; }
    LINE="$(grep -m1 '^[[:space:]]*first_at=\$(jq' "$f")"
    [[ -n "$LINE" ]] || { fail "extract first_at line from $copy"; continue; }
    # shellcheck disable=SC2034  # recs/cause are read by the eval'd first_at line, invisible to shellcheck
    run() { local recs="$1" cause="$2" first_at; eval "$LINE"; echo "$first_at"; }

    got=$(run "{\"records\":[$(rec 2026-10-01T00:00:00Z c 1),$(rec 2026-10-02T00:00:00Z c 2),$(rec 2026-11-04T00:00:00Z c 1),$(rec 2026-11-05T00:00:00Z c 2)]}" c)
    [[ "$got" == "2026-11-04T00:00:00Z" ]] && pass "old chain + gap + fresh attempt-1 => fresh chain start" || fail "old chain (got '$got')"
    got=$(run "{\"records\":[$(rec 2026-11-01T00:00:00Z c 1),$(rec 2026-11-02T00:00:00Z c 2)]}" c)
    [[ "$got" == "2026-11-01T00:00:00Z" ]] && pass "single chain => its first record" || fail "single chain (got '$got')"
    got=$(run "{\"records\":[$(rec 2026-10-01T00:00:00Z other 1),$(rec 2026-11-02T00:00:00Z c 1)]}" c)
    [[ "$got" == "2026-11-02T00:00:00Z" ]] && pass "other cause ignored" || fail "other cause (got '$got')"
    got=$(run '{"records":[]}' c)
    [[ -z "$got" ]] && pass "no records => empty" || fail "no records (got '$got')"
done
echo "Tests run: $TESTS_RUN"
[[ $TESTS_FAILED -eq 0 ]] && echo "All tests passed" || { echo "Tests failed: $TESTS_FAILED"; exit 1; }
