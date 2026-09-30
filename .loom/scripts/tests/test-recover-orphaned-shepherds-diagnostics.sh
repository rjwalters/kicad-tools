#!/usr/bin/env bash
# test-recover-orphaned-shepherds-diagnostics.sh — Regression guard for #6392.
#
# Bug: recover-orphaned-shepherds.sh execs straight into a resolved
# `loom-daemon recover-orphans` and inherits its stderr. Before this fix,
# lib/locate-daemon-bin.sh's `loom_locate_daemon_bin: resolved ... via ...`
# success trace (#4997) was always printed to stderr ahead of the exec, so a
# caller that surfaces "the first line of stderr" as the failure reason on
# any non-zero exit (e.g. /loom:sweep's orphan-recovery capability
# pre-probe) could end up quoting that success trace verbatim — which reads
# like the failure itself even though it always reports a successful
# resolution. Observed live: `loom-daemon recover-orphans` exiting 2
# ("orphans found in dry-run mode" — itself not a failure, see
# cleanup_ops.rs) previously emitted no stderr diagnostic of its own at all,
# leaving the trace as the ONLY line in stderr.
#
# This suite asserts, with a fake `loom-daemon` standing in for the real
# binary (so it needs no build and is fast/hermetic):
#   1. By default (no --verbose/-v), the resolution-success trace is
#      suppressed and does not appear anywhere in stderr.
#   2. The exec'd binary's own stderr output still passes through untouched
#      — the wrapper does not eat or reorder it.
#   3. With --verbose (or -v), the resolution-success trace DOES appear in
#      stderr again (opt-in, not removed outright — other callers of the
#      shared lib function still get it unconditionally, see
#      test-locate-daemon-bin.sh).
#   4. The script's own header documents what each exit code means (#6392
#      AC3).
#
# Usage:
#   ./.loom/scripts/tests/test-recover-orphaned-shepherds-diagnostics.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RECOVER_SCRIPT="$SCRIPTS_DIR/recover-orphaned-shepherds.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

if [[ ! -x "$RECOVER_SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $RECOVER_SCRIPT not found or not executable" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

STRIPPED_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

# A fake `loom-daemon` that answers the capability probe, then on the real
# `recover-orphans` invocation prints a report to stdout, a single
# unambiguous diagnostic line to stderr, and exits 2 -- mirroring the real
# "orphans found in dry-run mode" shape (cleanup_ops.rs's
# format_dry_run_orphans_found_stderr()).
FAKE_BIN="$WORK/fake/loom-daemon"
mkdir -p "$(dirname "$FAKE_BIN")"
cat > "$FAKE_BIN" <<'EOF'
#!/usr/bin/env bash
for arg in "$@"; do
    if [[ "$arg" == "--help" ]]; then exit 0; fi
done
echo "Orphaned Spawn-Loop Task Detection & Recovery"
echo "1 orphaned claim(s) found"
echo "recover-orphans: 1 orphaned claim(s) found in dry-run mode (exiting 2, not a failure) -- see the report above, or rerun with --recover to reclaim them" >&2
exit 2
EOF
chmod +x "$FAKE_BIN"

run_recover() { # <extra args...>
    LOOM_DAEMON_BIN="$FAKE_BIN" PATH="$STRIPPED_PATH" \
        "$RECOVER_SCRIPT" "$@" >"$WORK/stdout" 2>"$WORK/stderr"
    return $?
}

# ---------- Test 1: default invocation suppresses the resolution trace ----------
echo "Test 1: default invocation (no --verbose) suppresses the binary-resolution trace"
run_recover
rc=$?
stderr_out="$(cat "$WORK/stderr")"
if [[ "$rc" -eq 2 ]]; then
    pass "exits 2, passing through the daemon's own exit code"
else
    fail "expected exit 2, got $rc. stderr: $stderr_out"
fi
if [[ "$stderr_out" != *"loom_locate_daemon_bin: resolved"* ]]; then
    pass "resolution-success trace is suppressed by default"
else
    fail "resolution-success trace leaked into stderr by default: $stderr_out"
fi

# ---------- Test 2: the exec'd binary's own diagnostic still passes through ----------
echo "Test 2: the daemon's own stderr diagnostic is preserved verbatim"
if [[ "$stderr_out" == *"1 orphaned claim(s) found in dry-run mode (exiting 2, not a failure)"* ]]; then
    pass "daemon's diagnostic line reaches stderr unmodified"
else
    fail "daemon's diagnostic line did not reach stderr: $stderr_out"
fi
first_line="$(head -n1 "$WORK/stderr")"
if [[ "$first_line" == *"orphaned claim(s) found in dry-run mode"* ]]; then
    pass "the FIRST line of stderr is the genuine diagnostic, not a resolution trace (#6392 core fix)"
else
    fail "the first line of stderr was not diagnostic: $first_line"
fi

# ---------- Test 3: --verbose restores the resolution trace ----------
echo "Test 3: --verbose opts back into the resolution-success trace"
run_recover --verbose
verbose_stderr="$(cat "$WORK/stderr")"
if [[ "$verbose_stderr" == *"loom_locate_daemon_bin: resolved"* ]]; then
    pass "--verbose restores the resolution-success trace"
else
    fail "--verbose did not restore the resolution-success trace: $verbose_stderr"
fi

echo "Test 3b: -v (short form) also restores the resolution-success trace"
run_recover -v
short_verbose_stderr="$(cat "$WORK/stderr")"
if [[ "$short_verbose_stderr" == *"loom_locate_daemon_bin: resolved"* ]]; then
    pass "-v restores the resolution-success trace"
else
    fail "-v did not restore the resolution-success trace: $short_verbose_stderr"
fi

# ---------- Test 4: --verbose is still passed through to the daemon binary ----------
echo "Test 4: --verbose/-v are still forwarded to the daemon binary verbatim"
FORWARD_BIN="$WORK/forward/loom-daemon"
mkdir -p "$(dirname "$FORWARD_BIN")"
ARGS_FILE="$WORK/forwarded-args"
cat > "$FORWARD_BIN" <<EOF
#!/usr/bin/env bash
for arg in "\$@"; do
    if [[ "\$arg" == "--help" ]]; then exit 0; fi
done
echo "\$*" >> "$ARGS_FILE"
exit 0
EOF
chmod +x "$FORWARD_BIN"
LOOM_DAEMON_BIN="$FORWARD_BIN" PATH="$STRIPPED_PATH" "$RECOVER_SCRIPT" --verbose --json >/dev/null 2>&1
recorded="$(cat "$ARGS_FILE" 2>/dev/null)"
if [[ "$recorded" == "recover-orphans --verbose --json" ]]; then
    pass "--verbose --json forwarded verbatim to 'recover-orphans'"
else
    fail "expected 'recover-orphans --verbose --json', got '$recorded'"
fi

# ---------- Test 5: script header documents exit codes (#6392 AC3) ----------
echo "Test 5: script header documents exit code meanings"
header="$(sed -n '1,40p' "$RECOVER_SCRIPT")"
for code_desc in "0 " "1 " "2 " "3 "; do
    if grep -qE "^#[[:space:]]*${code_desc}—" <<<"$header"; then
        pass "header documents exit code '${code_desc}—'"
    else
        fail "header does not document exit code '${code_desc}—'. Header:\n$header"
    fi
done

# ---------- Summary ----------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
