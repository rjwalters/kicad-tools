#!/usr/bin/env bash
# test-extract-capability-markers.sh - tests for extract-capability-markers.sh,
# the reference parser for the `<!-- loom:capability=<name> -->` convention
# (#6892).
#
# Covers: no-marker (exit 1), single/multiple valid markers (exit 0,
# deduped+sorted), the `cloud-profile:<name>` prefix family, unknown values
# failing closed (exit 2, named on stderr, excluded from stdout), a mix of
# valid + unknown values, duplicate markers deduping, and the #4840-style
# false-positive guard (prose that quotes the marker syntax as literal
# example text must not be mistaken for a live marker).
#
# Usage:
#   ./defaults/scripts/tests/test-extract-capability-markers.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PARSER_SCRIPT="$SCRIPTS_DIR/extract-capability-markers.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg (expected '$expected', got '$actual')"
    fi
}

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

if [[ ! -x "$PARSER_SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $PARSER_SCRIPT not found or not executable" >&2
    exit 1
fi

run_parser() { # <body>
    printf '%s' "$1" | "$PARSER_SCRIPT"
}

# Capture stdout, stderr, and exit code from ONE invocation (avoids re-running
# the subject three times per assertion, which would be wasteful and easy to
# get out of sync). Sets globals: CAP_OUT, CAP_ERR, CAP_RC.
CAP_OUT=""
CAP_ERR=""
CAP_RC=0
capture_parser() { # <body>
    local outfile errfile
    outfile="$(mktemp "${TMPDIR:-/tmp}/capmarkers-out.XXXXXX")"
    errfile="$(mktemp "${TMPDIR:-/tmp}/capmarkers-err.XXXXXX")"
    printf '%s' "$1" | "$PARSER_SCRIPT" >"$outfile" 2>"$errfile"
    CAP_RC=$?
    CAP_OUT="$(cat "$outfile")"
    CAP_ERR="$(cat "$errfile")"
    rm -f "$outfile" "$errfile"
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$PARSER_SCRIPT" ]]; then
    pass "extract-capability-markers.sh is executable"
else
    fail "extract-capability-markers.sh is missing or not executable"
fi

# -------- Test 2: no marker at all -> exit 1, empty stdout --------
echo "Test 2: no marker -> exit 1, empty stdout"
out="$(run_parser "An issue body with no capability marker at all." 2>/dev/null)"; rc=$?
assert_eq "1" "$rc" "no-marker body exits 1"
assert_eq "" "$out" "no-marker body prints nothing to stdout"

# -------- Test 3: single valid marker -> exit 0, printed --------
echo "Test 3: single valid marker -> exit 0"
out="$(run_parser "Needs root.
<!-- loom:capability=host-sudo -->
" 2>/dev/null)"; rc=$?
assert_eq "0" "$rc" "single valid marker exits 0"
assert_eq "host-sudo" "$out" "single valid marker is printed alone"

# -------- Test 4: multiple valid markers -> deduped + sorted --------
echo "Test 4: multiple distinct valid markers -> sorted, deduped list"
out="$(run_parser "<!-- loom:capability=tailnet-access -->
<!-- loom:capability=host-sudo -->
<!-- loom:capability=host-sudo -->
" 2>/dev/null)"; rc=$?
assert_eq "0" "$rc" "multiple valid markers exit 0"
assert_eq "$(printf 'host-sudo\ntailnet-access')" "$out" "duplicate + multi markers dedupe and sort"

# -------- Test 5: cloud-profile:<name> family matches by prefix --------
echo "Test 5: cloud-profile:<name> prefix family"
out="$(run_parser "<!-- loom:capability=cloud-profile:prod-aws -->" 2>/dev/null)"; rc=$?
assert_eq "0" "$rc" "cloud-profile:<name> exits 0"
assert_eq "cloud-profile:prod-aws" "$out" "cloud-profile:<name> value is preserved verbatim"

# -------- Test 6: bare 'cloud-profile:' with no name is NOT valid --------
echo "Test 6: bare 'cloud-profile:' (no name after the colon) fails closed"
# The marker syntax itself still matches (the value grammar's `:` is part of
# the repeating character class, so "cloud-profile:" alone is a well-formed
# match) -- but the closed-vocabulary check requires strictly more characters
# after the `cloud-profile:` prefix (an actual name), so a bare trailing
# colon with nothing after it fails validation exactly like any other
# unrecognized value: exit 2, named on stderr, excluded from stdout. Pin
# this boundary down explicitly rather than leaving it implicit.
capture_parser "<!-- loom:capability=cloud-profile: -->"
assert_eq "2" "$CAP_RC" "bare 'cloud-profile:' with nothing after it fails closed-vocabulary validation"
assert_eq "" "$CAP_OUT" "bare 'cloud-profile:' prints nothing to stdout"
assert_contains "cloud-profile:" "$CAP_ERR" "bare 'cloud-profile:' is named on stderr as the offending value"

# -------- Test 7: unrecognized value fails closed -- named on stderr, excluded from stdout --------
echo "Test 7: unrecognized value fails closed (exit 2)"
capture_parser "<!-- loom:capability=host-sudooo -->"
assert_eq "2" "$CAP_RC" "unrecognized (misspelled) value exits 2"
assert_eq "" "$CAP_OUT" "unrecognized value is never printed to stdout"
assert_contains "UNKNOWN" "$CAP_ERR" "unrecognized value is named on stderr"
assert_contains "host-sudooo" "$CAP_ERR" "stderr names the exact offending value"

# -------- Test 8: mix of valid + unknown -- valid still prints, exit is still 2 --------
echo "Test 8: valid + unknown mixed -> valid prints, exit 2 (whole set unsatisfiable)"
capture_parser "<!-- loom:capability=host-sudo -->
<!-- loom:capability=totally-made-up -->
"
assert_eq "2" "$CAP_RC" "mixed valid+unknown exits 2"
assert_eq "host-sudo" "$CAP_OUT" "the valid marker in a mixed set still prints"
assert_contains "totally-made-up" "$CAP_ERR" "the unknown marker in a mixed set is named on stderr"

# -------- Test 9: prose quoting the marker syntax as example text is never a false match (#4840-style) --------
echo "Test 9: prose that quotes the marker syntax as literal example text is not a false match"
out="$(run_parser "This convention looks like \`<!-- loom:capability=<name> -->\` in prose,
with the real marker appearing after it:
<!-- loom:capability=host-sudo -->
" 2>/dev/null)"; rc=$?
assert_eq "0" "$rc" "real marker found despite preceding prose mention -> exit 0"
assert_eq "host-sudo" "$out" "only the real marker's value is reported, not a match on the prose"

# -------- Test 10: prose-only mention with no real marker anywhere -> no false positive --------
echo "Test 10: prose-only mention, no real marker anywhere -> exit 1, no false positive"
out="$(run_parser "This doc describes \`<!-- loom:capability=<name> -->\` as the syntax, with no live marker in this body." 2>/dev/null)"; rc=$?
assert_eq "1" "$rc" "prose-only mention (no live marker) exits 1, not a false positive"
assert_eq "" "$out" "prose-only mention prints nothing"

# -------- Test 11: no-space marker form -- extraction must not swallow the
# closing delimiter's dashes (#6914) --------
echo "Test 11: no-space marker form -- value must not pick up the closing delimiter's dashes"
out="$(run_parser '<!--loom:capability=tailnet-access-->' 2>/dev/null)"; rc=$?
assert_eq "0" "$rc" "no-space marker exits 0"
assert_eq "tailnet-access" "$out" "no-space marker's value excludes the closing delimiter's dashes"

# -------- Test 12: genuinely dash-suffixed value, no-space form -- must still
# fail closed as unknown (distinct from Test 11 -- the trailing dash here is
# part of the declared value itself, not the closing delimiter) --------
echo "Test 12: dash-suffixed value with no space before the delimiter still fails closed"
capture_parser '<!--loom:capability=host-sudo--->'
assert_eq "2" "$CAP_RC" "no-space dash-suffixed value exits 2"
assert_eq "" "$CAP_OUT" "no-space dash-suffixed value prints nothing to stdout"
assert_contains "host-sudo-" "$CAP_ERR" "stderr names the value with exactly one trailing dash, not extra"
assert_not_contains "host-sudo--" "$CAP_ERR" "stderr does not name the value with the delimiter's dashes appended"

# -------- Summary --------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
