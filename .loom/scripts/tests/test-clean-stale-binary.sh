#!/usr/bin/env bash
# test-clean-stale-binary.sh - Regression guard for #4384.
#
# clean.sh / cleanup.sh / recover-orphaned-shepherds.sh delegate to the native
# `loom-daemon clean` / `cleanup logs` / `recover-orphans` subcommands (#4272,
# PR #4301). Each capability-probes the resolved binary first, because a host
# mid-roll can carry a `loom-daemon` predating those subcommands.
#
# The original probe fell back to the `run_loom_tool` Python chain — but PR
# #4301 (commit dba33666) deleted `loom_tools/clean.py`, `cleanup.py`, and
# `orphan_recovery.py` (plus their console-script entries) in the very same
# commit, so the fallback was dead on arrival and produced a raw
# `No module named loom_tools.clean` traceback on every stale-binary host.
#
# This test asserts (modelled on test-probe-tokens-fallback.sh, the Phase 2
# precedent the wrappers already cite):
#   1. All three wrappers exist and are executable.
#   2. None of them references `run_loom_tool` / `lib/loom-tools.sh` / `python3`
#      in executable code — the dead fallback tier is gone, not just fixed.
#   3. With a `loom-daemon` that rejects the subcommand, each wrapper exits
#      non-zero, names the resolved binary path AND its reported version, names
#      the rebuild remedy, emits no `No module named` traceback, and never
#      invokes `python3`.
#   4. With a capable `loom-daemon`, each wrapper delegates unchanged and passes
#      its arguments through verbatim (`--dry-run`, `--force`, `logs`, …).
#   5. With no resolvable `loom-daemon` at all, each wrapper still fails with a
#      coherent, actionable message (no unbound-variable / traceback noise).
#
# Usage:
#   ./.loom/scripts/tests/test-clean-stale-binary.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CLEAN_SCRIPT="$SCRIPTS_DIR/clean.sh"
CLEANUP_SCRIPT="$SCRIPTS_DIR/cleanup.sh"
RECOVER_SCRIPT="$SCRIPTS_DIR/recover-orphaned-shepherds.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

STRIPPED_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

# ---------- stubs ----------
STUB_DIR="$WORK/stubs"
mkdir -p "$STUB_DIR"

# A `python3` that must never run. If any wrapper still reaches for the Python
# fallback chain, this drops a marker file we can detect.
PY_MARKER="$WORK/python3-was-invoked"
cat > "$STUB_DIR/python3" <<EOF
#!/usr/bin/env bash
echo "python3 stub invoked with: \$*" >> "$PY_MARKER"
exit 1
EOF
chmod +x "$STUB_DIR/python3"

# A stale loom-daemon: knows --version, rejects every subcommand (mirrors the
# real "error: unrecognized subcommand" clap failure).
STALE_BIN="$WORK/stale/loom-daemon"
mkdir -p "$(dirname "$STALE_BIN")"
cat > "$STALE_BIN" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
    echo "loom-daemon 0.15.0 (commit 615545ba, built 2026-07-29T15:23:18Z)"
    exit 0
fi
echo "error: unrecognized subcommand '${1:-}'" >&2
exit 2
EOF
chmod +x "$STALE_BIN"

# A current loom-daemon: answers the `--help` capability probe silently and
# records the real delegated invocation for arg-passthrough assertions.
CURRENT_BIN="$WORK/current/loom-daemon"
ARGS_FILE="$WORK/delegated-args"
mkdir -p "$(dirname "$CURRENT_BIN")"
cat > "$CURRENT_BIN" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--version" ]]; then
    echo "loom-daemon 0.16.0 (commit deadbeef, built 2026-07-29T00:00:00Z)"
    exit 0
fi
# The capability probe is "<subcommand> [sub] --help" — answer it, don't record.
for arg in "\$@"; do
    if [[ "\$arg" == "--help" ]]; then exit 0; fi
done
echo "\$*" >> "$ARGS_FILE"
exit 0
EOF
chmod +x "$CURRENT_BIN"

# ---------- Test 1: all three wrappers exist and are executable ----------
echo "Test 1: wrappers exist and are executable"
missing=0
for s in "$CLEAN_SCRIPT" "$CLEANUP_SCRIPT" "$RECOVER_SCRIPT"; do
    if [[ -x "$s" ]]; then
        pass "$(basename "$s") is executable"
    else
        fail "$(basename "$s") is missing or not executable: $s"
        missing=1
    fi
done
if [[ "$missing" -eq 1 ]]; then
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# ---------- Test 2: dead Python fallback tier is gone from executable code ----
# Comments explaining the history (why there is no fallback) are allowed —
# only non-comment lines count.
echo "Test 2: no run_loom_tool / loom-tools.sh / python3 in executable code"
for s in "$CLEAN_SCRIPT" "$CLEANUP_SCRIPT" "$RECOVER_SCRIPT"; do
    code_hits="$(grep -vE '^[[:space:]]*#' "$s" | grep -nE 'run_loom_tool|lib/loom-tools\.sh|python3' || true)"
    if [[ -n "$code_hits" ]]; then
        fail "$(basename "$s") executable code still reaches for the Python fallback: $code_hits"
    else
        pass "$(basename "$s") executable code has no Python fallback tier"
    fi
done

# ---------- Test 3: stale binary -> loud, actionable failure ----------
echo "Test 3: stale loom-daemon -> non-zero exit with rebuild remedy, no python3"
run_stale() {
    # $1 = script, rest = args
    local script="$1"; shift
    LOOM_DAEMON_BIN="$STALE_BIN" PATH="$STUB_DIR:$STRIPPED_PATH" \
        "$script" "$@" 2>&1
}

check_stale() {
    local script="$1"; shift
    local name; name="$(basename "$script")"
    local out rc
    out="$(run_stale "$script" "$@")"
    rc=$?

    if [[ "$rc" -ne 0 ]]; then
        pass "$name exits non-zero on a stale binary (rc=$rc)"
    else
        fail "$name exited 0 on a stale binary (expected non-zero). Output: $out"
    fi
    if [[ "$out" == *"$STALE_BIN"* ]]; then
        pass "$name names the resolved (stale) binary path"
    else
        fail "$name did not name the stale binary path. Got: $out"
    fi
    if [[ "$out" == *"615545ba"* ]]; then
        pass "$name reports the stale binary's version"
    else
        fail "$name did not report the stale binary's version. Got: $out"
    fi
    if [[ "$out" == *"cargo build --release -p loom-daemon"* ]]; then
        pass "$name names the rebuild remedy"
    else
        fail "$name did not name the rebuild remedy. Got: $out"
    fi
    if [[ "$out" == *"loom-daemon-update.sh"* ]]; then
        pass "$name names the installed-host update remedy"
    else
        fail "$name did not name the update remedy. Got: $out"
    fi
    if [[ "$out" == *"No module named"* ]]; then
        fail "$name still produced a 'No module named' traceback. Got: $out"
    else
        pass "$name produced no 'No module named' traceback"
    fi
}

check_stale "$CLEAN_SCRIPT" --dry-run
check_stale "$CLEANUP_SCRIPT" logs --dry-run
check_stale "$RECOVER_SCRIPT" --recover

if [[ -f "$PY_MARKER" ]]; then
    fail "python3 was invoked by a wrapper: $(cat "$PY_MARKER")"
else
    pass "python3 was never invoked by any wrapper"
fi

# ---------- Test 4: capable binary -> unchanged delegation, args passed through
echo "Test 4: capable loom-daemon -> delegation with verbatim arg passthrough"
run_current() {
    local script="$1"; shift
    LOOM_DAEMON_BIN="$CURRENT_BIN" PATH="$STUB_DIR:$STRIPPED_PATH" \
        "$script" "$@" >/dev/null 2>&1
}

check_current() {
    local script="$1"; local expected="$2"; shift 2
    local name; name="$(basename "$script")"
    : > "$ARGS_FILE"
    run_current "$script" "$@"
    local rc=$?
    if [[ "$rc" -eq 0 ]]; then
        pass "$name exits 0 on a capable binary"
    else
        fail "$name expected exit 0 on a capable binary, got $rc"
    fi
    local recorded; recorded="$(cat "$ARGS_FILE" 2>/dev/null)"
    if [[ "$recorded" == "$expected" ]]; then
        pass "$name delegated as 'loom-daemon $expected'"
    else
        fail "$name delegated as 'loom-daemon $recorded' (expected '$expected')"
    fi
}

check_current "$CLEAN_SCRIPT"   "clean --dry-run --force"          --dry-run --force
check_current "$CLEANUP_SCRIPT" "cleanup logs --dry-run"           logs --dry-run
check_current "$RECOVER_SCRIPT" "recover-orphans --recover --json" --recover --json

if [[ -f "$PY_MARKER" ]]; then
    fail "python3 was invoked on the happy path: $(cat "$PY_MARKER")"
else
    pass "python3 was never invoked on the happy path"
fi

# ---------- Test 5: no resolvable binary at all -> coherent failure ----------
# Stage hermetic copies of the wrappers so REPO_ROOT (SCRIPT_DIR/../..) has no
# target/{release,debug}/loom-daemon build-output candidate underneath it —
# otherwise this checkout's own freshly-built binary would be resolved.
echo "Test 5: no resolvable loom-daemon -> coherent, actionable failure"
STAGE="$WORK/stage/defaults/scripts"
mkdir -p "$STAGE/lib"
cp "$CLEAN_SCRIPT" "$CLEANUP_SCRIPT" "$RECOVER_SCRIPT" "$STAGE/"
cp "$SCRIPTS_DIR/lib/locate-daemon-bin.sh" "$STAGE/lib/"

# Isolate step 4 of loom_locate_daemon_bin (the machine-level install fallback
# under ${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}) too -- on a host with a real
# machine-level Loom install, leaving $HOME untouched would let that step
# resolve the real binary and defeat this "no resolvable binary" fixture
# (#5183).
EMPTY_HOME="$WORK/empty-home"
mkdir -p "$EMPTY_HOME"

for s in clean.sh cleanup.sh recover-orphaned-shepherds.sh; do
    out="$(LOOM_DAEMON_BIN="/nonexistent/loom-daemon" PATH="$STUB_DIR:$STRIPPED_PATH" \
        HOME="$EMPTY_HOME" LOOM_DAEMON_BIN_DIR="/nonexistent" \
        "$STAGE/$s" 2>&1)"
    rc=$?
    if [[ "$rc" -ne 0 ]]; then
        pass "$s exits non-zero with no resolvable binary (rc=$rc)"
    else
        fail "$s exited 0 with no resolvable binary. Output: $out"
    fi
    if [[ "$out" == *"no loom-daemon binary could be resolved"* ]]; then
        pass "$s reports that no daemon binary could be resolved"
    else
        fail "$s message was not coherent for the missing-binary case. Got: $out"
    fi
    if [[ "$out" == *"cargo build --release -p loom-daemon"* ]]; then
        pass "$s names the rebuild remedy with no resolvable binary"
    else
        fail "$s did not name the rebuild remedy. Got: $out"
    fi
done

if [[ -f "$PY_MARKER" ]]; then
    fail "python3 was invoked in the missing-binary case: $(cat "$PY_MARKER")"
else
    pass "python3 was never invoked in the missing-binary case"
fi

# ---------- Summary ----------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
