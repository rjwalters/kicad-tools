#!/usr/bin/env bash
# test-merge-pr-label-freshness.sh - Regression guard for #8550: merge-pr.sh's
# initial PR fetch (the sole source of $PR_LABELS) must be UNCACHED.
#
# THE INCIDENT (2026-09-21, PR #8462). An operator released a merge-risk hold
# by removing `loom:operator`, then immediately ran `merge-pr.sh 8462`. Three
# consecutive runs refused with the #8112 verdict-contradiction error, while
# `gh pr view` showed clean labels (`loom:pr` only) and a direct invocation of
# the guard returned LOOM-VERDICT-CLEAN within seconds of a failing attempt.
# The fourth attempt, minutes later, merged with no other change.
#
# ROOT CAUSE. `$GH` is the `gh-cached` wrapper whenever it is present, and the
# initial `PR_JSON=$(forge_get_pr …)` fetch went through it. Any merge started
# inside the wrapper's short TTL therefore read the PR's PRE-change label set —
# here the `loom:pr` + `loom:operator` pair — and the guard correctly refused
# what it had (incorrectly) been told was present. A hold release is precisely
# the sequence that lands inside that window: remove the label, merge at once.
#
# THE FIX (#8550). That fetch now uses `forge_get_pr_nocache`, joining the 15+
# rechecks in the same script that already read uncached. Label state is
# verdict-gating / merge-gating data — the deliberately-uncached carve-out
# class in docs/gh-cached.md — not a repeated observation. The guard's own
# stdin path is unchanged: it was always correct given correct input.
#
# WHAT THIS SUITE DOES. It drives the REAL code, extracted verbatim from
# merge-pr.sh (no re-implementation, so it cannot drift):
#
#   * the fetch + label-derivation block, against a stub `gh-cached` that
#     models the TTL cache — a plain `api` read serves the stale pre-change
#     snapshot, a `--no-cache` read serves current forge truth; and
#   * `_check_verdict_label_contradiction`, against the REAL `loom-daemon
#     merge-pr verdict-contradiction` (same harness as
#     test-merge-pr-verdict-label-guard.sh).
#
# so the assertion chain is end to end: mutate labels -> fetch inside the TTL
# window -> derive $PR_LABELS -> real merge decision. T4 is the discriminating
# control: the same fixture read through the CACHED path reproduces the
# incident (a blocked merge), which is what proves T1-T3 would fail against
# the pre-#8550 script rather than passing vacuously.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-label-freshness.sh

# SC2034: PR_NUMBER/PR_LABELS/PR_HEAD_SHA/DRY_RUN/REPO_NWO/GH are read by the
# extracted+sourced merge-pr.sh code, which shellcheck cannot see.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

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
    # Here-string, not a pipe (#3820 — a pipe runs grep in a subshell whose
    # early exit trips pipefail in the caller).
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

[[ -f "$MERGE_PR_SRC" ]] || { echo "ERROR: $MERGE_PR_SRC not found" >&2; exit 1; }

# --- Minimal logging/error shims the extracted merge-pr.sh code calls ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# The merge decision under test is `loom-daemon merge-pr verdict-contradiction`.
# Pin the binary and verify it knows the subcommand — FATAL, never a skip, for
# the reason spelled out in lib/require-daemon-bin.sh.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR" 2>/dev/null || true' EXIT
export STUB_STATE="$WORKDIR/state"
mkdir -p "$STUB_STATE"

# --- Fixtures: the PR as the forge sees it now, and as the cache saw it ---
#
# live.json  — current forge truth, AFTER the operator removed the hold.
# cached.json — what a read landing inside the wrapper's TTL window returns:
#               the pre-change snapshot, still carrying `loom:operator`.
cat > "$STUB_STATE/live.json" <<'JSON'
{
  "state": "open",
  "merged": false,
  "mergeable": true,
  "title": "A held PR whose hold was just released",
  "head": { "ref": "feature/issue-8462", "sha": "c0ffee1" },
  "labels": [ { "name": "loom:pr" } ]
}
JSON

cat > "$STUB_STATE/cached.json" <<'JSON'
{
  "state": "open",
  "merged": false,
  "mergeable": true,
  "title": "A held PR whose hold was just released",
  "head": { "ref": "feature/issue-8462", "sha": "c0ffee1" },
  "labels": [ { "name": "loom:pr" }, { "name": "loom:operator" } ]
}
JSON

# --- Stub `gh-cached`: models the wrapper's short-TTL read cache ---
# `api …`            -> the stale pre-change snapshot (a hit inside the window)
# `--no-cache api …` -> current forge truth (the documented bypass)
# Every call records its mode, so a test can assert which path was taken.
mkdir -p "$WORKDIR/bin"
cat > "$WORKDIR/bin/gh-cached" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
mode="cached"
if [[ "${1:-}" == "--no-cache" ]]; then mode="nocache"; shift; fi
printf '%s\n' "$mode" >> "$STUB_STATE/calls.log"
[[ "${1:-}" == "api" ]] || { echo "stub gh-cached: unexpected args: $*" >&2; exit 64; }
if [[ "$mode" == "nocache" ]]; then cat "$STUB_STATE/live.json"; else cat "$STUB_STATE/cached.json"; fi
STUB
chmod +x "$WORKDIR/bin/gh-cached"

# --- Stub plain `gh`: no wrapper installed on this host ---
# Real `gh` rejects `--no-cache` (it is a wrapper flag, #3547) and `2>/dev/null`
# would swallow the error, so this stub fails loudly if the flag ever reaches
# it. Plain `gh api` is already uncached, hence it serves live.json.
cat > "$WORKDIR/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
for a in "$@"; do
  if [[ "$a" == "--no-cache" ]]; then echo "unknown flag: --no-cache" >&2; exit 1; fi
done
[[ "${1:-}" == "api" ]] || { echo "stub gh: unexpected args: $*" >&2; exit 64; }
cat "$STUB_STATE/live.json"
STUB
chmod +x "$WORKDIR/bin/gh"

# --- Extract the REAL fetch + label-derivation block from merge-pr.sh ---
# From the `# Fetch PR state` comment through the `PR_LABELS=` assignment that
# derives the guard's only input. Extracting from source keeps this suite in
# lockstep with the script instead of re-implementing its derivation.
FETCH_FILE="$WORKDIR/fetch-block.sh"
awk '
  /^# Fetch PR state/ { f = 1 }
  f                   { print }
  f && /^PR_LABELS=/  { exit }
' "$MERGE_PR_SRC" > "$FETCH_FILE"

if ! grep -q '^PR_LABELS=' "$FETCH_FILE" || ! grep -q '^PR_JSON=' "$FETCH_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract the PR fetch + label-derivation block from $MERGE_PR_SRC" >&2
    exit 2
fi

# --- Extract the REAL guard from merge-pr.sh (same recipe as #8112's suite) ---
# `_mp_daemon_roll_hint` and the `# requires-daemon:` markers travel with it:
# the hint reads the version floor back out of ${BASH_SOURCE[0]}, i.e. out of
# this extracted file (#8285).
GUARD_FILE="$WORKDIR/guard.sh"
awk '
  /^# requires-daemon:/          { print; next }
  /^_mp_daemon_roll_hint\(\) \{/ { print; next }
  /^_check_verdict_label_contradiction\(\) \{/ { print; exit }
' "$MERGE_PR_SRC" > "$GUARD_FILE"

if ! grep -q '_check_verdict_label_contradiction()' "$GUARD_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_verdict_label_contradiction from $MERGE_PR_SRC" >&2
    exit 2
fi

# shellcheck source=lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"
# shellcheck disable=SC1090
source "$GUARD_FILE"

FORGE_TYPE="github"
REPO_NWO="rjwalters/loom"
PR_NUMBER="8462"
DRY_RUN=false
PR_JSON=""
PR_LABELS=""
PR_HEAD_SHA=""

# Run the extracted fetch block exactly as merge-pr.sh runs it, with $GH set to
# the stub under test. Sets PR_JSON / PR_LABELS / PR_HEAD_SHA / … in this shell.
run_fetch() {
    GH="$1"
    # shellcheck disable=SC1090
    source "$FETCH_FILE"
}

LAST_OUT=""
LAST_RC=0
run_guard() {
    set +e
    LAST_OUT="$( _check_verdict_label_contradiction 2>&1 )"
    LAST_RC=$?
    set -e
}

echo "Testing merge-pr.sh's PR label fetch freshness (#8550)..."

# T1: the label set merge-pr.sh derives reflects CURRENT forge state, not the
# pre-change snapshot a read inside the TTL window would return. This is the
# acceptance criterion; it fails against the pre-#8550 cached fetch.
: > "$STUB_STATE/calls.log"
run_fetch "$WORKDIR/bin/gh-cached"
assert_eq "loom:pr" "$PR_LABELS" \
  "labels reflect the MUTATED forge state (loom:operator removed), not the cached pre-change pair"
assert_not_contains "$PR_LABELS" "loom:operator" \
  "the released hold label is absent — the stale pair never reaches the guard"

# T2: the fetch actually took the cache-bypass path (the mechanism, not just
# the outcome — a stub that happened to serve the same JSON on both paths
# would make T1 pass for the wrong reason).
assert_eq "nocache" "$(tr -d '[:space:]' < "$STUB_STATE/calls.log")" \
  "the fetch went through the wrapper's --no-cache bypass exactly once"

# T3: the rest of the derivation still works off the uncached response — the
# swap changed the freshness of the read, nothing about its shape.
assert_eq "c0ffee1" "$PR_HEAD_SHA" "head SHA is derived from the uncached response"
assert_eq "open" "$PR_STATE" "PR state is derived from the uncached response"
assert_eq "false" "$PR_MERGED" "merged flag is derived from the uncached response"
assert_eq "feature/issue-8462" "$PR_BRANCH" "head ref is derived from the uncached response"

# T4: the merge DECISION, end to end. The real `loom-daemon merge-pr
# verdict-contradiction` sees the fresh label set and clears the merge — the
# outcome the operator's first three attempts should have had.
run_guard
assert_eq "0" "$LAST_RC" "the verdict guard CLEARS the merge on the post-hold-release labels"
assert_not_contains "$LAST_OUT" "Merge blocked" "no block message on a clean, current label set"

# T5: the discriminating control — the pre-#8550 CACHED read, same fixture,
# same guard. It reproduces the incident: the stale `loom:pr` + `loom:operator`
# pair blocks a merge that should proceed. Without this, T1/T4 could pass
# against a fixture that simply had nothing stale to find.
_STALE_JSON="$(forge_get_pr "$REPO_NWO" "$PR_NUMBER" "$WORKDIR/bin/gh-cached")"
PR_LABELS="$(echo "$_STALE_JSON" | jq -r '.labels[]?.name // empty')"
assert_contains "$PR_LABELS" "loom:operator" \
  "control: the CACHED read still returns the pre-change pair (the fixture is genuinely stale)"
run_guard
assert_eq "1" "$LAST_RC" \
  "control: fed the cached label set, the guard blocks — this IS the #8550 incident"
assert_contains "$LAST_OUT" "loom:operator" \
  "control: the reproduced block names the label the operator had already removed"

# T6: no `gh-cached` wrapper on this host — the plain-`gh` fallback. The fetch
# must still succeed and must NOT pass `--no-cache` (a wrapper-only flag real
# gh rejects, #3547; the error would be swallowed by 2>/dev/null and the
# caller would see an empty fetch).
PR_LABELS=""
run_fetch "$WORKDIR/bin/gh"
assert_eq "loom:pr" "$PR_LABELS" \
  "plain gh (no wrapper): the fetch succeeds and yields the current label set"
assert_eq "c0ffee1" "$PR_HEAD_SHA" "plain gh (no wrapper): the response is parsed, not an empty '{}' fallback"

# --- Source guards (fail if a refactor reintroduces the cached fetch) ---
echo ""
echo "Testing merge-pr.sh source guards..."
assert_contains "$(cat "$FETCH_FILE")" 'forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH"' \
  "merge-pr.sh's initial PR fetch uses the UNCACHED forge_get_pr_nocache"
assert_not_contains "$(cat "$FETCH_FILE")" 'PR_JSON=$(forge_get_pr "' \
  "the cached forge_get_pr fetch is gone from that call site (#8550)"
assert_contains "$(cat "$FETCH_FILE")" 'PR_LABELS=$(echo "$PR_JSON"' \
  "PR_LABELS is still derived from that same fetch (no second, cached source crept in)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
