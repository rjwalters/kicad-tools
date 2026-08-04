#!/usr/bin/env bash
# test-resolve-tier-model.sh - Tests for resolve-tier-model.sh's complexity-tier
# parsing (#4448).
#
# resolve-tier-model.sh's `grep -oE '<!-- ... -->' | case` layer had no
# shell-level test coverage — only the Python `--tier` lookup behind it
# (loom-tools/tests/test_model_tiers.py) was exercised. This harness targets
# that layer: valid marker, absent marker, out-of-vocabulary marker (the drift
# bug #4448 was filed for — curators emitting `trivial`/`large`/`moderate`
# instead of the closed `mechanical|routine|complex` enum), last-marker-wins
# when a body carries more than one canonical marker, and prose that merely
# discusses the marker syntax before the real marker appears (#4840).
#
# Style matches test-blame-issue.sh: a fake `gh` stub on PATH answers the
# `gh issue view <issue> -R <repo> --json body -q .body` invocation the script
# makes, keyed off issue number, so no network access is required. The repo
# argument is passed explicitly to the script (its 3rd positional arg), so the
# forge-remote auto-detection path is never exercised here.
#
# Usage:
#   ./defaults/scripts/tests/test-resolve-tier-model.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESOLVE_SCRIPT="$SCRIPTS_DIR/resolve-tier-model.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (expected substring '$needle' in: $haystack)"
    fi
}

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (unexpected substring '$needle' in: $haystack)"
    fi
}

if [[ ! -x "$RESOLVE_SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $RESOLVE_SCRIPT not found or not executable" >&2
    exit 1
fi

# Scratch area for the fixture repo + fake gh -- cleaned on exit.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resolve-tier-model.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# ---- Fake `gh` stub ---------------------------------------------------------
# Answers exactly the `gh issue view <issue> -R <repo> --json body -q .body`
# invocation resolve-tier-model.sh makes, keyed off a synthetic issue number.
FAKE_BIN="$WORKDIR/bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/gh" <<'FAKEGH'
#!/usr/bin/env bash
# GraphQL path: gh issue view <issue> -R <repo> --json body -q .body
if [[ "$1" == "issue" && "$2" == "view" ]]; then
    issue="$3"
    case "$issue" in
        9001) echo "Some issue body.

<!-- loom:complexity=mechanical -->

More text." ;;
        9002) echo "An issue body with no complexity marker at all." ;;
        9003) echo "Drifted marker from a paraphrasing curator.

<!-- loom:complexity=trivial -->
" ;;
        9004) echo "Body with two markers -- last one wins.

<!-- loom:complexity=complex -->
<!-- loom:complexity=mechanical -->
" ;;
        9005) exit 1 ;;   # GraphQL quota exhausted -> forces REST fallback (#4472)
        9006) exit 1 ;;   # GraphQL fails ...
        9007) echo "Meta issue about the complexity-marker feature itself, which
legitimately quotes the marker syntax as literal example text before the real
marker appears (#4840): \`<!-- loom:complexity=<tier> -->\`.

<!-- loom:complexity=mechanical -->
" ;;
        *) echo "" ;;
    esac
    exit 0
fi
# REST fallback path: gh api repos/<repo>/issues/<issue> --jq .body (#4472)
if [[ "$1" == "api" ]]; then
    issue="${2##*/}"
    case "$issue" in
        9005) echo "Recovered via REST.

<!-- loom:complexity=complex -->
" ; exit 0 ;;
        9006) exit 1 ;;   # ... and REST fails too -> falls through to routine
        *) exit 1 ;;
    esac
fi
exit 1
FAKEGH
chmod +x "$FAKE_BIN/gh"

# ---- Fixture repo -------------------------------------------------------
# resolve-tier-model.sh requires a git repo (for `git rev-parse --show-toplevel`
# to locate .loom/config.json) and passes through to Python's model_tiers
# --tier lookup, which needs loom-tools on the real source tree -- that lookup
# is resolved relative to resolve-tier-model.sh's own location, not this
# fixture's cwd, so an unconfigured fixture repo (no .loom/config.json) is
# sufficient: it always falls through to "no tierModels/optimization-preset
# entry" (exit 3), which every test here treats as expected, not a failure --
# these tests target the tier-parsing layer, not model resolution.
REPO="$WORKDIR/repo"
mkdir -p "$REPO"
git init --quiet "$REPO"

run_resolve() {
    local issue="$1"
    ( cd "$REPO" && PATH="$FAKE_BIN:$PATH" "$RESOLVE_SCRIPT" "$issue" claude "owner/repo" )
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$RESOLVE_SCRIPT" ]]; then
    pass "resolve-tier-model.sh is executable"
else
    fail "resolve-tier-model.sh is missing or not executable"
fi

# -------- Test 2: valid marker resolves to itself, no warning --------
echo "Test 2: valid marker (mechanical) resolves as-is"
err="$(run_resolve 9001 2>&1 1>/dev/null)"
assert_contains "tier=mechanical" "$err" "valid marker resolves tier=mechanical"
assert_not_contains "invalid complexity tier" "$err" "valid marker emits no invalid-tier warning"
assert_not_contains "no complexity marker" "$err" "valid marker emits no absent-marker message"

# -------- Test 3: absent marker -> routine, info-level, no warning --------
echo "Test 3: absent marker falls through to routine (info-level, not a warning)"
err="$(run_resolve 9002 2>&1 1>/dev/null)"
assert_contains "no complexity marker -> routine" "$err" "absent marker logs the info-level fall-through message"
assert_not_contains "invalid complexity tier" "$err" "absent marker never emits the invalid-tier warning"

# -------- Test 4: out-of-vocabulary marker names the invalid value --------
echo "Test 4: out-of-vocabulary marker ('trivial') is named in the warning"
err="$(run_resolve 9003 2>&1 1>/dev/null)"
assert_contains "invalid complexity tier 'trivial' -> routine" "$err" "invalid marker warning names the offending value"
assert_not_contains "no complexity marker" "$err" "invalid marker is not conflated with the absent-marker message"

# -------- Test 5: last-marker-wins when multiple markers are present --------
echo "Test 5: last marker wins when a body carries two"
err="$(run_resolve 9004 2>&1 1>/dev/null)"
assert_contains "tier=mechanical" "$err" "last marker (mechanical) wins over the first (complex)"
assert_not_contains "tier=complex" "$err" "first marker is not the one resolved"

# -------- Test 6: all four cases still exit 3 (unconfigured tierModels) --------
# Confirms the resolution layer itself is unchanged -- only the diagnostics
# were split -- by checking the well-known "no mapping" fall-through exit code
# every case shares in an unconfigured fixture repo.
echo "Test 6: unconfigured repo still falls through to exit 3 in every case"
rc=0
run_resolve 9001 >/dev/null 2>&1 || rc=$?
assert_contains "3" "$rc" "valid-marker case exits 3 (no tierModels configured)"
rc=0
run_resolve 9002 >/dev/null 2>&1 || rc=$?
assert_contains "3" "$rc" "absent-marker case exits 3 (no tierModels configured)"
rc=0
run_resolve 9003 >/dev/null 2>&1 || rc=$?
assert_contains "3" "$rc" "invalid-marker case exits 3 (no tierModels configured)"

# -------- Test 7: GraphQL failure falls back to REST body (#4472) --------
# When `gh issue view` (GraphQL) fails under quota exhaustion, the resolver must
# fall back to the REST body rather than silently defaulting to routine. Issue
# 9005's GraphQL fetch fails and REST returns a valid `complex` marker.
echo "Test 7: GraphQL failure falls back to REST-fetched body"
err="$(run_resolve 9005 2>&1 1>/dev/null)"
assert_contains "tier=complex" "$err" "REST-fallback body's tier (complex) is resolved, not routine"
assert_not_contains "could not fetch" "$err" "successful REST fallback emits no fetch-error message"

# -------- Test 8: both fetches fail -> routine, with a diagnostic (#4472) --------
# When GraphQL and REST both fail, the non-blocking resolver still degrades to
# routine (unchanged, non-breaking) but says so, so the degradation is visible.
echo "Test 8: total fetch failure degrades to routine with a diagnostic"
err="$(run_resolve 9006 2>&1 1>/dev/null)"
assert_contains "could not fetch body" "$err" "total fetch failure logs a fetch-error diagnostic"
assert_contains "-> routine" "$err" "total fetch failure still degrades to routine"

# -------- Test 9: prose mentioning the marker syntax before the real marker
# -------- is ignored (#4840) --------
# The bug this issue reports: a body that *discusses* the marker syntax in
# prose before the real marker (e.g. a meta issue about the complexity-marker
# feature itself, quoting `<!-- loom:complexity=<tier> -->` as literal example
# text) used to produce an empty first match that `head -1` picked over the
# real marker later in the body, silently resolving to `routine` instead of
# the issue's actual `mechanical` tier.
echo "Test 9: prose that mentions the marker syntax before the real marker is ignored"
err="$(run_resolve 9007 2>&1 1>/dev/null)"
assert_contains "tier=mechanical" "$err" "real marker (mechanical) is resolved despite preceding prose mentions"
assert_not_contains "invalid complexity tier" "$err" "prose mention never triggers the invalid-tier warning"
assert_not_contains "no complexity marker" "$err" "prose mention never triggers the absent-marker message"

# -------- Summary --------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
