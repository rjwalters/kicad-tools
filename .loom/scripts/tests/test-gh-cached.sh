#!/usr/bin/env bash
# test-gh-cached.sh — behavioral tests for the gh-cached short-TTL read cache
# as it is used by the sweep / judge / champion skills (issue #4667).
#
# These cover the properties those skills' documented command sequences now
# depend on (`defaults/docs/gh-cached.md`):
#
#   1. Repeated hot discovery reads (`pr list` / `issue list` / `pr view`) are
#      served from the cache within the TTL window.
#   2. `--no-cache` bypasses the cache (the explicit-bypass escape hatch).
#   3. Verdict/merge-gating shapes (`gh pr checks`) and the liveness probe
#      (`gh repo view`) are NEVER cached — the deliberate carve-outs.
#   4. A state change is observed once the TTL expires (no indefinite masking).
#   5. A mutation issued through the wrapper invalidates cached reads of that
#      resource — including the cached *listing* that contains it.
#   6. `--clear-cache` drops every entry (the documented remedy after a write
#      issued as literal `gh`, which is how the skills must issue writes so the
#      destructive-command guard hooks still see them).
#   7. Failed reads (non-zero exit) are never cached.
#
# Hermetic: a stub `gh` on PATH plus a temp GH_CACHE_DIR. No network, no forge,
# no real `gh`. Requires python3 (the wrapper's runtime); skips cleanly without.
#
# Usage:
#   ./defaults/scripts/tests/test-gh-cached.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GH_CACHED="$(cd "$SCRIPT_DIR/.." && pwd)/gh-cached"

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

if [[ ! -x "$GH_CACHED" ]]; then
    echo "FAIL: gh-cached not found or not executable at $GH_CACHED" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "SKIP: python3 not available — gh-cached cannot run on this host"
    exit 0
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

STUB_DIR="$TMP_ROOT/bin"
STATE_DIR="$TMP_ROOT/state"
mkdir -p "$STUB_DIR" "$STATE_DIR"

CALL_LOG="$TMP_ROOT/calls.log"
: > "$CALL_LOG"

# --- Stub `gh` -------------------------------------------------------------
# Logs every invocation (so we can count real forge calls) and answers from
# $STATE_DIR files, which the tests mutate to simulate forge-side changes.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_STUB_CALL_LOG"

case "$1 ${2:-}" in
  "--version "*|"--version")
    echo "gh version 2.99.0 (stub)"; exit 0 ;;
esac

case "$1" in
  repo)   cat "$GH_STUB_STATE/repo.txt"; exit 0 ;;
esac

case "$1 ${2:-}" in
  "pr list")     cat "$GH_STUB_STATE/pr-list.json"; exit 0 ;;
  "pr view")     cat "$GH_STUB_STATE/pr-view.json"; exit 0 ;;
  "pr checks")   cat "$GH_STUB_STATE/pr-checks.txt"; exit 0 ;;
  "issue list")  cat "$GH_STUB_STATE/issue-list.json"; exit 0 ;;
  "pr edit"|"pr comment"|"issue edit")
    exit 0 ;;
  "api "*|"api")
    # A failing read: used to prove non-zero responses are never cached.
    echo "stub api error" >&2; exit 1 ;;
esac

echo "stub: unhandled args: $*" >&2
exit 64
STUB
chmod +x "$STUB_DIR/gh"

export GH_STUB_CALL_LOG="$CALL_LOG"
export GH_STUB_STATE="$STATE_DIR"

echo '{"name":"loom"}'                          > "$STATE_DIR/repo.txt"
echo '[{"number":4242,"labels":["loom:review-requested"]}]' > "$STATE_DIR/pr-list.json"
echo '{"labels":["loom:review-requested"]}'     > "$STATE_DIR/pr-view.json"
echo 'build  pass  1m0s'                        > "$STATE_DIR/pr-checks.txt"
echo '[{"number":99,"title":"an issue"}]'       > "$STATE_DIR/issue-list.json"

CACHE_DIR="$TMP_ROOT/cache"

# Run gh-cached with a hermetic environment. Args are passed through verbatim.
# LOOM_ETAG_LIST_DISABLE=1 pins these to the TTL-cache layer under test — the
# #5056 ETag/REST routing (which would otherwise front-run `issue/pr list` when
# a loom-daemon is on PATH) has its own dedicated block at the end of this file.
ghc() {
    PATH="$STUB_DIR:$PATH" \
    GH_CACHE_DIR="$CACHE_DIR" \
    GH_CACHE_TTL="${TTL_OVERRIDE:-30}" \
    LOOM_ETAG_LIST_DISABLE=1 \
      "$GH_CACHED" "$@"
}

call_count() { wc -l < "$CALL_LOG" | tr -d ' '; }
reset_calls() { : > "$CALL_LOG"; }
reset_cache() { rm -rf "$CACHE_DIR"; reset_calls; }

# --- 1. Hot discovery reads are cached -------------------------------------
echo "Testing cache hits on hot discovery reads..."

reset_cache
out1=$(ghc pr list --label "loom:review-requested" --state open --limit 500)
out2=$(ghc pr list --label "loom:review-requested" --state open --limit 500)
assert_eq "1" "$(call_count)" "repeated 'pr list' issues ONE real gh call (2nd is a cache hit)"
assert_eq "$out1" "$out2" "cached 'pr list' returns the identical payload"

reset_cache
ghc issue list --label "loom:issue" --state open --limit 100 >/dev/null
ghc issue list --label "loom:issue" --state open --limit 100 >/dev/null
assert_eq "1" "$(call_count)" "repeated 'issue list' issues ONE real gh call"

reset_cache
ghc pr view 4242 --json labels >/dev/null
ghc pr view 4242 --json labels >/dev/null
assert_eq "1" "$(call_count)" "repeated 'pr view --json labels' issues ONE real gh call"

# Distinct arguments must be distinct cache keys (no cross-query bleed).
reset_cache
ghc pr list --label "loom:review-requested" --state open --limit 500 >/dev/null
ghc pr list --label "loom:pr" --state open --limit 500 >/dev/null
assert_eq "2" "$(call_count)" "a different --label is a different cache key"

# --- 2. --no-cache bypasses ------------------------------------------------
echo ""
echo "Testing the explicit --no-cache bypass..."

reset_cache
ghc pr view 4242 --json labels >/dev/null
ghc --no-cache pr view 4242 --json labels >/dev/null
assert_eq "2" "$(call_count)" "--no-cache bypasses a warm cache entry"

# --- 3. Deliberate carve-outs are never cached -----------------------------
echo ""
echo "Testing the uncached carve-outs (verdict/merge gating + liveness probe)..."

reset_cache
ghc pr checks 4242 >/dev/null 2>&1
ghc pr checks 4242 >/dev/null 2>&1
assert_eq "2" "$(call_count)" "'pr checks' (CI gate) is NEVER cached"

reset_cache
ghc repo view --json name >/dev/null 2>&1
ghc repo view --json name >/dev/null 2>&1
assert_eq "2" "$(call_count)" "'repo view' (liveness probe) is NEVER cached"

# --- 4. A state change is observed once the TTL expires --------------------
echo ""
echo "Testing TTL expiry (a state change is not masked indefinitely)..."

reset_cache
TTL_OVERRIDE=1 ghc pr list --state open --limit 500 >/dev/null
echo '[{"number":4242,"labels":["loom:pr"]}]' > "$STATE_DIR/pr-list.json"
fresh_within_ttl=$(TTL_OVERRIDE=1 ghc pr list --state open --limit 500)
assert_eq "1" "$(call_count)" "within the TTL window the changed state is still served from cache"
case "$fresh_within_ttl" in
  *loom:review-requested*) within_ok="stale" ;;
  *) within_ok="fresh" ;;
esac
assert_eq "stale" "$within_ok" "…and that cached payload is the pre-change one (as designed)"

sleep 2
after_ttl=$(TTL_OVERRIDE=1 ghc pr list --state open --limit 500)
assert_eq "2" "$(call_count)" "after the TTL expires the read hits the forge again"
case "$after_ttl" in
  *'"loom:pr"'*) after_ok="fresh" ;;
  *) after_ok="stale" ;;
esac
assert_eq "fresh" "$after_ok" "…and the new state IS observed within one TTL window"

# restore the baseline listing for the remaining tests
echo '[{"number":4242,"labels":["loom:review-requested"]}]' > "$STATE_DIR/pr-list.json"

# --- 5. Mutation-triggered invalidation ------------------------------------
echo ""
echo "Testing mutation-triggered invalidation (a skill's own write is never masked)..."

reset_cache
ghc pr view 4242 --json labels >/dev/null           # warm: 1 call
echo '{"labels":["loom:pr"]}' > "$STATE_DIR/pr-view.json"
ghc pr edit 4242 --add-label "loom:pr" >/dev/null    # mutation: 2 calls, invalidates
after_edit=$(ghc pr view 4242 --json labels)         # re-read: 3 calls
assert_eq "3" "$(call_count)" "a wrapped 'pr edit' invalidates the cached 'pr view' of that PR"
case "$after_edit" in
  *'"loom:pr"'*) mut_ok="fresh" ;;
  *) mut_ok="stale" ;;
esac
assert_eq "fresh" "$mut_ok" "…so the re-read observes the skill's own write"

# The cached LISTING containing that PR must be invalidated too (the stdout
# fallback match) — otherwise a Judge that labels loom:pr could still see the
# PR in its own loom:review-requested queue.
reset_cache
ghc pr list --label "loom:review-requested" --state open --limit 500 >/dev/null
ghc pr edit 4242 --add-label "loom:pr" >/dev/null
ghc pr list --label "loom:review-requested" --state open --limit 500 >/dev/null
assert_eq "3" "$(call_count)" "a wrapped mutation also invalidates a cached listing containing that PR"

reset_cache
ghc pr view 4242 --json labels >/dev/null
ghc pr comment 4242 --body "hello" >/dev/null
ghc pr view 4242 --json labels >/dev/null
assert_eq "3" "$(call_count)" "'pr comment' invalidates cached reads of that PR too"

# --- 6. --clear-cache (the post-literal-`gh`-write remedy) -----------------
echo ""
echo "Testing --clear-cache (the documented remedy after a literal-gh write)..."

reset_cache
ghc pr list --label "loom:review-requested" --state open --limit 500 >/dev/null
ghc --clear-cache >/dev/null 2>&1
ghc pr list --label "loom:review-requested" --state open --limit 500 >/dev/null
assert_eq "2" "$(call_count)" "--clear-cache drops the warm entry, forcing a fresh read"

# --- 7. Failed reads are never cached --------------------------------------
echo ""
echo "Testing that failing reads are not memoized..."

reset_cache
ghc api "repos/o/r/commits/deadbeef/check-runs" >/dev/null 2>&1
rc1=$?
ghc api "repos/o/r/commits/deadbeef/check-runs" >/dev/null 2>&1
assert_eq "2" "$(call_count)" "a non-zero read is retried, never served from cache"
assert_eq "1" "$rc1" "the wrapper propagates the underlying gh exit code"

# --- 8. The fallback contract ----------------------------------------------
# Every skill resolves the wrapper with `"$GH_CACHED" --version` and falls back
# to plain `gh` when it fails. Assert the probe the skills document works.
echo ""
echo "Testing the wrapper-resolution probe used by the skills..."

reset_cache
if ghc --version >/dev/null 2>&1; then probe_rc=0; else probe_rc=$?; fi
assert_eq "0" "$probe_rc" "'gh-cached --version' succeeds (the skills' resolution probe)"

# --- 9. ETag/REST routing (#5056) ------------------------------------------
# `issue list` / `pr list` prefer loom-daemon's ETag-cached REST path when the
# binary is present (free 304 on repeat, separate REST pool). Here a FAKE
# loom-daemon on PATH proves: (a) a cacheable list shape is served by it, not
# the stub gh; (b) a decline (non-zero exit) transparently falls through to the
# normal gh path; (c) LOOM_ETAG_LIST_DISABLE=1 turns the whole layer off.
echo ""
echo "Testing the #5056 ETag/REST cached-listing routing..."

FAKE_DAEMON_DIR="$TMP_ROOT/daemon"
mkdir -p "$FAKE_DAEMON_DIR"
# A fake loom-daemon: `forge <e> list --cached` with --json succeeds and emits a
# sentinel; any shape without --json exits 3 (decline), mirroring the real one.
cat > "$FAKE_DAEMON_DIR/loom-daemon" <<'FAKE'
#!/usr/bin/env bash
if [[ "$1" == "forge" && "$3" == "list" ]]; then
    if printf '%s\n' "$@" | grep -q -- '--json'; then
        echo '[{"number":777,"title":"from-etag-daemon"}]'
        exit 0
    fi
    exit 3
fi
exit 3
FAKE
chmod +x "$FAKE_DAEMON_DIR/loom-daemon"

# Runner with the fake daemon on PATH and the ETag layer ENABLED.
ghc_etag() {
    PATH="$STUB_DIR:$FAKE_DAEMON_DIR:$PATH" \
    GH_CACHE_DIR="$CACHE_DIR" \
    LOOM_DAEMON_BIN="$FAKE_DAEMON_DIR/loom-daemon" \
      "$GH_CACHED" "$@"
}

reset_cache
etag_out=$(ghc_etag issue list --label "loom:issue" --state open --json number,title)
assert_eq '[{"number":777,"title":"from-etag-daemon"}]' "$etag_out" \
    "a cacheable 'issue list --json' is served by the ETag daemon path, not stub gh"
assert_eq "0" "$(call_count)" "…and the stub gh is never called for the ETag-served list"

# A shape the daemon declines (no --json → daemon exits 3) falls through to gh.
reset_cache
ghc_etag pr list --label "loom:review-requested" --state open >/dev/null  # no --json → decline
assert_eq "1" "$(call_count)" "a daemon-declined list shape falls through to a real gh call"

# The kill switch disables the whole ETag layer even with the daemon present.
reset_cache
LOOM_ETAG_LIST_DISABLE=1 ghc_etag issue list --label "loom:issue" --state open --json number,title >/dev/null
assert_eq "1" "$(call_count)" "LOOM_ETAG_LIST_DISABLE=1 bypasses the ETag path (falls to gh)"

# --- Summary ---------------------------------------------------------------
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
