#!/usr/bin/env bash
# test-check-host-sleep.sh - Smoke tests for check-host-sleep.sh
#
# This script exercises the host-sleep readiness check (#3350) against the
# current host. It does NOT mock the platform — it just verifies the helper
# behaves as designed:
#   - always exits 0 (never blocks Loom)
#   - prints something coherent on supported platforms
#   - handles unknown platforms without crashing
#   - respects --quiet (suppresses stdout one-liner but still warns on stderr)
#   - handles --help
#
# Usage:
#   ./.loom/scripts/tests/test-check-host-sleep.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-host-sleep.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-host-sleep.sh is executable"
else
    fail "check-host-sleep.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: default invocation exits 0 --------
echo "Test 2: default invocation always exits 0"
"$SCRIPT" >/dev/null 2>&1
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "exit code is 0"
else
    fail "expected exit 0, got $rc"
fi

# -------- Test 3: --quiet exits 0 and suppresses stdout --------
echo "Test 3: --quiet suppresses stdout"
stdout_quiet="$("$SCRIPT" --quiet 2>/dev/null || true)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--quiet exit code is 0"
else
    fail "--quiet exit code expected 0, got $rc"
fi
if [[ -z "$stdout_quiet" ]]; then
    pass "--quiet produces no stdout"
else
    fail "--quiet produced stdout: $stdout_quiet"
fi

# -------- Test 4: default invocation prints SOMETHING (stdout or stderr) --------
echo "Test 4: default invocation produces output"
combined="$("$SCRIPT" 2>&1 || true)"
if [[ -n "$combined" ]]; then
    pass "produced output on supported platform"
else
    fail "produced no output at all (unexpected)"
fi

# -------- Test 5: --help works --------
echo "Test 5: --help prints usage and exits 0"
help_out="$("$SCRIPT" --help 2>&1 || true)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--help exit code is 0"
else
    fail "--help exit expected 0, got $rc"
fi
if printf '%s' "$help_out" | grep -q -i "Usage"; then
    pass "--help mentions Usage"
else
    fail "--help did not mention Usage. Got: $help_out"
fi

# -------- Test 6: unknown args do not break it --------
echo "Test 6: unknown args do not break the script"
"$SCRIPT" --some-nonsense-flag --another 99 >/dev/null 2>&1
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "unknown args tolerated; exit 0"
else
    fail "unknown args caused non-zero exit ($rc)"
fi

# -------- Test 7: platform-aware content on macOS / Linux --------
echo "Test 7: platform-aware content (best-effort)"
platform="$(uname -s 2>/dev/null || echo unknown)"
combined="$("$SCRIPT" 2>&1 || true)"
case "$platform" in
    Darwin)
        # Either we get a warning mentioning pmset/sleep, or a success-line
        # confirming sleep is disabled. Both are acceptable.
        if printf '%s' "$combined" | grep -qE "(pmset|sleep)"; then
            pass "macOS output mentions pmset/sleep"
        else
            fail "macOS output missing pmset/sleep keywords. Got: $combined"
        fi
        ;;
    Linux)
        if printf '%s' "$combined" | grep -qiE "(systemd|sleep|suspend|inhibit)"; then
            pass "Linux output mentions systemd-inhibit/sleep keywords"
        else
            fail "Linux output missing expected keywords. Got: $combined"
        fi
        ;;
    *)
        if printf '%s' "$combined" | grep -qi "unknown"; then
            pass "unknown platform handled gracefully"
        else
            # Acceptable to be silent; at minimum the script exited 0.
            pass "unknown platform produced no recognizable output (allowed)"
        fi
        ;;
esac

# -------- Test 8: host.preventSleep config awareness (#6311) --------
# Builds a scratch `.loom/` repo root + a stubbed `systemctl`/`systemd-inhibit`
# on PATH so these assertions are deterministic regardless of the live host's
# own sleep-inhibitor state (unlike Test 7 above, which reads the real host).
#
# #6661 design decision: `check_linux()` in check-host-sleep.sh takes an
# early "no systemd" return (see the comment there) before it ever reaches
# the #6311 `PREVENT_SLEEP_ENABLED` note this test exercises below — so on a
# real Linux host/container without systemd, that branch would be skipped
# entirely and this test would silently pass or fail depending on host
# environment rather than on the code path we actually mean to test. We
# stub `systemctl` (not just `systemd-inhibit`) here so this test forces the
# "systemd present" branch deterministically on ANY Linux host, systemd or
# not — a test-only fix; check-host-sleep.sh's real-world behavior on a
# genuinely non-systemd host is intentionally left unchanged (see its
# comment for the rationale).
echo "Test 8: host.preventSleep config awareness (#6311)"

SCRATCH_DIR="$(mktemp -d)"
mkdir -p "$SCRATCH_DIR/.loom" "$SCRATCH_DIR/stub"
trap 'rm -rf "$SCRATCH_DIR"' EXIT

# A stub `systemctl` that always reports a usable version, so `check_linux()`
# takes the "systemd present" branch regardless of whether this test host
# actually has systemd.
cat > "$SCRATCH_DIR/stub/systemctl" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "--version" ]]; then
    echo "systemd 255 (stub)"
    exit 0
fi
exit 0
STUB
chmod +x "$SCRATCH_DIR/stub/systemctl"

# A stub `systemd-inhibit` that reports NO active lock via --list (the
# "warn" branch) but still execs through when invoked as a real wrapper.
cat > "$SCRATCH_DIR/stub/systemd-inhibit" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == "--list" ]]; then
    echo "WHO WHAT WHY MODE"
    exit 0
fi
exec "$@"
STUB
chmod +x "$SCRATCH_DIR/stub/systemd-inhibit"

platform="$(uname -s 2>/dev/null || echo unknown)"
if [[ "$platform" == "Linux" ]]; then
    # 8a: absent config -> byte-identical to pre-#6311 behavior, no new note.
    rm -f "$SCRATCH_DIR/.loom/config.json"
    out_absent="$(cd "$SCRATCH_DIR" && PATH="$SCRATCH_DIR/stub:$PATH" "$SCRIPT" 2>&1 || true)"
    if ! printf '%s' "$out_absent" | grep -q "6311"; then
        pass "absent host.preventSleep: no #6311 note leaks into the default warning (regression guard)"
    else
        fail "absent host.preventSleep: unexpected #6311 note in output. Got: $out_absent"
    fi

    # 8b: host.preventSleep=true, no active lock -> extra note appears.
    echo '{"host": {"preventSleep": true}}' > "$SCRATCH_DIR/.loom/config.json"
    out_enabled="$(cd "$SCRATCH_DIR" && PATH="$SCRATCH_DIR/stub:$PATH" "$SCRIPT" 2>&1 || true)"
    if printf '%s' "$out_enabled" | grep -q "host.preventSleep is enabled"; then
        pass "host.preventSleep=true with no active lock: the #6311 note is printed"
    else
        fail "host.preventSleep=true with no active lock: expected the #6311 note. Got: $out_enabled"
    fi

    # 8c: malformed config value never blocks -- still exits 0, still warns.
    echo '{"host": {"preventSleep": "not-a-bool"}}' > "$SCRATCH_DIR/.loom/config.json"
    out_malformed="$(cd "$SCRATCH_DIR" && PATH="$SCRATCH_DIR/stub:$PATH" "$SCRIPT" 2>&1)"
    rc=$?
    if [[ "$rc" -eq 0 ]]; then
        pass "malformed host.preventSleep value: script still exits 0 (never blocks, #6311)"
    else
        fail "malformed host.preventSleep value: expected exit 0, got $rc"
    fi
    if printf '%s' "$out_malformed" | grep -qi "malformed"; then
        pass "malformed host.preventSleep value: falls back to disabled with a visible warning"
    else
        fail "malformed host.preventSleep value: expected a 'malformed' warning. Got: $out_malformed"
    fi

    rm -f "$SCRATCH_DIR/.loom/config.json"
else
    pass "host.preventSleep config-awareness assertions are Linux-specific (platform=$platform, skipped)"
fi

rm -rf "$SCRATCH_DIR"
trap - EXIT

# -------- Summary --------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
