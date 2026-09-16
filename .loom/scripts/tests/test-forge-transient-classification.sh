#!/usr/bin/env bash
# test-forge-transient-classification.sh - Unit tests for the forge-transient
# vs. credential/permission fault discrimination in forge-helpers.sh
# (issue #6425).
#
# Incident (2026-08-17): during a confirmed GitHub partial outage, two sweeps
# hit forge WRITE failures and wrote a confident CREDENTIAL diagnosis into
# their operator-facing summaries -- "this needs operator attention, not a
# retry ... the GitHub App installation token lacking write permission" --
# complete with an "Action needed from you" line. Both were wrong: the first
# PR merged normally 17 minutes later with no permission change, and the
# second repo's writes resumed once GitHub recovered. Nothing about the App
# installation had changed in either case.
#
# This file tests:
#   1. is_forge_transient_error() fires on the outage-shaped signatures (5xx,
#      "No server is currently available", connection resets) and does NOT
#      fire on an isolated, uncorroborated permission-scope 403 or unrelated
#      text.
#   2. forge_write_permission_confirmed(): confirms a permission fault ONLY
#      when the write error is non-transient AND a same-credential read
#      probe succeeds; a failing read (evidence of an outage) or a
#      forge-transient write error never confirms.
#   3. Fixture regression: the two wrong summary shapes from the incident,
#      run through the classifiers on their own quoted write-failure text,
#      must NOT be classified as a confirmed permission fault -- proving the
#      new classifier would have caught both misdiagnoses.
#
# Usage:
#   ./.loom/scripts/tests/test-forge-transient-classification.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

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

# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

# --- 1. is_forge_transient_error() signature table --------------------------
echo "Testing is_forge_transient_error() signature table..."

forge_transient_positives=(
    "http503:HTTP 503: No server is currently available to service your request (https://api.github.com/repos/o/r/issues)"
    "http500:HTTP 500: Internal Server Error"
    "http502:HTTP 502: Bad Gateway"
    "http504:HTTP 504: Gateway Timeout"
    "no_server:no server is currently available to service your request"
    "service_unavailable:Service Unavailable"
    "conn_reset:Post https://api.github.com/...: read: connection reset by peer"
    "econnreset:Error: connect ECONNRESET"
    "econnrefused:dial tcp: connect: connection refused (ECONNREFUSED)"
)
for entry in "${forge_transient_positives[@]}"; do
    name="${entry%%:*}"
    value="${entry#*:}"
    if is_forge_transient_error "$value"; then
        pass "is_forge_transient_error fires on $name"
    else
        fail "is_forge_transient_error must fire on $name: '$value'"
    fi
done

forge_transient_negatives=(
    "plain_403:HTTP 403: Resource not accessible by integration (https://api.github.com/repos/o/r/pulls)"
    "auth_401:HTTP 401: Bad credentials"
    "not_found:HTTP 404: Not Found"
    "validation:HTTP 422: Validation Failed"
    "issue_number:merged PR #503 successfully"
    "rate_limit:API rate limit exceeded for installation ID 1"
    "unrelated:build failed: 2 tests did not pass"
)
for entry in "${forge_transient_negatives[@]}"; do
    name="${entry%%:*}"
    value="${entry#*:}"
    if is_forge_transient_error "$value"; then
        fail "is_forge_transient_error false-positived on $name: '$value'"
    else
        pass "is_forge_transient_error does NOT fire on $name"
    fi
done

# A PR/issue number that happens to look like an HTTP status code must not
# false-positive -- anchored on "http 5xx", not a bare "500"/"502"/... digit
# substring (issue #6425's precision requirement for operator-facing text).
if is_forge_transient_error "closes #500, part of epic #502"; then
    fail "is_forge_transient_error must not false-positive on issue/PR numbers 500/502"
else
    pass "is_forge_transient_error ignores bare issue/PR numbers that look like status codes"
fi

# --- 2. forge_write_permission_confirmed() ----------------------------------
echo ""
echo "Testing forge_write_permission_confirmed()..."

STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT
GH_MODE_FILE="$STUB_DIR/gh-mode.txt"
export GH_MODE_FILE

# A `gh` stub answering `gh api /rate_limit` per $GH_MODE_FILE; any other
# invocation is unused by this function and always "succeeds" harmlessly.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2" == "api /rate_limit" ]]; then
    mode="$(cat "$GH_MODE_FILE" 2>/dev/null || echo read-ok)"
    if [[ "$mode" == "read-ok" ]]; then
        echo '{"resources":{"core":{"limit":5000,"remaining":4999}}}'
        exit 0
    else
        echo "HTTP 403: Resource not accessible by integration" >&2
        exit 1
    fi
fi
exit 0
STUB
chmod +x "$STUB_DIR/gh"

_with_gh_stub() {
    local mode="$1"
    shift
    echo "$mode" > "$GH_MODE_FILE"
    PATH="$STUB_DIR:$PATH" "$@"
}

# Read succeeds while the write 403s on a non-transient error -> CONFIRMED.
rc=0
_with_gh_stub read-ok forge_write_permission_confirmed \
    "HTTP 403: Resource not accessible by integration" || rc=$?
if [[ $rc -eq 0 ]]; then
    pass "forge_write_permission_confirmed: confirms when the read succeeds and the write 403s"
else
    fail "forge_write_permission_confirmed: must confirm when the read succeeds and the write 403s"
fi

# Read ALSO fails -> NOT confirmed, even though the write error looks
# permission-shaped in isolation. This is the exact gap in the second
# incident summary, which recorded `gh api /user` also 403'ing and still
# concluded "permissions".
rc=0
_with_gh_stub read-fail forge_write_permission_confirmed \
    "HTTP 403: Resource not accessible by integration" || rc=$?
if [[ $rc -ne 0 ]]; then
    pass "forge_write_permission_confirmed: does NOT confirm when the read also fails"
else
    fail "forge_write_permission_confirmed: must NOT confirm when the read also fails (outage evidence)"
fi

# The write error is itself forge-transient (5xx) -> NOT confirmed, and the
# read probe must not even be consulted (short-circuit).
rc=0
_with_gh_stub read-ok forge_write_permission_confirmed \
    "HTTP 503: No server is currently available to service your request" || rc=$?
if [[ $rc -ne 0 ]]; then
    pass "forge_write_permission_confirmed: does NOT confirm on a forge-transient write error"
else
    fail "forge_write_permission_confirmed: must NOT confirm on a forge-transient write error"
fi

# --- 3. Fixture regression: the two wrong incident summary shapes ----------
echo ""
echo "Testing the two incident summary shapes as the WRONG outputs (#6425)..."

# Shape 1: "Merge attempt failed ... this needs operator attention, not a
# retry: ./.loom/scripts/merge-pr.sh <N> --auto -> 403 Resource not
# accessible by integration." The PR merged normally 17 minutes later; the
# only quoted evidence is the single write 403 with no corroborating read
# check, so the classifier must NOT confirm a permission fault from this text
# alone.
INCIDENT_1_WRITE_ERROR="403 Resource not accessible by integration"
rc=0
_with_gh_stub read-fail forge_write_permission_confirmed "$INCIDENT_1_WRITE_ERROR" || rc=$?
if [[ $rc -ne 0 ]]; then
    pass "incident shape 1 (merge-pr.sh 403, no corroborating read): not confirmed as a permission fault"
else
    fail "incident shape 1 must not be confirmed as a permission fault without a successful read probe"
fi

# Shape 2: "all write access ... is currently down for this session's GitHub
# App token ... confirmed via gh issue edit --add-label (2 attempts), gh api
# .../labels ... [and] gh api /user also 403'd." The write failed AND the
# read probe (gh api /user, the sibling of /rate_limit) also failed on the
# same token -- textbook outage evidence, not a scoped permission gap.
INCIDENT_2_WRITE_ERROR="HTTP 403: Resource not accessible by integration (gh issue edit --add-label)"
rc=0
_with_gh_stub read-fail forge_write_permission_confirmed "$INCIDENT_2_WRITE_ERROR" || rc=$?
if [[ $rc -ne 0 ]]; then
    pass "incident shape 2 (write 403 + read also failing): not confirmed as a permission fault"
else
    fail "incident shape 2 must not be confirmed when the read probe also fails (outage evidence)"
fi

# The fleet's own observed forge-transient signature during the incident --
# `HTTP 503: No server is currently available` -- must classify as
# forge-transient outright, independent of any read probe.
if is_forge_transient_error "HTTP 503: No server is currently available"; then
    pass "the incident's own claim_reconciliation 503 classifies as forge-transient"
else
    fail "the incident's own claim_reconciliation 503 must classify as forge-transient"
fi

# --- 4. sweep.md carries the operator-facing guidance -----------------------
echo ""
echo "Testing sweep.md documents the forge write failure diagnosis policy (#6425)..."

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (HELPERS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (HELPERS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#447).
if [[ -d "$HELPERS_DIR/../../.claude/commands/loom" ]]; then
    SWEEP_SKILL_DIR="$(cd "$HELPERS_DIR/../../.claude/commands/loom" && pwd)"
else
    SWEEP_SKILL_DIR="$(cd "$HELPERS_DIR/../.claude/commands/loom" && pwd)"
fi

# The /loom:sweep skill in document order (#7726 split the monolithic
# sweep.md into a dispatcher + 11 sibling reference files). Concatenate them
# into one file (in $STUB_DIR, already cleaned up by the trap above) so both
# checks below find their content wherever it now lives — mirrors
# SWEEP_SKILL_FILES in loom-daemon/tests/sweep_md_doc_lint.rs.
SWEEP_SKILL_FILES=(
    sweep.md
    sweep-arguments.md
    sweep-examples.md
    sweep-execution-model.md
    sweep-backend-detection.md
    sweep-scheduling-signals.md
    sweep-dry-run.md
    sweep-mode-c-lifecycle.md
    sweep-wave-lifecycle.md
    sweep-summary-output.md
    sweep-run-hygiene.md
    sweep-reference.md
)
SWEEP_MD="$STUB_DIR/sweep-skill-concat.md"
: > "$SWEEP_MD"
for f in "${SWEEP_SKILL_FILES[@]}"; do
    path="$SWEEP_SKILL_DIR/$f"
    [[ -r "$path" ]] && cat "$path" >> "$SWEEP_MD"
done

if [[ -s "$SWEEP_MD" ]]; then
    if grep -q "forge-transient" "$SWEEP_MD"; then
        pass "sweep.md references the forge-transient classification"
    else
        fail "sweep.md must reference the forge-transient classification (#6425)"
    fi
    if grep -qi "forge_write_permission_confirmed" "$SWEEP_MD"; then
        pass "sweep.md points callers at forge_write_permission_confirmed for the positive-evidence check"
    else
        fail "sweep.md must point callers at forge_write_permission_confirmed (#6425)"
    fi
else
    fail "sweep.md / sibling skill files not found under $SWEEP_SKILL_DIR"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
