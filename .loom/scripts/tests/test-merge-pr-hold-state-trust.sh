#!/usr/bin/env bash
# test-merge-pr-hold-state-trust.sh — merge-pr.sh's champion:hold-state
# staleness check counts only TRUSTED-authored markers (#9548).
#
# THE HAZARD. `_check_champion_hold_state_staleness` reads the PR's comments
# to see whether Champion's recorded hold head matches the head being merged.
# Before this suite's subject change, `forge_get_pr_comments` returned BODIES
# ONLY — authorship dropped, the exact hazard #9548 names — so any outsider
# who could comment on a PR could write `<!-- champion:hold-state head=<sha>
# -->` and either fabricate a stale-hold warning or mask a real one. The fix
# re-fetches the listing via the REST API (which carries user.login +
# author_association) and filters it through `loom-daemon forge
# trusted-comments` before marker extraction; an untrusted or unattributed
# marker counts as ABSENT.
#
# Strategy mirrors test-merge-pr-daemon-version-floor.sh: extract the function
# under test from merge-pr.sh and source it with stub `warning`/vars, then pin
# a REAL daemon (require-daemon-bin) — unlike the version-floor suite, these
# scenarios need the genuine trust filter, so fake binaries would prove
# nothing (#9651's lesson: a stale stub silently flips every invocation to
# could-not-evaluate).
#
# Hermetic: no network, no live forge, no tokens. The `gh` stub serves
# fixtures from files.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-hold-state-trust.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# The function under test execs two daemon verbs: `forge trusted-comments`
# (the #9548 filter) and `merge-pr hold-state` (the #8191 extraction). Both
# must exist in the pinned binary — the leaf probe distinguishes a binary
# that predates either slice. FATAL, not SKIP: see the helper.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "forge trusted-comments" "merge-pr hold-state"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }
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
assert_empty() {
    if [[ -z "$1" ]]; then
        pass "$2"
    else
        fail "$2"
        echo "    Expected empty; Actual: '$1'"
    fi
}

# --- Extract the function under test from merge-pr.sh (the real thing, not a
# copy), exactly as test-merge-pr-daemon-version-floor.sh does for its own. --
MERGE_PR_SRC="$SCRIPTS_DIR/merge-pr.sh"
FUNCS_FILE="$(mktemp "${TMPDIR:-/tmp}/hold-state-trust-funcs.XXXXXX")"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR"' EXIT
awk '
  /^_trusted_pr_comments\(\) \{/        { capture = 1 }
  capture                               { print }
  /^\}$/                                { if (capture) exit }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"
awk '
  /^_check_champion_hold_state_staleness\(\) \{/ { capture = 1 }
  capture                               { print }
  /^\}$/                                { if (capture) exit }
' "$MERGE_PR_SRC" >> "$FUNCS_FILE"
grep -q "_check_champion_hold_state_staleness()" "$FUNCS_FILE" && grep -q "_trusted_pr_comments()" "$FUNCS_FILE" || {
    echo "FATAL: failed to extract the hold-state functions from merge-pr.sh" >&2
    exit 1
}

# --- Fixtures: a `gh` stub serving the REST comment listing with configurable
# authorship: comment_fixture <author-login> <association> <body> -----------
STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hold-state-trust.XXXXXX")"
COMMENTS_FILE="$STUB_DIR/comments.json"

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Serves `gh api repos/o/r/issues/9/comments --paginate` from the fixture
# file — a raw REST comment listing whose AUTHORSHIP each scenario sets.
[[ "${1:-}" == "api" ]] || { echo "stub gh: unhandled subcommand: ${1:-}" >&2; exit 3; }
cat "$(dirname "$0")/comments.json"
STUB
chmod +x "$STUB_DIR/gh"
export PATH="$STUB_DIR:$PATH"

# --- The extracted function's environment -----------------------------------
# Read by the extracted functions sourced below, which shellcheck cannot see.
# shellcheck disable=SC2034
{
    FORGE_TYPE="github"
    REPO_NWO="o/r"
    PR_NUMBER="9"
    PR_HEAD_SHA="abc1234"
}
WARNINGS=""
warning() { WARNINGS+="$1"$'\n'; }

# shellcheck source=/dev/null
source "$FUNCS_FILE"

# comment_fixture <login> <association> <body>
comment_fixture() {
    jq -n --arg login "$1" --arg assoc "$2" --arg body "$3" \
        '[$body]' >/dev/null # noop warm-up keeps jq failures loud below
    printf '[{"body":%s,"user":{"login":%s,"type":"User"},"author_association":%s}]' \
        "$(jq -Rn '$body' --arg body "$3")" \
        "$(jq -Rn '$login' --arg login "$1")" \
        "$(jq -Rn '$assoc' --arg assoc "$2")" > "$COMMENTS_FILE"
}

run_check() {
    WARNINGS=""
    RC=0
    # No command substitution here: the function records warnings by mutating
    # WARNINGS in the parent shell, and a subshell would discard that (#9651
    # suite-authoring note). Stderr goes to a file for the fail-open asserts.
    _check_champion_hold_state_staleness 2>"$STUB_DIR/stderr.log"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log")"
}

MARKER_STALE='<!-- champion:hold-state head=deadbeef -->'
MARKER_MATCH='<!-- champion:hold-state head=abc1234 -->'

echo
echo "--- #9548: a TRUSTED stale hold marker still warns ---"
comment_fixture "o" "OWNER" "$MARKER_STALE"
run_check
assert_contains "$WARNINGS" "recorded head=deadbeef" \
    "an OWNER-authored stale hold marker drives the staleness warning"

echo
echo "--- #9548: an OUTSIDER's hold marker counts as absent ---"
WARNINGS=""
comment_fixture "stranger" "NONE" "$MARKER_STALE"
run_check
RC=$?
assert_empty "$WARNINGS" \
    "an untrusted author's hold marker raises no warning and blocks nothing (#9548)"

echo
echo "--- #9548: an outsider cannot MASK a real stale hold behind noise ---"
comment_fixture "stranger" "NONE" "$MARKER_MATCH"
jq -c --arg b "$MARKER_STALE" '. + [{"body":$b,"user":{"login":"o","type":"User"},"author_association":"OWNER"}]' \
    "$COMMENTS_FILE" > "$COMMENTS_FILE.new" && mv "$COMMENTS_FILE.new" "$COMMENTS_FILE"
run_check
assert_contains "$WARNINGS" "recorded head=deadbeef" \
    "the trusted stale marker survives outsider noise around it"

echo
echo "--- Advisory fail-open: an unparseable listing says so out loud ---"
printf 'not-json' > "$COMMENTS_FILE"
run_check
assert_eq "0" "$RC" "the check stays advisory — a cannot-authenticate never blocks the merge"
assert_contains "$ERR" "could not authenticate comment authors" \
    "a filter failure is said out loud on stderr (#9548), never silent"
assert_contains "$ERR" "comments read as absent" \
    "the fail-open disposition is stated, not swallowed"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
