#!/usr/bin/env bash
# test-merge-pr-closed-issue-cleanup.sh - Unit tests for the closed-issue
# `loom:building` cleanup logic in merge-pr.sh (#6199).
#
# A merge that auto-closes an issue via `Closes #N` / `Fixes #N` /
# `Resolves #N` leaves the issue's `loom:building` claim label in place —
# #2838 deliberately decided this was harmless for most labels, but #6199
# found it is NOT harmless specifically for `loom:building`: any consumer
# that reads the label as "in flight" without also filtering on issue state
# (a dashboard, a capacity check, a manual `gh issue list --label
# loom:building` spot-check) sees pure noise once the population of
# closed-but-still-labelled issues grows. merge-pr.sh's
# _strip_closed_issue_building_labels removes the label from every issue THIS
# merge closed (resolved via forge_pr_close_targets ->
# closingIssuesReferences), at the same post-merge choke point as the #3667
# partial-increment reset.
#
# Strategy: mirrors test-merge-pr-partial-increment.sh — extract just the
# functions under test from merge-pr.sh (they depend only on globals
# REPO_NWO, PR_NUMBER, FORGE_TYPE, GH and the `gh` CLI) and source them, stub
# `gh` on PATH to serve canned issue JSON and record mutating calls, then
# assert on the recorded calls. Extracting from source (rather than
# replicating) keeps the test in lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-closed-issue-cleanup.sh

# SC2034: several globals (REPO_NWO, PR_NUMBER, FORGE_TYPE, GH) are read only
# by the functions extracted+sourced from merge-pr.sh, which shellcheck
# cannot see.
# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

# #8191: the strip-or-skip decision now delegates to `loom-daemon merge-pr
# closed-building`. Pin the binary the extracted function execs and verify it
# HAS that subcommand — the same harness the epic's other ported suites use.
# Without the subcommand check every decision below would come back "did not
# run", and a pass that never strips anything looks exactly like a pass whose
# skips are all correct.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr closed-building"

# Colors
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

# --- Minimal logging shims the extracted functions call ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }

# --- Real forge-helpers.sh (for forge_gh_remove_label_rl_safe, #6199 /
# the #4856 rate-limit-safe mutation wrapper family it belongs to) ---
# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

# --- Extract the functions under test from merge-pr.sh and source them ---
FUNCS_FILE="$(mktemp)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT
awk '
  /^_strip_one_closed_issue_building_label\(\) \{/ { capture=1 }
  /^_strip_closed_issue_building_labels\(\) \{/    { capture=1 }
  capture { print }
  capture && /^}/ { capture=0 }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

for _fn in _strip_one_closed_issue_building_label _strip_closed_issue_building_labels; do
    if ! grep -q "^${_fn}() {" "$FUNCS_FILE"; then
        echo -e "${RED}FATAL${NC}: could not extract $_fn from $MERGE_PR_SRC" >&2
        exit 2
    fi
done
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub gh on PATH ---
STUB_DIR="$(mktemp -d)"
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh for test-merge-pr-closed-issue-cleanup.sh.
#   gh api repos/OWNER/REPO/issues/N -X DELETE -> record + succeed (unless
#     LOOM_TEST_DELETE_FAIL=1)
#   gh api repos/OWNER/REPO/issues/N        -> cat $STUB_DIR/issue-N.json (or {})
#   gh issue edit N ...                     -> record to $STUB_DIR/gh-calls.log
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "api" ]]; then
  shift
  path="" is_delete=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -X)
        shift
        [[ "${1:-}" == "DELETE" ]] && is_delete=1
        shift
        continue
        ;;
      --jq|-q|--field|-f|--raw-field|-F|--header|-H|--input)
        shift
        [[ $# -gt 0 ]] && shift
        continue
        ;;
      -*) shift; continue ;;
      *) path="$1"; break ;;
    esac
  done

  if [[ $is_delete -eq 1 ]]; then
    echo "api DELETE $path" >> "$LOG"
    if [[ "${LOOM_TEST_DELETE_FAIL:-}" == "1" ]]; then
      echo '{"message":"simulated failure"}' >&2
      exit 1
    fi
    exit 0
  fi

  num="${path##*/}"
  canned="$STUB_DIR_FROM_ENV/issue-$num.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '{}'; fi
  exit 0
fi

if [[ "$1" == "issue" ]]; then
  echo "$*" >> "$LOG"
  if [[ "${LOOM_TEST_ISSUE_EDIT_FAIL:-}" == "1" ]]; then
    echo "stub gh: simulated gh issue edit failure" >&2
    exit 1
  fi
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# --- Shared globals the functions read ---
REPO_NWO="owner/repo"
PR_NUMBER="999"
FORGE_TYPE="github"
GH="gh"

# Shim for the GraphQL-backed close-target helper merge-pr.sh gets from
# forge-helpers.sh, mirroring test-merge-pr-partial-increment.sh's approach.
FORGE_CLOSE_TARGETS=""
forge_pr_close_targets() { printf '%s' "$FORGE_CLOSE_TARGETS"; }

# Canned issue fixtures.
cat > "$STUB_DIR/issue-100.json" <<'EOF'
{"state":"closed","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-101.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-102.json" <<'EOF'
{"state":"closed","labels":[{"name":"loom:issue"}]}
EOF
cat > "$STUB_DIR/issue-103.json" <<'EOF'
{"state":"closed","pull_request":{"url":"x"},"labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-104.json" <<'EOF'
{"state":"closed","labels":[{"name":"loom:building"},{"name":"tier:maintenance"}]}
EOF

reset_log() {
  : > "$STUB_DIR/gh-calls.log"
  FORGE_CLOSE_TARGETS=""
  unset LOOM_TEST_DELETE_FAIL
  unset LOOM_TEST_ISSUE_EDIT_FAIL
}
read_log()  { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }

echo "Testing _strip_one_closed_issue_building_label behavior..."

# T1: closed + loom:building -> label removed via the rate-limit-safe wrapper.
reset_log
_strip_one_closed_issue_building_label "100"
log="$(read_log)"
assert_contains "$log" "issue edit 100 --repo owner/repo --remove-label loom:building" \
  "Closed issue #100 (loom:building) -> label removed via gh issue edit"

# T2: open issue -> no-op (never strip a live claim).
reset_log
_strip_one_closed_issue_building_label "101"
assert_eq "" "$(read_log)" "Open issue #101 -> no-op (label left in place)"

# T3: closed but no loom:building label -> no-op (idempotent).
reset_log
_strip_one_closed_issue_building_label "102"
assert_eq "" "$(read_log)" "Closed issue #102 (no loom:building) -> no-op, idempotent"

# T4: target is actually a PR (has .pull_request) -> skip.
reset_log
_strip_one_closed_issue_building_label "103"
assert_eq "" "$(read_log)" "Issue #103 (is a PR, has .pull_request) -> skipped"

# T5: closed issue carrying loom:building plus other labels -> only
# loom:building is targeted (the --remove-label argument names it explicitly,
# no other label is touched).
reset_log
_strip_one_closed_issue_building_label "104"
log="$(read_log)"
assert_contains "$log" "issue edit 104 --repo owner/repo --remove-label loom:building" \
  "Closed issue #104 (loom:building + tier:maintenance) -> only loom:building removed"
assert_not_contains "$log" "--remove-label tier:maintenance" \
  "Closed issue #104 -> tier:maintenance is untouched"

echo ""
echo "Testing _strip_closed_issue_building_labels behavior..."

# T6: PR closes #100 (closed, building) -> label stripped.
reset_log
FORGE_CLOSE_TARGETS="100"
_strip_closed_issue_building_labels
assert_contains "$(read_log)" "issue edit 100 --repo owner/repo --remove-label loom:building" \
  "forge_pr_close_targets=100 -> #100's loom:building is stripped"

# T7: multiple close targets, mixed states -> only the closed+building one acted on.
reset_log
FORGE_CLOSE_TARGETS="$(printf '100\n101\n102')"
_strip_closed_issue_building_labels
log="$(read_log)"
assert_contains "$log" "issue edit 100 --repo owner/repo --remove-label loom:building" \
  "Multi-target: #100 (closed, building) is stripped"
assert_not_contains "$log" "issue edit 101" \
  "Multi-target: #101 (open) is left alone"
assert_not_contains "$log" "issue edit 102" \
  "Multi-target: #102 (closed, not building) is left alone"

# T8: no close targets at all -> no-op, no gh calls.
reset_log
FORGE_CLOSE_TARGETS=""
_strip_closed_issue_building_labels
assert_eq "" "$(read_log)" "No close targets -> no-op"

# T9: FORGE_TYPE != github -> no-op (v1 is GitHub-only, mirrors #3667's gating).
reset_log
FORGE_TYPE="gitea"
FORGE_CLOSE_TARGETS="100"
_strip_closed_issue_building_labels
assert_eq "" "$(read_log)" "FORGE_TYPE=gitea -> no-op (GitHub-only v1)"
FORGE_TYPE="github"

# T10: GraphQL mutation fails, but is NOT a rate-limit error (e.g. permissions)
# -> the REST fallback inside forge_gh_remove_label_rl_safe is not attempted;
# best-effort caller (_strip_one_closed_issue_building_label) swallows the
# failure and never propagates a non-zero exit (merge-pr.sh calls this whole
# pass with `|| true`, but the function itself must also not abort the caller
# under `set -e` since it runs inline, not in isolation).
reset_log
LOOM_TEST_ISSUE_EDIT_FAIL=1
set +e
_strip_one_closed_issue_building_label "100"
rc=$?
set -e
assert_eq "0" "$rc" "A failed removal attempt does not propagate a nonzero exit (best-effort)"

# T11 (#8191): the decision moved to `loom-daemon merge-pr closed-building`,
# so a daemon that predates the verb must produce a WARNING naming the manual
# removal and must NOT mutate anything. A guessed strip here would drop a live
# builder's claim label; a silent skip would hide that the pass stopped
# running at all.
reset_log
fake_no_verb="$STUB_DIR/fake-loom-daemon-no-closed-building"
cat > "$fake_no_verb" <<'FAKEDAEMON'
#!/usr/bin/env bash
echo "error: unrecognized subcommand 'closed-building'" >&2
exit 2
FAKEDAEMON
chmod +x "$fake_no_verb"
saved_bin="${LOOM_DAEMON_BIN:-}"
export LOOM_DAEMON_BIN="$fake_no_verb"
stderr_out="$(_strip_one_closed_issue_building_label "100" 2>&1 >/dev/null)"
export LOOM_DAEMON_BIN="$saved_bin"
assert_eq "" "$(read_log)" "Daemon without the verb -> nothing is mutated"
assert_contains "$stderr_out" "--remove-label loom:building" \
  "Daemon without the verb -> warning names the manual removal"

# T12 (#8191): exit 0 is not enough — the wrapper accepts only the two known
# lines. A daemon answering with anything else (a truncated write, a future
# protocol it does not share) must be treated as "did not run", never replayed
# as a silent SKIP. This is the whole reason the skip is a positive `SKIP`
# line rather than empty output.
reset_log
fake_garbage="$STUB_DIR/fake-loom-daemon-garbage"
cat > "$fake_garbage" <<'FAKEDAEMON'
#!/usr/bin/env bash
echo "MAYBE"
exit 0
FAKEDAEMON
chmod +x "$fake_garbage"
saved_bin="${LOOM_DAEMON_BIN:-}"
export LOOM_DAEMON_BIN="$fake_garbage"
stderr_out="$(_strip_one_closed_issue_building_label "100" 2>&1 >/dev/null)"
export LOOM_DAEMON_BIN="$saved_bin"
assert_eq "" "$(read_log)" "Daemon answering off-protocol at exit 0 -> nothing is mutated"
assert_contains "$stderr_out" "rather than STRIP or SKIP" \
  "Daemon answering off-protocol -> warning says what it printed instead"

# --- Invariant guard: merge-pr.sh actually wires this pass in at the
# confirmed-merge choke point (#6199 AC #1 and #4) ---
echo ""
echo "Testing merge-pr.sh wiring..."

src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_strip_closed_issue_building_labels || true" \
  "merge-pr.sh invokes the closed-issue cleanup pass at the confirmed-merge choke point"
assert_contains "$src" 'forge_gh_remove_label_rl_safe "$REPO_NWO" "$issue_num" "loom:building"' \
  "merge-pr.sh strips loom:building via the rate-limit-safe remove wrapper (#4856-style)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
