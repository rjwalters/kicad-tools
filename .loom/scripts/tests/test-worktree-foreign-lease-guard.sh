#!/usr/bin/env bash
# test-worktree-foreign-lease-guard.sh — Tests for #5783.
#
# `worktree.sh N`'s "worktree already exists, has uncommitted changes ->
# preserve" fast path used to hand the shared `.loom/worktrees/issue-N`
# directory back unconditionally, with no check for whether a second, still
# -live sweep lease might be mid-edit in it right now — exactly what
# happened on issue #5781: two Builders concurrently editing the same
# uncommitted file in that shared worktree, one's edit leaking into the
# other's pushed commit (PR #5782).
#
# This is a pure lib-function test (no worktree.sh invocation needed) —
# follows the pattern in test-worktree-race-rescue.sh / test-disk-headroom.sh:
# source `lib/worktree-foreign-lease-guard.sh` directly and drive
# `_worktree_foreign_lease_guard` against a stubbed `gh` on PATH (the same
# stub shape test-sweep-lease-publish.sh / test-sweep-lease-fence.sh use).
#
# Covers:
#   1. No lease comments at all on the issue -> returns 0 (no evidence).
#   2. Exactly one fresh lease -> returns 0 (the common, legitimate case: a
#      single sweep resuming into its own worktree).
#   3. Two distinct (host, sweep) pairs both holding a fresh lease at once
#      -> returns 1, with a `print_warning` line naming each live pair (the
#      exact #5781 signature: more than one live lease on one issue).
#   4. One fresh + one STALE (past-TTL) lease -> returns 0 (only one is
#      actually live).
#   5. Two fresh leases, but one has already posted a matching
#      `loom:lease-yield` standdown (#6287's tie-break) -> returns 0 (the
#      yielded one is not live, so there is only one live pair left).
#   6. `gh` missing from PATH -> returns 0 (fail open, never blocks worktree
#      creation on a missing dependency).
#   7. A `gh api` read failure -> returns 0 (fail open; absence of evidence
#      is not evidence of a peer, matching every other lease-subsystem
#      probe).
#
# Usage:
#   ./.loom/scripts/tests/test-worktree-foreign-lease-guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LEASE_GUARD_LIB="$SCRIPTS_DIR/lib/worktree-foreign-lease-guard.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 (expected substring '$2' in: '$1')"; fi
}

if [[ ! -f "$LEASE_GUARD_LIB" ]]; then
    echo -e "${RED}FATAL${NC}: $LEASE_GUARD_LIB not found" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH -- reads $STUB_DIR/comments.json (or "[]"), or fails
# when $STUB_DIR/comments-fail exists. Mirrors test-sweep-lease-publish.sh's
# stub, GET-only: this guard never writes.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "api" ]]; then
  shift
  path=""
  jq_filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -R) shift 2 ;;
      --paginate) shift ;;
      --jq) jq_filter="$2"; shift 2 ;;
      *)
        if [[ -z "$path" ]]; then path="$1"; fi
        shift
        ;;
    esac
  done
  if [[ "$path" == repos/*/issues/*/comments ]]; then
    if [[ -f "$D/comments-fail" ]]; then
      echo "stub gh: comments fetch failed" >&2
      exit 1
    fi
    canned="$D/comments.json"
    [[ -f "$canned" ]] || echo "[]" > "$canned"
    if [[ -n "$jq_filter" ]]; then
      jq -c "$jq_filter" "$canned"
    else
      cat "$canned"
    fi
    exit 0
  fi
  echo "stub gh: unhandled api path: $path" >&2
  exit 3
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
REAL_PATH="$PATH"
export PATH="$STUB_DIR:$PATH"

NOW_EPOCH="$(date -u +%s)"
NOW_ISO="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
FRESH_ISO="$(date -u -d "@$((NOW_EPOCH - 120))" +"%Y-%m-%dT%H:%M:%SZ" 2> /dev/null \
    || date -u -j -f %s "$((NOW_EPOCH - 120))" +"%Y-%m-%dT%H:%M:%SZ")"
STALE_ISO="$(date -u -d "@$((NOW_EPOCH - 3600))" +"%Y-%m-%dT%H:%M:%SZ" 2> /dev/null \
    || date -u -j -f %s "$((NOW_EPOCH - 3600))" +"%Y-%m-%dT%H:%M:%SZ")"
export LOOM_LEASE_PUBLISH_NOW="$NOW_EPOCH"

reset_state() {
    rm -f "$STUB_DIR"/comments.json "$STUB_DIR"/comments-fail
}

# lease_pair <host> <sweep> <updated_at> -- a single-element ARRAY (not a
# bare object): `jq -s add` on a list of ARRAYS concatenates them into one
# flat list, exactly what these fixtures need to compose via `jq -s add
# <(lease_pair ...) <(lease_pair ...)`. A bare object would instead make
# `add` perform an object-MERGE (last key wins), silently collapsing two
# fixtures into one.
lease_pair() {
    jq -n --arg host "$1" --arg sweep "$2" --arg ts "$3" \
        '[{updated_at: $ts, body: ("<!-- loom:lease host=" + $host + " sweep=" + $sweep + " -->\nprose")}]'
}

# yield_pair <host> <sweep> <updated_at> <earliest_host> <earliest_sweep> --
# also a single-element array; see lease_pair's comment above.
yield_pair() {
    jq -n --arg host "$1" --arg sweep "$2" --arg ts "$3" --arg eh "$4" --arg es "$5" \
        '[{updated_at: $ts, body: ("<!-- loom:lease-yield host=" + $host + " sweep=" + $sweep + " earliest_host=" + $eh + " earliest_sweep=" + $es + " -->\nprose")}]'
}

# shellcheck source=../lib/worktree-foreign-lease-guard.sh
source "$LEASE_GUARD_LIB"

echo "Testing worktree-foreign-lease-guard.sh (now=$NOW_ISO)..."

# --- 1. no lease comments at all -------------------------------------------
reset_state
WARN_OUT="$(_worktree_foreign_lease_guard 5781 2>&1)"
RC=$?
assert_eq "0" "$RC" "1. no lease comments on the issue -> returns 0"
assert_eq "" "$WARN_OUT" "1. no warning printed"

# --- 2. exactly one fresh lease --------------------------------------------
reset_state
lease_pair "host-a" "sweep-1" "$FRESH_ISO" > "$STUB_DIR/comments.json"
_worktree_foreign_lease_guard 5781 > /dev/null 2>&1
assert_eq "0" "$?" "2. exactly one fresh lease -> returns 0 (single legitimate sweep)"

# --- 3. two distinct fresh leases -> returns 1 with a warning per pair ----
reset_state
jq -s 'add' <(lease_pair "host-d9142cf3" "sweep-a" "$FRESH_ISO") \
    <(lease_pair "host-d9142cf3" "sweep-b" "$NOW_ISO") > "$STUB_DIR/comments.json"
WARN_OUT="$(_worktree_foreign_lease_guard 5781 2>&1)"
RC=$?
assert_eq "1" "$RC" "3. two simultaneously fresh leases -> returns 1"
assert_contains "$WARN_OUT" "simultaneously FRESH" "3. warning names the multi-lease condition"
assert_contains "$WARN_OUT" "sweep=sweep-a" "3. warning names the first live lease"
assert_contains "$WARN_OUT" "sweep=sweep-b" "3. warning names the second live lease"

# --- 4. one fresh + one stale -> returns 0 (only one is actually live) ----
reset_state
jq -s 'add' <(lease_pair "host-a" "sweep-old" "$STALE_ISO") \
    <(lease_pair "host-a" "sweep-new" "$FRESH_ISO") > "$STUB_DIR/comments.json"
_worktree_foreign_lease_guard 5781 > /dev/null 2>&1
assert_eq "0" "$?" "4. one fresh + one stale lease -> returns 0"

# --- 5. two fresh leases, one already yielded -> returns 0 -----------------
reset_state
jq -s 'add' <(lease_pair "host-a" "sweep-old" "$FRESH_ISO") \
    <(lease_pair "host-a" "sweep-new" "$FRESH_ISO") \
    <(yield_pair "host-a" "sweep-old" "$FRESH_ISO" "host-a" "sweep-new") \
    > "$STUB_DIR/comments.json"
_worktree_foreign_lease_guard 5781 > /dev/null 2>&1
assert_eq "0" "$?" "5. one of two fresh leases already yielded -> only one live pair remains, returns 0"

# --- 6. gh missing from PATH -> fail open -----------------------------------
# Filter every PATH component that actually resolves a `gh` executable (not
# just prepend an empty dir ahead of the real one -- the real `gh` CLI is
# routinely installed on this host for other Loom scripts, so it would still
# be found further down an unfiltered PATH).
reset_state
jq -s 'add' <(lease_pair "host-a" "sweep-1" "$FRESH_ISO") \
    <(lease_pair "host-b" "sweep-2" "$FRESH_ISO") > "$STUB_DIR/comments.json"
NO_GH_PATH=""
while IFS= read -r dir; do
    [[ -n "$dir" ]] || continue
    [[ -x "$dir/gh" ]] && continue
    NO_GH_PATH="${NO_GH_PATH:+$NO_GH_PATH:}$dir"
done < <(printf '%s' "$REAL_PATH" | tr ':' '\n')
(
    export PATH="$NO_GH_PATH"
    _worktree_foreign_lease_guard 5781 > /dev/null 2>&1
)
assert_eq "0" "$?" "6. gh missing from PATH -> fail open, returns 0"

# --- 7. a gh api read failure -> fail open ----------------------------------
reset_state
touch "$STUB_DIR/comments-fail"
_worktree_foreign_lease_guard 5781 > /dev/null 2>&1
assert_eq "0" "$?" "7. a comments-read failure fails open, returns 0"
rm -f "$STUB_DIR/comments-fail"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}ALL PASSED${NC}"
    exit 0
else
    echo -e "${RED}$TESTS_FAILED FAILED${NC}"
    exit 1
fi
