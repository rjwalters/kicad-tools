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
#  11. GRADUATED RESPONSE (#8289, re-landed by #8360): a WARN-band
#      ("NEAR #N: …") match warns on stderr and FILES; an at/above-BLOCK-
#      threshold match still blocks, and carries the warn-band rows as
#      context. Band edges are derived from the two flags and passed through
#      to check-duplicate.sh.
#  12. NEVER SILENT (#8289): every non-zero exit says something. The three
#      silent shapes reproduced for #8289 — a forge failure with empty stderr,
#      a forge failure that wrote to stdout, and an exit-0 create that
#      returned no URL — plus the block path's stderr discipline, captured
#      with stdout and stderr SEPARATE.
#
# Black-box and hermetic: create-issue.sh + lib/ are copied into a throwaway
# dir next to a STUB check-duplicate.sh and a STUB `gh` on PATH, so no test
# ever reaches the network or files a real issue. The one exception is the
# END-TO-END case in 11, which swaps in the REAL check-duplicate.sh against
# the stubbed forge — and since #8360 that means the real
# `loom-daemon duplicate-scan` too, so this suite needs a BUILT daemon
# (pinned via tests/lib/require-daemon-bin.sh; wired in the "Native Port
# Suites" CI job, not shell-suite-tests).

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# The e2e case drives the real check-duplicate.sh -> the real daemon scorer.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "duplicate-scan"

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
    near)
        # WARN band only (#8289): context lines, exit 0 -- a near match must
        # NOT move check-duplicate.sh's exit code, so the rows are the only
        # signal create-issue.sh has.
        echo "NEAR_DUPLICATE (13% <= similarity < 18% -- context only, not a duplicate verdict)"
        echo "NEAR #4242: sweep-lease-fence.sh:392 repo_args unbound under bash 3.2 (similarity: 15%)"
        exit 0
        ;;
    nearandmatch)
        echo "DUPLICATE_FOUND"
        echo "#4242: sweep-lease-fence.sh:392 repo_args unbound under bash 3.2 (similarity: 34%)"
        echo "NEAR_DUPLICATE (13% <= similarity < 18% -- context only, not a duplicate verdict)"
        echo "NEAR #4300: bash 3.2 array guards elsewhere in the fence (similarity: 14%)"
        exit 1
        ;;
    error) echo "boom" >&2; exit 2 ;;
esac
STUB
chmod +x "$FAKE_SCRIPTS/check-duplicate.sh"

# Stub `gh`: records the create it was asked for and prints a plausible URL.
# With STUB_GH_MODE=ratelimited it reproduces GraphQL exhaustion, so the #5047
# REST fallback (`gh api --method POST`) is the path that answers. The three
# STUB_GH_MODE=silent* shapes are #8289's reproduced silent exits (case 12).
FAKE_BIN="$WORK/bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/gh" << 'STUB'
#!/usr/bin/env bash
if [[ "${1:-}" == "issue" && "${2:-}" == "create" ]]; then
    printf '%s\n' "$*" >> "${STUB_GH_CREATES:-/dev/null}"
    case "${STUB_GH_MODE:-ok}" in
        ratelimited)
            echo "GraphQL: API rate limit already exceeded for user ID 1234." >&2
            exit 1
            ;;
        # The three silent shapes reproduced for #8289 (see case 12).
        silentfail) exit 1 ;;
        stdouterr) echo "something broke"; exit 1 ;;
        emptyok) exit 0 ;;
    esac
    echo "https://github.com/example/repo/issues/9999"
    exit 0
fi
if [[ "${1:-}" == "issue" && "${2:-}" == "list" ]]; then
    # Only the end-to-end case (11c) reaches this: it runs the REAL
    # check-duplicate.sh, which fetches the open-issue pool from here.
    if [[ -n "${STUB_ISSUE_LIST:-}" && -f "${STUB_ISSUE_LIST:-}" ]]; then
        cat "$STUB_ISSUE_LIST"
    else
        echo "[]"
    fi
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

# Stub loom-daemon on PATH but non-functional, so the REAL check-duplicate.sh
# (end-to-end case 11c) takes its documented `gh` fallback for the FETCH
# instead of whatever loom-daemon the host happens to have installed. The
# SCORER still runs for real: it resolves through $LOOM_DAEMON_SELF_BIN (set
# by the harness above), which script-helper.sh checks before any PATH
# lookup — same fixture rationale as test-check-duplicate.sh.
cat > "$FAKE_BIN/loom-daemon" << 'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$FAKE_BIN/loom-daemon"

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
        STUB_ISSUE_LIST="${STUB_ISSUE_LIST:-}" \
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

# --- 11. Graduated response (#8289, re-landed by #8360) ---------------------
# The backstop used to be a cliff: hard exit 3 at >= 18% similarity, total
# silence at 17%. A score in the 13-17% band is genuinely undecidable in this
# repo (unrelated issues reach 13%; one confirmed duplicate pair scored 13%),
# so it now warns and files instead of doing nothing or blocking.
echo "--- WARN band: a near match warns on stderr and FILES ---"
STUB_DUP_MODE=near run_create --title "Something adjacent" --body "Body."
assert_eq "$RC" "0" "a warn-band match does NOT block the filing"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "1" "the issue was created"
assert_contains "$OUT" "WARNING" "the near match is announced"
assert_contains "$OUT" "FILING ANYWAY" "the message says the filing proceeded"
assert_contains "$OUT" "#4242" "the near match is named"
assert_not_contains "$OUT" "NOT FILED" "a warn-band match is never reported as a block"

echo "--- WARN band: exit code stays 0, so --force is NOT needed to proceed ---"
STUB_DUP_MODE=near run_create --title "Something adjacent" --body "Body."
assert_eq "$RC" "0" "no --force required for a warn-band match"

echo "--- WARN band: a cross-referenced near match is not even warned about ---"
STUB_DUP_MODE=near run_create --title "Phase 2" --body "Part of #4242. Follow-up slice."
assert_eq "$RC" "0" "an intentional follow-up files"
assert_not_contains "$OUT" "WARNING" "a cross-referenced near match is exempt, like a blocking one"

echo "--- BLOCK band: an at-threshold match still blocks, with the band as context ---"
STUB_DUP_MODE=nearandmatch run_create --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "repo_args[@] is unbound under macOS bash 3.2."
assert_eq "$RC" "3" "an at/above-threshold match still hard-blocks"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "0" "nothing was created"
assert_contains "$OUT" "NOT FILED" "the block is announced"
assert_contains "$OUT" "#4242" "the blocking match is named"
assert_contains "$OUT" "18% BLOCK threshold" "the block message states the threshold it applied"
assert_contains "$OUT" "#3550/#3551" "the block message cites the calibration data, not a bare number"
assert_contains "$OUT" "#4300" "warn-band rows are shown as context alongside the block"
assert_contains "$OUT" "NOT a reason for this block" "context rows are labeled as not-the-cause"
assert_contains "$OUT" "--force" "the --force escape is still offered"

echo "--- band edges are derived and passed through to check-duplicate.sh ---"
STUB_DUP_MODE=clean run_create --title "Something new" --body "Body."
assert_contains "$(cat "$DUP_CALLS")" "--warn-threshold 13" "default warn floor is the calibrated 13"

STUB_DUP_MODE=clean run_create --duplicate-threshold 40 --title "Something new" --body "Body."
assert_contains "$(cat "$DUP_CALLS")" "--threshold 40 --warn-threshold 35" \
    "moving the block line carries the 5-point band with it"

STUB_DUP_MODE=clean run_create --duplicate-warn-threshold 5 --title "Something new" --body "Body."
assert_contains "$(cat "$DUP_CALLS")" "--warn-threshold 5" "an explicit warn floor wins"

STUB_DUP_MODE=clean run_create --duplicate-warn-threshold 0 --title "Something new" --body "Body."
assert_not_contains "$(cat "$DUP_CALLS")" "--warn-threshold" "0 disables the band entirely"

STUB_DUP_MODE=clean run_create --duplicate-warn-threshold 30 --title "Something new" --body "Body."
assert_not_contains "$(cat "$DUP_CALLS")" "--warn-threshold" \
    "a warn floor at/above the block line disables the band rather than inverting it"

STUB_DUP_MODE=clean run_create --duplicate-warn-threshold low --title "Something new" --body "Body."
assert_eq "$RC" "2" "a non-numeric warn threshold is an argument error"

echo "--- --force still bypasses everything, warn band included ---"
STUB_DUP_MODE=nearandmatch run_create --force --title "sweep-lease-fence.sh:392 unbound variable" --body "Body."
assert_eq "$RC" "0" "--force files despite a blocking + near match"
assert_eq "$(wc -l < "$DUP_CALLS" | tr -d ' ')" "0" "--force does not run the check at all"

STUB_DUP_MODE=nearandmatch LOOM_SKIP_DUPLICATE_CHECK=1 run_create \
    --title "sweep-lease-fence.sh:392 unbound variable" --body "Body."
assert_eq "$RC" "0" "LOOM_SKIP_DUPLICATE_CHECK=1 files despite a blocking + near match"

echo "--- END-TO-END against the REAL check-duplicate.sh (wire format, both bands) ---"
# Every case above stubs check-duplicate.sh, so nothing there would notice if
# the two scripts disagreed about the NEAR row format. This case runs the real
# scorer (real check-duplicate.sh -> real `loom-daemon duplicate-scan`) against
# a stubbed forge, so the NEAR wire format cannot drift. NATO-word fixture,
# query {alpha,bravo,charlie,delta} with --duplicate-threshold 30 (=> warn
# floor 25):
#   #701 {alpha,bravo,echo,foxtrot}            -> 2/(4+4-2) = 33% BLOCK
#   #702 {alpha,bravo,echo,foxtrot,golf,hotel} -> 2/(4+6-2) = 25% WARN
mv "$FAKE_SCRIPTS/check-duplicate.sh" "$WORK/check-duplicate.stub"
cp "$SCRIPTS_DIR/check-duplicate.sh" "$FAKE_SCRIPTS/check-duplicate.sh"

cat > "$WORK/issues-near.json" << 'EOF'
[{"number": 702, "title": "Alpha Bravo Echo Foxtrot Golf Hotel", "body": ""}]
EOF
STUB_ISSUE_LIST="$WORK/issues-near.json" run_create --duplicate-threshold 30 \
    --title "Alpha Bravo Charlie Delta" --body ""
assert_eq "$RC" "0" "(e2e) a real 25% score in the warn band files"
assert_contains "$OUT" "WARNING" "(e2e) …and the real NEAR row is recognized by create-issue.sh"
assert_contains "$OUT" "#702" "(e2e) …naming the near match"
assert_contains "$OUT" "(similarity: 25%)" "(e2e) …with the score the scorer actually produced"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "1" "(e2e) …and the issue was created"

cat > "$WORK/issues-block.json" << 'EOF'
[{"number": 701, "title": "Alpha Bravo Echo Foxtrot", "body": ""}]
EOF
STUB_ISSUE_LIST="$WORK/issues-block.json" run_create --duplicate-threshold 30 \
    --title "Alpha Bravo Charlie Delta" --body ""
assert_eq "$RC" "3" "(e2e) a real 33% score at/above the block line still blocks"
assert_contains "$OUT" "#701" "(e2e) …naming the blocking match"
assert_eq "$(wc -l < "$GH_CREATES" | tr -d ' ')" "0" "(e2e) …and nothing was created"

mv "$WORK/check-duplicate.stub" "$FAKE_SCRIPTS/check-duplicate.sh"

echo "--- fail-open now quotes WHY the check could not answer ---"
STUB_DUP_MODE=error run_create --title "Something new" --body "Body."
assert_contains "$OUT" "check-duplicate.sh said: boom" "the checker's stderr is surfaced, not discarded"

echo

# --- 12. Never silent (#8289) -----------------------------------------------
# The other half of the report: "no URL and no refusal text". Captured with
# stdout and stderr SEPARATE, because the assertion is precisely that stderr
# is non-empty whenever stdout carries no URL.
echo "--- no exit path is silent ---"
run_create_split() {
    : > "$GH_CREATES"
    STDOUT="$(
        PATH="$FAKE_BIN:$PATH" \
        LOOM_FORGE_TYPE=github \
        LOOM_FILING_LOCK=0 \
        STUB_DUP_MODE="${STUB_DUP_MODE:-clean}" \
        STUB_DUP_CALLS="$DUP_CALLS" \
        STUB_GH_CREATES="$GH_CREATES" \
        STUB_GH_MODE="${STUB_GH_MODE:-ok}" \
        bash "$CREATE_ISSUE" "$@" 2> "$WORK/stderr.txt"
    )"
    RC=$?
    STDERR="$(cat "$WORK/stderr.txt")"
}

STUB_GH_MODE=silentfail run_create_split --title "Something new" --body "Body."
assert_eq "$RC" "1" "a forge failure with empty stderr still exits non-zero"
assert_contains "$STDERR" "no error text (exit 1)" "…and names the exit code the forge died with"
assert_contains "$STDERR" "check the forge before re-filing" "…and says what to do instead of retrying"

STUB_GH_MODE=stdouterr run_create_split --title "Something new" --body "Body."
assert_eq "$RC" "1" "a forge failure that wrote to stdout still exits non-zero"
assert_contains "$STDERR" "something broke" "…and the misrouted error text is surfaced on stderr"

STUB_GH_MODE=emptyok run_create_split --title "Something new" --body "Body."
assert_eq "$RC" "1" "an exit-0 create with no URL is a failure, not a silent success"
assert_contains "$STDERR" "no usable issue URL" "…and says the URL was missing"
assert_contains "$STDERR" "MAY exist" "…and warns against a blind retry"
assert_eq "$STDOUT" "" "…and emits no bogus empty URL on stdout"

STUB_DUP_MODE=match run_create_split --title "sweep-lease-fence.sh:392 unbound variable" \
    --body "repo_args[@] is unbound under macOS bash 3.2."
assert_eq "$RC" "3" "the duplicate block path exits 3"
assert_contains "$STDERR" "NOT FILED" "…with its refusal text on stderr (never stdout-only)"
assert_eq "$STDOUT" "" "…and no URL on stdout"

echo
echo "=== $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed ==="
[[ "$TESTS_FAILED" -eq 0 ]]
