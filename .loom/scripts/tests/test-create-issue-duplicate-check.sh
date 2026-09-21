#!/usr/bin/env bash
# test-create-issue-duplicate-check.sh — Tests for create-issue.sh's duplicate
# backstop (#7971).
#
# The backstop exists because prompt-level dedup only ever reaches the roles
# somebody remembered to write it into: on 2026-09-16 three Builders filed the
# SAME bug (#7957/#7960/#7968) within four minutes, because Builder/Doctor/Judge
# — the roles that file issues as a SIDE EFFECT of other work — had no dedup
# step at any layer, and create-issue.sh had none either.
#
# Cases:
#   1. an above-threshold open match BLOCKS the filing (exit 3, nothing filed)
#      and names the match plus the --force escape on stderr.
#   2. no match -> files normally (exit 0, URL on stdout).
#   3. --force files anyway, and does not even run the duplicate check.
#   4. LOOM_SKIP_DUPLICATE_CHECK=1 does the same for a whole burst.
#   5. INTENTIONAL FOLLOW-UP (AC #4): a filing whose body cross-references the
#      matched issue ("Part of #4242") is NOT blocked — decomposition children
#      score high against their parent by construction.
#   6. FAIL-OPEN: check-duplicate.sh exiting 2 (error / rate-limited into no
#      answer at all) files anyway. The #5047 REST fallback path must never
#      acquire a new way to die.
#   7. FAIL-OPEN: NON_DISCRIMINATIVE (#4409 — the scorer reporting it isn't
#      separating anything) files anyway.
#   8. --repo (cross-repo filing) skips the check entirely — check-duplicate.sh
#      searches the working directory's repo and cannot answer for another one.
#   9. --duplicate-threshold is passed through, and rejects a non-numeric value.
#  10. GraphQL EXHAUSTED (#5047): the duplicate check degrades out of the way
#      and the REST fallback still files — the backstop must not put a GraphQL
#      dependency in front of the path that exists for GraphQL exhaustion.
#
# Black-box and hermetic: create-issue.sh + lib/ are copied into a throwaway
# dir next to a STUB check-duplicate.sh and a STUB `gh` on PATH, so no test
# ever reaches the network or files a real issue.

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

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

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}
# Pure-bash substring match (no forked grep) so a transient fork failure under
# the parallel suite pool cannot masquerade as a content mismatch (#7819).
assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 (missing '$2' in: $1)"; fi
}
assert_not_contains() {
    if [[ "$1" != *"$2"* ]]; then pass "$3"; else fail "$3 (unexpectedly found '$2' in: $1)"; fi
}

WORK="$(mktemp -d)"
cleanup() { [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"; }
trap cleanup EXIT

# --- Fixture: a copy of create-issue.sh with stubbed siblings ----------------
FAKE_SCRIPTS="$WORK/scripts"
mkdir -p "$FAKE_SCRIPTS"
cp "$SCRIPTS_DIR/create-issue.sh" "$FAKE_SCRIPTS/"
cp -R "$SCRIPTS_DIR/lib" "$FAKE_SCRIPTS/lib"
CREATE_ISSUE="$FAKE_SCRIPTS/create-issue.sh"

# Stub check-duplicate.sh: behaviour is driven by $STUB_DUP_MODE so each case
# can pick an outcome. It records its own invocation + args for the cases that
# assert the check was (or was NOT) run.
cat > "$FAKE_SCRIPTS/check-duplicate.sh" << 'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_DUP_CALLS:-/dev/null}"
case "${STUB_DUP_MODE:-clean}" in
    clean) exit 0 ;;
    match)
        echo "DUPLICATE_FOUND"
        echo "#4242: sweep-lease-fence.sh:392 repo_args unbound under bash 3.2 (similarity: 34%)"
        exit 1
        ;;
    nondiscriminative)
        echo "NON_DISCRIMINATIVE (open issues): 9 of 12 candidates scored >= 18% similarity -- not discriminative, fall back to manual review."
        exit 1
        ;;
    error) echo "boom" >&2; exit 2 ;;
esac
STUB
chmod +x "$FAKE_SCRIPTS/check-duplicate.sh"

# Stub `gh`: records the create it was asked for and prints a plausible URL.
# With STUB_GH_MODE=ratelimited it reproduces GraphQL exhaustion, so the #5047
# REST fallback (`gh api --method POST`) is the path that answers.
FAKE_BIN="$WORK/bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/gh" << 'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "issue" && "${2:-}" == "create" ]]; then
    printf '%s\n' "$*" >> "${STUB_GH_CREATES:-/dev/null}"
    if [[ "${STUB_GH_MODE:-ok}" == "ratelimited" ]]; then
        echo "GraphQL: API rate limit already exceeded for user ID 1234." >&2
        exit 1
    fi
    echo "https://github.com/example/repo/issues/9999"
    exit 0
fi
if [[ "${1:-}" == "api" ]]; then
    cat > /dev/null
    echo "https://github.com/example/repo/issues/8888"
    exit 0
fi
exit 0
STUB
chmod +x "$FAKE_BIN/gh"

DUP_CALLS="$WORK/dup-calls.log"
GH_CREATES="$WORK/gh-creates.log"

# run_create [env assignments handled by caller] <args...>
# Always: github forge forced, filing lock disabled (covered by
# test-filing-lock.sh), stub gh first on PATH, logs reset.
run_create() {
    : > "$DUP_CALLS"
    : > "$GH_CREATES"
    OUT="$(
        PATH="$FAKE_BIN:$PATH" \
        LOOM_FORGE_TYPE=github \
        LOOM_FILING_LOCK=0 \
        STUB_DUP_MODE="${STUB_DUP_MODE:-clean}" \
        STUB_DUP_CALLS="$DUP_CALLS" \
        STUB_GH_CREATES="$GH_CREATES" \
        STUB_GH_MODE="${STUB_GH_MODE:-ok}" \
        LOOM_SKIP_DUPLICATE_CHECK="${LOOM_SKIP_DUPLICATE_CHECK:-}" \
        bash "$CREATE_ISSUE" "$@" 2>&1
    )"
    RC=$?
}

echo "=== create-issue.sh duplicate backstop (#7971) ==="
echo

# --- 1. Above-threshold open match blocks the filing ------------------------
echo "--- an above-threshold open match blocks the filing ---"
STUB_DUP_MODE=match run_create --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "repo_args[@] is unbound under macOS bash 3.2, so the fence fails open."
assert_eq "$RC" "3" "blocked filing exits 3"
assert_contains "$OUT" "NOT FILED" "stderr says nothing was filed"
assert_contains "$OUT" "#4242" "stderr names the matched issue"
assert_contains "$OUT" "--force" "stderr names the --force escape"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "0" "no issue was created"

echo

# --- 2. No match files normally ---------------------------------------------
echo "--- no match files normally ---"
STUB_DUP_MODE=clean run_create --title "Add a widget" --body "A brand new thing." --label "loom:triage"
assert_eq "$RC" "0" "clean check exits 0"
assert_contains "$OUT" "https://github.com/example/repo/issues/9999" "issue URL on stdout"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "1" "exactly one create"
assert_contains "$(cat "$GH_CREATES")" "loom:triage" "label rode along with the create"
assert_eq "$(wc -l < "$DUP_CALLS" | tr -d ' ')" "1" "the duplicate check ran"

echo

# --- 3. --force files anyway, without running the check ---------------------
echo "--- --force bypasses the backstop ---"
STUB_DUP_MODE=match run_create --force --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "repo_args[@] is unbound under macOS bash 3.2."
assert_eq "$RC" "0" "--force files despite the match"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "1" "the issue was created"
assert_eq "$(wc -l < "$DUP_CALLS" | tr -d ' ')" "0" "--force does not even run the check"

echo "--- --skip-duplicate-check is an alias for --force ---"
STUB_DUP_MODE=match run_create --skip-duplicate-check --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "repo_args[@] is unbound under macOS bash 3.2."
assert_eq "$RC" "0" "--skip-duplicate-check files despite the match"

echo

# --- 4. LOOM_SKIP_DUPLICATE_CHECK=1 skips it for a whole burst --------------
echo "--- LOOM_SKIP_DUPLICATE_CHECK=1 skips the backstop ---"
STUB_DUP_MODE=match LOOM_SKIP_DUPLICATE_CHECK=1 run_create \
    --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "repo_args[@] is unbound under macOS bash 3.2."
assert_eq "$RC" "0" "env skip files despite the match"
assert_eq "$(wc -l < "$DUP_CALLS" | tr -d ' ')" "0" "env skip does not run the check"

echo

# --- 5. Intentional follow-ups are never blocked (AC #4) --------------------
echo "--- a cross-referenced match is an intentional follow-up, not a duplicate ---"
STUB_DUP_MODE=match run_create --title "sweep-lease-fence.sh:392 unbound variable — phase 2" \
    --body "Part of #4242. Splits the remaining bash 3.2 array guards out of the parent."
assert_eq "$RC" "0" "a filing that cross-references the match is NOT blocked"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "1" "the follow-up was created"
assert_contains "$OUT" "intentional follow-up" "stderr explains why it was not blocked"
assert_not_contains "$OUT" "NOT FILED" "no block message"

echo "--- an UNREFERENCED match is still blocked when other refs are present ---"
STUB_DUP_MODE=match run_create --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "Seen while working #9001; the fence fails open under bash 3.2."
assert_eq "$RC" "3" "referencing some OTHER issue does not disarm the backstop"

echo

# --- 6. Fail-open when the duplicate check itself errors --------------------
echo "--- fail-open: check-duplicate.sh error (exit 2) still files ---"
STUB_DUP_MODE=error run_create --title "Something new" --body "Body."
assert_eq "$RC" "0" "an erroring duplicate check does not block the filing"
assert_contains "$OUT" "duplicate check unavailable" "the degradation is announced"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "1" "the issue was created anyway"

echo "--- fail-open: a MISSING check-duplicate.sh still files ---"
mv "$FAKE_SCRIPTS/check-duplicate.sh" "$WORK/check-duplicate.sh.hidden"
run_create --title "Something new" --body "Body."
assert_eq "$RC" "0" "a missing duplicate check does not block the filing"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "1" "the issue was created anyway"
mv "$WORK/check-duplicate.sh.hidden" "$FAKE_SCRIPTS/check-duplicate.sh"

echo

# --- 7. Fail-open on NON_DISCRIMINATIVE (#4409) -----------------------------
echo "--- fail-open: NON_DISCRIMINATIVE still files ---"
STUB_DUP_MODE=nondiscriminative run_create --title "Something new" --body "Body."
assert_eq "$RC" "0" "a non-discriminative result does not block the filing"
assert_contains "$OUT" "no discriminating match" "the degradation is announced"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "1" "the issue was created anyway"

echo

# --- 8. --repo skips the check ----------------------------------------------
echo "--- cross-repo filing skips the check ---"
STUB_DUP_MODE=match run_create --repo "example/other" --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "repo_args[@] is unbound under macOS bash 3.2."
assert_eq "$RC" "0" "--repo filing is not blocked by a local-repo match"
assert_eq "$(wc -l < "$DUP_CALLS" | tr -d ' ')" "0" "--repo does not run the local-repo check"

echo

# --- 9. --duplicate-threshold -----------------------------------------------
echo "--- --duplicate-threshold ---"
STUB_DUP_MODE=clean run_create --duplicate-threshold 40 --title "Something new" --body "Body."
assert_eq "$RC" "0" "a numeric threshold is accepted"
assert_contains "$(cat "$DUP_CALLS")" "--threshold 40" "the threshold is passed to check-duplicate.sh"

STUB_DUP_MODE=clean run_create --duplicate-threshold high --title "Something new" --body "Body."
assert_eq "$RC" "2" "a non-numeric threshold is an argument error"

echo

# --- 10. GraphQL exhausted (#5047): the REST fallback stays reachable -------
# The backstop must not put a GraphQL dependency in front of the path that
# exists precisely FOR GraphQL exhaustion. Under exhaustion the duplicate
# check cannot answer either, so it degrades out of the way and the REST POST
# still files the issue.
echo "--- GraphQL exhausted: duplicate check degrades, REST fallback still files ---"
STUB_DUP_MODE=error STUB_GH_MODE=ratelimited run_create --title "Something new" \
    --body "Body." --label "loom:triage"
assert_eq "$RC" "0" "an exhausted-GraphQL filing still succeeds"
assert_contains "$OUT" "https://github.com/example/repo/issues/8888" "the REST fallback produced the URL"

echo "--- GraphQL exhausted with a duplicate match: still blocks, still no create --"
STUB_DUP_MODE=match STUB_GH_MODE=ratelimited run_create --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "repo_args[@] is unbound under macOS bash 3.2."
assert_eq "$RC" "3" "a match found via check-duplicate.sh's own REST fallback still blocks"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "0" "no create attempt was made at all"

echo
echo "=== $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed ==="
[[ "$TESTS_FAILED" -eq 0 ]]
