#!/usr/bin/env bash
# test-agent-destroy.sh — Regression test for #7465.
#
# agent-destroy.sh's "don't remove a worktree a live process still has as
# its cwd" safety check (the line right above this test's subject) used to
# request `lsof -F pt` and match the awk pattern `/^tcwd/` against it. That
# is a field-selector bug: `-F pt` requests the PID field (`p`) and the file
# TYPE field (`t`) — whose values are things like REG/DIR/VREG/CHR, never
# "cwd". The FD field (`f`) is the one that carries the value `cwd`, and
# `-F pf` is what actually requests it. So `/^tcwd/` could never match, and
# the safety check was a silent no-op regardless of whether a live process
# actually had the worktree as its cwd (mirrors the identical bug fixed on
# the Rust side by PR #7259/#7252, and independently discovered on the bash
# side by PR #7467).
#
# Covers two things:
#   1. Regression guard: the fixed field selector (`-F pf`) and awk match
#      (`/^fcwd/`) are present in agent-destroy.sh's source (both the
#      installed and source-tree copies, if both exist) — guards against the
#      bug being silently reintroduced.
#   2. Functional check: a real backgrounded process with its cwd inside a
#      scratch directory is actually detected via the corrected lsof/awk
#      pipeline extracted verbatim from the script.
#
# This is a light, targeted test (not a full tmux+session integration test
# of agent-destroy.sh's `main()`) — proportionate to the single-line,
# well-understood fix it covers.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# --- Resolve the subject: prefer the installed copy, fall back to source tree ---
INSTALLED_SUBJECT="$REPO_ROOT/.loom/scripts/agent-destroy.sh"
SOURCE_SUBJECT="$SCRIPTS_DIR/agent-destroy.sh"

CANDIDATES=()
[[ -f "$INSTALLED_SUBJECT" ]] && CANDIDATES+=("$INSTALLED_SUBJECT")
[[ -f "$SOURCE_SUBJECT" ]] && CANDIDATES+=("$SOURCE_SUBJECT")

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
    echo "FATAL: agent-destroy.sh not found at $INSTALLED_SUBJECT or $SOURCE_SUBJECT" >&2
    exit 1
fi

echo "=== Test 1: field-selector regression guard (source inspection) ==="
for subject in "${CANDIDATES[@]}"; do
    if grep -qE "lsof \+d \"\\\$worktree_real\" -F pf" "$subject" \
       && grep -qE "/\\^fcwd/\{print pid\}" "$subject"; then
        pass "$subject uses the corrected -F pf / /^fcwd/ pair"
    else
        fail "$subject does not use the corrected -F pf / /^fcwd/ pair"
    fi

    if grep -qE "lsof \+d \"\\\$worktree_real\" -F pt" "$subject" \
       || grep -qE "/\\^tcwd/\{print pid\}" "$subject"; then
        fail "$subject still contains the buggy -F pt / /^tcwd/ pattern"
    else
        pass "$subject no longer contains the buggy -F pt / /^tcwd/ pattern"
    fi
done

echo ""
echo "=== Test 2: functional detection (live process, corrected pipeline) ==="
if ! command -v lsof >/dev/null 2>&1; then
    echo -e "  ${YELLOW}SKIP${NC}: lsof not available on this host"
else
    TMP=$(mktemp -d /tmp/loom-agent-destroy-test.XXXXXX)
    trap 'rm -rf "$TMP"' EXIT

    # Background a process whose cwd is the scratch dir, mirroring the
    # issue's manual Test Plan repro.
    (cd "$TMP" && exec sleep 60) &
    BG_PID=$!

    # Give lsof a brief moment to be able to see the new process's cwd.
    for _ in 1 2 3 4 5; do
        if lsof +d "$TMP" -F pf 2>/dev/null | grep -q "^p${BG_PID}\$"; then
            break
        fi
        sleep 0.5
    done

    # The exact pipeline shape used by the corrected agent-destroy.sh line
    # (self-contained here rather than sourced, since the check lives inline
    # inside main() rather than in an extractable function).
    active_pids=$(lsof +d "$TMP" -F pf 2>/dev/null | awk '/^p/{pid=substr($0,2)} /^fcwd/{print pid}' | grep -v "$$" || true)

    if echo "$active_pids" | grep -qx "$BG_PID"; then
        pass "corrected -F pf / /^fcwd/ pipeline detects a live process with cwd in the scratch dir (PID $BG_PID)"
    else
        fail "corrected pipeline did NOT detect PID $BG_PID (active_pids='$active_pids')"
    fi

    # Sanity: prove the OLD buggy pipeline would have missed it (documents
    # the bug this test guards against; not a statement about current code).
    buggy_pids=$(lsof +d "$TMP" -F pt 2>/dev/null | awk '/^p/{pid=substr($0,2)} /^tcwd/{print pid}' | grep -v "$$" || true)
    if [[ -z "$buggy_pids" ]]; then
        pass "buggy -F pt / /^tcwd/ pipeline confirmed to miss the same live process (regression evidence)"
    else
        fail "buggy pipeline unexpectedly matched something (buggy_pids='$buggy_pids') — repro assumption invalid"
    fi

    kill "$BG_PID" 2>/dev/null || true
    wait "$BG_PID" 2>/dev/null || true
fi

echo ""
echo "=== Results: $TESTS_PASSED/$TESTS_RUN passed ==="
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
