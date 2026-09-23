#!/usr/bin/env bash
# test-judge-tdd-merge-base.sh - Behavioral coverage for issue #8265.
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# judge.md's "Test-First (TDD) Claim Verification" verdict table used to accept
# a `TDD: yes — <path>` claim on PATH PRESENCE ALONE ("Referenced path is in
# the changed-files list -> Accept as verified — no further action needed").
# That answers "did a test file with that name change?", never "does that test
# fail without the fix?" — the only thing `TDD: yes` asserts. Four tests in one
# 2026-09-16/17 session were in the diff and still worthless; the decisive one
# (PR #8092 Test 12C) ran 189/189 green against a merge-base tree where its own
# grep target had zero occurrences, because it exercised a mirror helper
# defined inside the test file instead of the shipped code.
#
# WHAT THIS SUITE DOES
#
# Deliberately NOT a prose-existence assertion (loom-daemon/tests/README.md ->
# "Markdown Doc-Lint Tests: No New Prose-Existence Assertions", #7992/#7979).
# Two halves, both behavioral:
#
#   1. EXECUTE THE SHIPPED FUNCTION. `tdd_merge_base_run()` lives in a fenced
#      code block in judge-reference.md -> "Merge-Base Run for a `TDD: yes`
#      Claim". This suite EXTRACTS that fence from the shipped markdown and
#      RUNS it (the defaults/scripts/tests/test-guide-*.sh pattern) against
#      purpose-built git fixtures — including a faithful reconstruction of the
#      #8092 Test 12C mirror-helper shape, which must come back CONTRADICTED.
#      A reworded heading or paragraph does not fail this; a behavior change
#      does.
#   2. PARSE THE SHIPPED VERDICT TABLE. judge.md's TDD table is read as a
#      table (split on `|`, classify the disposition column), not grepped for
#      sentences: every `yes — <path>` row must be reachable, the merge-base
#      "passes" row must be Blocking, the "cannot be run" row must be
#      Advisory, and — the actual regression guard — NO `yes` row may accept
#      on changed-files-list presence without mentioning the merge base.
#      The `TDD: no` / absent rows are asserted UNCHANGED (#8265 AC).
#
# Hermetic: local git fixtures under a private TMPDIR. No forge, no network.
#
# Usage:
#   ./.loom/scripts/tests/test-judge-tdd-merge-base.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Installed layout (.loom/scripts/tests -> two `..` to repo root) vs. this
# source repo (defaults/scripts/tests -> one `..`). Probe both (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
JUDGE_MD="$PROMPT_DIR/judge.md"
JUDGE_REF_MD="$PROMPT_DIR/judge-reference.md"

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
        fail "$msg (missing '$needle' in: ${haystack//$'\n'/ | })"
    fi
}

for f in "$JUDGE_MD" "$JUDGE_REF_MD"; do
    [[ -f "$f" ]] || { echo "FATAL: subject not found: $f" >&2; exit 1; }
done

# =====================================================================
# Extract tdd_merge_base_run() from the SHIPPED markdown and source it.
# Everything below runs the real shipped code, not a local mirror — a
# mirror is precisely the defect #8265 was filed about.
# =====================================================================
FN_SRC="$(awk '
    /^tdd_merge_base_run\(\) \{$/ { infn = 1 }
    infn { print }
    infn && /^\}$/ { exit }
' "$JUDGE_REF_MD")"

if [[ -z "$FN_SRC" || "$FN_SRC" != *"git archive"* ]]; then
    echo "FATAL: could not extract tdd_merge_base_run() from $JUDGE_REF_MD" >&2
    echo "       judge.md's TDD verdict table depends on it; see #8265." >&2
    exit 1
fi

if ! bash -n <(printf '%s\n' "$FN_SRC"); then
    echo "FATAL: the shipped tdd_merge_base_run() is not valid bash" >&2
    exit 1
fi
pass "tdd_merge_base_run() extracted from the shipped judge-reference.md and parses as bash"

# shellcheck disable=SC1090
source <(printf '%s\n' "$FN_SRC")

# =====================================================================
# Fixture: a two-commit repo. BASE has the bug; HEAD has the fix plus
# three candidate "TDD: yes" tests of differing quality.
# =====================================================================
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# Private TMPDIR so the leak check below can't see anyone else's temp dirs.
export TMPDIR="$WORK/tmp"
mkdir -p "$TMPDIR"

REPO="$WORK/repo"
mkdir -p "$REPO/t"
cd "$REPO" || exit 1

git init -q .
git config user.email "test@loom.test"
git config user.name "Loom Test"
git config commit.gpgsign false

# --- BASE commit: the bug. `greet` forgets the name. ---
cat > impl.sh <<'EOF'
#!/usr/bin/env bash
greet() { echo "hello"; }
EOF
chmod +x impl.sh
git add -A >/dev/null
git commit -qm "base: buggy greet"
BASE="$(git rev-parse HEAD)"

# --- HEAD commit: the fix, plus three tests. ---
cat > impl.sh <<'EOF'
#!/usr/bin/env bash
greet() { echo "hello $1"; }
EOF

# (1) A real test: sources the shipped implementation and asserts the fix.
cat > t/real.sh <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
. ./impl.sh
[ "$(greet world)" = "hello world" ] || { echo "greet dropped its argument"; exit 1; }
echo "1/1 passed"
EOF

# (2) The #8092 Test 12C shape: a MIRROR of the logic, defined inside the test
#     file itself. It never reads impl.sh, so it is green on every tree.
cat > t/mirror.sh <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
greet() { echo "hello $1"; }   # mirror, not the shipped function
[ "$(greet world)" = "hello world" ] || exit 1
echo "189/189 passed"
EOF

# (3) The #8093 shape: a suite run under `set -uo pipefail` with NO `set -e`.
#     Its head-only helper is absent at the merge base, bash prints the error
#     and keeps going, and the suite reports green anyway. Nothing about the
#     PR's own green run reveals this.
cat > t/no_set_e.sh <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
. ./lib/helper.sh
echo "1/1 passed"
EOF
mkdir -p lib
echo 'helper() { :; }' > lib/helper.sh

# (4) A probe that reports which impl.sh it actually loaded, then fails. Used
#     to prove the base tree really is UNFIXED — only the named test file is
#     taken from head.
cat > t/shows_impl.sh <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
. ./impl.sh
echo "greet-says: $(greet world)"
exit 1
EOF

chmod +x t/real.sh t/mirror.sh t/no_set_e.sh t/shows_impl.sh
git add -A >/dev/null
git commit -qm "head: fix greet, add tests"

echo
echo "--- tdd_merge_base_run: a real test fails at the merge base (VERIFIED) ---"

out="$(tdd_merge_base_run "$BASE" t/real.sh bash t/real.sh)"
rc=$?
assert_eq "0" "$rc" "a test that fails without the fix exits 0 (claim verified)"
assert_contains "$out" "VERIFIED" "...and says VERIFIED"
assert_contains "$out" "greet dropped its argument" \
    "...and echoes the failure output, so the reviewer can confirm it failed for the RIGHT reason"

echo
echo "--- tdd_merge_base_run: the #8092 Test 12C mirror-helper shape (CONTRADICTED) ---"

# This is the case the old path-presence-only row accepted as verified: the
# path IS in the diff, the test IS green on the PR, and it proves nothing.
out="$(tdd_merge_base_run "$BASE" t/mirror.sh bash t/mirror.sh)"
rc=$?
assert_eq "1" "$rc" "a test that passes at the merge base exits 1 (claim contradicted)"
assert_contains "$out" "CONTRADICTED" "...and says CONTRADICTED"
assert_contains "$out" "189/189 passed" "...and shows the misleading green output it is rejecting"

echo
echo "--- tdd_merge_base_run: only the named test file comes from head ---"

# If the fix leaked into the base tree, every test would look green there and
# the whole check would invert. The probe reports which impl.sh it loaded.
out="$(tdd_merge_base_run "$BASE" t/shows_impl.sh bash t/shows_impl.sh)"
rc=$?
assert_eq "0" "$rc" "the probe fails at the merge base (VERIFIED)"
assert_contains "$out" "greet-says: hello" "the base tree supplies its own files"
if [[ "$out" == *"greet-says: hello world"* ]]; then
    fail "the PR's FIX leaked into the merge-base tree — only the named test file may come from head"
else
    pass "the PR's fix did NOT leak into the merge-base tree (only the named test file came from head)"
fi

echo
echo "--- tdd_merge_base_run: unrunnable cases are distinguished from failures ---"

out="$(tdd_merge_base_run "$BASE" t/real.sh no-such-runner-8265 t/real.sh)"
rc=$?
assert_eq "3" "$rc" "a missing run command (exit 127) exits 3, not 0 — never a false VERIFIED"
assert_contains "$out" "UNRUNNABLE" "...and says UNRUNNABLE"

out="$(tdd_merge_base_run "0000000000000000000000000000000000000000" t/real.sh bash t/real.sh)"
rc=$?
assert_eq "3" "$rc" "a merge base that cannot be materialised exits 3 (UNRUNNABLE)"
assert_contains "$out" "UNRUNNABLE" "...and says UNRUNNABLE"

out="$(tdd_merge_base_run "$BASE" t/no_such_test.sh bash t/no_such_test.sh)"
rc=$?
assert_eq "3" "$rc" "a test path absent from HEAD exits 3 (UNRUNNABLE), not 0"

echo
echo "--- tdd_merge_base_run: the #8093 'set -uo pipefail, no set -e' shape ---"

# The suite's own helper fails to load at the merge base, bash keeps going,
# and the suite still reports green. A path check sees a changed test file; a
# merge-base run sees a suite that proves nothing.
out="$(tdd_merge_base_run "$BASE" t/no_set_e.sh bash t/no_set_e.sh)"
rc=$?
assert_eq "1" "$rc" "a suite that swallows its own load error and reports green is CONTRADICTED"
assert_contains "$out" "1/1 passed" "...and the misleading green line is shown to the reviewer"
assert_contains "$out" "lib/helper.sh" \
    "...alongside the swallowed error, so the reason is diagnosable from the verdict"

echo
echo "--- tdd_merge_base_run: leaves no trace ---"

assert_eq "" "$(git status --porcelain)" "the PR head working tree is untouched by the runs above"
assert_eq "" "$(ls -A "$TMPDIR")" "no merge-base temp tree is left behind"

echo
echo "--- judge.md verdict table: parsed as a table, not grepped for prose ---"

# Slice the verdict table out of the TDD section, then split each row on '|'
# into (claim, evidence, action). Assertions below classify the ACTION column
# rather than pinning any sentence, so a reword passes and a rule change fails.
TABLE="$(awk '
    /^## Test-First \(TDD\) Claim Verification$/ { insec = 1; next }
    insec && /^## / { exit }
    insec && /^\| / && !/^\|---/ && !/^\| `TDD:` line/ { print }
' "$JUDGE_MD")"

row_field() { printf '%s' "$1" | awk -F'|' -v n="$2" '{ print $(n+1) }'; }

yes_rows=0
yes_accept_on_path_alone=0
have_verified_row=0
have_blocking_pass_row=0
have_advisory_unrunnable_row=0
have_blocking_missing_path_row=0
absent_row_advisory=0
no_rows=0
no_rows_nonblocking=0

while IFS= read -r row; do
    [[ -z "$row" ]] && continue
    claim="$(row_field "$row" 1)"
    evidence="$(row_field "$row" 2)"
    action="$(row_field "$row" 3)"
    blocking=0
    [[ "$action" == *"**Blocking.**"* ]] && blocking=1
    mentions_base=0
    [[ "$evidence" == *"merge base"* ]] && mentions_base=1

    case "$claim" in
        *"Absent entirely"*)
            [[ "$action" == *"Advisory"* && $blocking -eq 0 ]] && absent_row_advisory=1
            ;;
        *'`no — <reason>`'*)
            no_rows=$((no_rows + 1))
            [[ $blocking -eq 0 ]] && no_rows_nonblocking=$((no_rows_nonblocking + 1))
            ;;
        *'`yes — <path>`'*)
            yes_rows=$((yes_rows + 1))
            if [[ "$action" == *"Accept as verified"* ]]; then
                if [[ $mentions_base -eq 1 && "$evidence" == *"fails"* ]]; then
                    have_verified_row=1
                else
                    yes_accept_on_path_alone=$((yes_accept_on_path_alone + 1))
                fi
            elif [[ $blocking -eq 1 && $mentions_base -eq 1 && "$evidence" == *"passes"* ]]; then
                have_blocking_pass_row=1
            elif [[ $blocking -eq 1 && "$evidence" == *"not** in the changed-files list"* ]]; then
                have_blocking_missing_path_row=1
            elif [[ "$action" == *"Advisory"* && "$evidence" == *"cannot be run"* ]]; then
                have_advisory_unrunnable_row=1
            fi
            ;;
    esac
done <<< "$TABLE"

assert_eq "4" "$yes_rows" "judge.md's table has four \`yes — <path>\` rows (verified / passes / unrunnable / missing path)"

# THE regression guard for #8265: no row may grant "verified" on the
# changed-files check alone.
assert_eq "0" "$yes_accept_on_path_alone" \
    "no \`yes\` row accepts as verified without a merge-base result (the #8265 defect)"

assert_eq "1" "$have_verified_row" \
    "the accepting row requires the test to FAIL at the merge base"
assert_eq "1" "$have_blocking_pass_row" \
    "a test that PASSES at the merge base is Blocking"
assert_eq "1" "$have_advisory_unrunnable_row" \
    "a test that cannot be run in isolation has an explicit Advisory disposition (never silent acceptance)"
assert_eq "1" "$have_blocking_missing_path_row" \
    "the pre-existing 'path not in the diff' row is still Blocking"

echo
echo "--- judge.md verdict table: the TDD: no / absent rows are unchanged (#8265 AC) ---"

assert_eq "1" "$absent_row_advisory" "an absent \`TDD:\` line is still advisory-only, never blocking"
assert_eq "2" "$no_rows" "both \`no — <reason>\` rows survive"
assert_eq "2" "$no_rows_nonblocking" "neither \`no — <reason>\` row became blocking"

echo
echo "--- judge.md points at the runnable recipe ---"

assert_contains "$(cat "$JUDGE_MD")" "tdd_merge_base_run" \
    "judge.md names the function a Judge is expected to run"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
