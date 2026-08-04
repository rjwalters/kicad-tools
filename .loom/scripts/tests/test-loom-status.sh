#!/usr/bin/env bash
# test-loom-status.sh — Tests for loom-status.sh's machine-daemon integration
# and expanded work-queue label counting (Issue #4793).
#
# Before #4793, `loom status` inspected ONLY the local tmux agent pool: a repo
# actively managed by a separate machine-level `loom-daemon` fleet (zero local
# tmux sessions, autonomous work-finder / role-runner dispatching sweeps)
# still printed a bare "No agents running" — misleadingly implying nothing
# would pick up open issues. This suite drives loom-status.sh entirely from a
# scratch workspace + a stubbed `loom-daemon` binary (pinned via
# LOOM_DAEMON_BIN) and a stubbed `gh` (pinned via PATH) — no real daemon, no
# real forge, no real tmux agent pool.
#
# Style matches the other CLI test suites (test-blame-issue.sh,
# test-loom-daemon-watchdog.sh) — plain bash, hand-rolled assertions. Bats is
# NOT used in this repository.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUS_SCRIPT="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-status.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "${GREEN}✓${NC} $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "${RED}✗${NC} $1"; }

assert_eq() { # <expected> <actual> <msg>
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$1', got '$2')"; fi
}

assert_contains() { # <needle> <haystack> <msg>
    if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3 (expected to find '$1')"; fi
}

assert_not_contains() { # <needle> <haystack> <msg>
    if [[ "$2" != *"$1"* ]]; then pass "$3"; else fail "$3 (did NOT expect to find '$1')"; fi
}

if [[ ! -x "$STATUS_SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $STATUS_SCRIPT not found or not executable" >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "jq not found on PATH -- skipping (loom-status.sh hard-requires jq for these code paths)"
    exit 0
fi

# ---- Scratch workspace -------------------------------------------------------
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-loom-status.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

REPO="$WORKDIR/repo"
mkdir -p "$REPO/.loom"
cat > "$REPO/.loom/config.json" <<'EOF'
{"terminals": []}
EOF

# Canonical (symlink-resolved) repo root -- loom-status.sh matches against
# THIS form (mirrors its own REPO_ROOT_CANON computation via `pwd -P`), so
# every stub below must embed this exact path, not the raw $REPO.
REPO_CANON="$(cd "$REPO" && pwd -P)"

FAKE_BIN="$WORKDIR/bin"
mkdir -p "$FAKE_BIN"

# ---- Fake `gh` stub -----------------------------------------------------------
# Answers `gh issue list --label <label> --state open --json number --jq length`
# and the `pr` equivalent with a fixed count per label, driven by the
# GH_LABEL_COUNTS env var (space-separated "label=count" pairs).
cat > "$FAKE_BIN/gh" <<'FAKEGH'
#!/usr/bin/env bash
kind="$1"; shift
label=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --label) label="$2"; shift 2 ;;
        *) shift ;;
    esac
done
for pair in $GH_LABEL_COUNTS; do
    key="${pair%%=*}"
    val="${pair#*=}"
    if [[ "$key" == "$label" ]]; then
        echo "$val"
        exit 0
    fi
done
echo "0"
FAKEGH
chmod +x "$FAKE_BIN/gh"

# ---- Fake `loom-daemon` stub factory -------------------------------------------
# Writes a `loom-daemon` binary at $WORKDIR/daemon-<mode>/loom-daemon that
# answers `status --json` per the named scenario. Echoes the stub's path.
make_daemon_stub() { # <mode>
    local mode="$1"
    local dir="$WORKDIR/daemon-$mode"
    mkdir -p "$dir"
    case "$mode" in
        managed)
            cat > "$dir/loom-daemon" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == "status" && "\$2" == "--json" ]]; then
  cat <<JSON
{"per_repo":[{"root":"$REPO_CANON","priority":100,"in_flight_count":3,"health_gate_halted":false}],
 "role_tick_records":[{"root":"$REPO_CANON","role":"champion","at":"2026-07-31T00:00:00Z","ok":true}],
 "work_finder_enabled":true}
JSON
  exit 0
fi
exit 1
EOF
            ;;
        managed-halted)
            cat > "$dir/loom-daemon" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == "status" && "\$2" == "--json" ]]; then
  cat <<JSON
{"per_repo":[{"root":"$REPO_CANON","priority":100,"in_flight_count":0,"health_gate_halted":true}],
 "role_tick_records":[],"work_finder_enabled":true}
JSON
  exit 0
fi
exit 1
EOF
            ;;
        unmanaged)
            cat > "$dir/loom-daemon" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "status" && "$2" == "--json" ]]; then
  echo '{"per_repo":[{"root":"/some/other/repo","priority":100,"in_flight_count":0,"health_gate_halted":false}],"role_tick_records":[],"work_finder_enabled":true}'
  exit 0
fi
exit 1
EOF
            ;;
        unreachable)
            cat > "$dir/loom-daemon" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "status" && "$2" == "--json" ]]; then
  echo '{"error":"could not reach loom-daemon at /tmp/x.sock: connect failed","install_state":{"state":"ExpectedButDead","started_at":"2026-07-30T00:00:00Z","pid":null,"liveness_detail":"no live pid file"}}'
  exit 74
fi
exit 1
EOF
            ;;
        *) echo "unknown stub mode $mode" >&2; return 1 ;;
    esac
    chmod +x "$dir/loom-daemon"
    echo "$dir/loom-daemon"
}

# Run loom-status.sh from $REPO with the given extra env assignments (as
# "KEY=VAL" args) and GH_LABEL_COUNTS pinned to a benign default (all zero).
# Sets globals OUT (combined stdout) and RC.
run_status() {
    OUT="$( (cd "$REPO" && env PATH="$FAKE_BIN:$PATH" GH_LABEL_COUNTS="${GH_LABEL_COUNTS:-}" "$@" bash "$STATUS_SCRIPT") 2>&1 )"
    RC=$?
}

run_status_json() {
    OUT="$( (cd "$REPO" && env PATH="$FAKE_BIN:$PATH" GH_LABEL_COUNTS="${GH_LABEL_COUNTS:-}" "$@" bash "$STATUS_SCRIPT" --json) 2>&1 )"
    RC=$?
}

TMUX_MISSING=false
command -v tmux >/dev/null 2>&1 || TMUX_MISSING=true

# ===================================================================
# 1. Script sanity: executable, --help.
# ===================================================================
echo "Test 1: script exists and is executable"
if [[ -x "$STATUS_SCRIPT" ]]; then pass "loom-status.sh is executable"; else fail "loom-status.sh is not executable"; fi

echo "Test 2: --help exits 0 and documents the Machine Daemon section"
OUT="$( (cd "$REPO" && bash "$STATUS_SCRIPT" --help) 2>&1 )"; RC=$?
assert_eq 0 "$RC" "--help exits 0"
assert_contains "Machine Daemon" "$OUT" "--help documents the Machine Daemon section"
assert_contains "loom:architect" "$OUT" "--help documents the expanded work-queue labels"

if [[ "$TMUX_MISSING" == "true" ]]; then
    echo "tmux not found on PATH -- skipping tests 3-9 (loom-status.sh's daemon/work-queue sections run only past the tmux-availability check)"
else

# ===================================================================
# 3. No loom-daemon binary resolvable at all: falls back to the ORIGINAL
#    "No agents running." message, and the JSON `daemon` block reports
#    binary_found:false. No Machine Daemon section is printed.
# ===================================================================
echo "Test 3: no loom-daemon binary resolvable -- original message, section omitted"
run_status LOOM_DAEMON_BIN=/nonexistent LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json"
assert_eq 0 "$RC" "no-binary case exits 0"
assert_contains "No agents running." "$OUT" "no-binary case prints the ORIGINAL unscoped message"
assert_not_contains "Machine Daemon:" "$OUT" "no-binary case omits the Machine Daemon section"

run_status_json LOOM_DAEMON_BIN=/nonexistent LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json"
assert_eq 0 "$RC" "no-binary --json case exits 0"
bf="$(echo "$OUT" | jq -r '.daemon.binary_found' 2>/dev/null)"
assert_eq "false" "$bf" "no-binary --json: daemon.binary_found is false"

# ===================================================================
# 4. Daemon reachable and this repo IS a managed workspace: the empty local
#    pool is named explicitly (AC2), and the Machine Daemon section reports
#    liveness + in-flight count + open gate.
# ===================================================================
echo "Test 4: daemon reachable, repo IS managed -- scoped message + daemon detail"
bin="$(make_daemon_stub managed)"
run_status LOOM_DAEMON_BIN="$bin" LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json"
assert_eq 0 "$RC" "managed case exits 0"
assert_contains "local agent pool: none" "$OUT" "managed case scopes the empty-pool message"
assert_contains "machine daemon manages this repo" "$OUT" "managed case names the daemon explicitly"
assert_contains "Machine Daemon:" "$OUT" "managed case prints the Machine Daemon section"
assert_contains "in-flight sweeps: 3" "$OUT" "managed case reports the in-flight sweep count"
assert_contains "champion" "$OUT" "managed case lists the recently-ticked role"

run_status_json LOOM_DAEMON_BIN="$bin" LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json"
manages="$(echo "$OUT" | jq -r '.daemon.manages_repo' 2>/dev/null)"
in_flight="$(echo "$OUT" | jq -r '.daemon.in_flight' 2>/dev/null)"
assert_eq "true" "$manages" "managed --json: daemon.manages_repo is true"
assert_eq "3" "$in_flight" "managed --json: daemon.in_flight is 3"

# ===================================================================
# 5. Daemon reachable, gate HALTED for this repo.
# ===================================================================
echo "Test 5: daemon reachable, this repo's dispatch gate is HALTED"
bin="$(make_daemon_stub managed-halted)"
run_status LOOM_DAEMON_BIN="$bin" LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json"
assert_contains "HALTED" "$OUT" "halted-gate case reports HALTED"

# ===================================================================
# 6. Daemon reachable but this repo is NOT a registered workspace: the
#    original unscoped message is kept (nothing IS managing this repo).
# ===================================================================
echo "Test 6: daemon reachable, repo NOT managed -- original message kept"
bin="$(make_daemon_stub unmanaged)"
run_status LOOM_DAEMON_BIN="$bin" LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json"
assert_contains "No agents running." "$OUT" "unmanaged case keeps the original unscoped message"
assert_contains "NOT a registered/managed workspace" "$OUT" "unmanaged case names the gap explicitly"

# ===================================================================
# 7. Daemon UNREACHABLE, but the on-disk registry lists this repo: the
#    registry-file fallback (#4793 AC) still surfaces "registered but not
#    answering" instead of silently reporting nothing.
# ===================================================================
echo "Test 7: daemon unreachable, registry file lists this repo"
bin="$(make_daemon_stub unreachable)"
registry="$WORKDIR/workspaces.json"
jq -n --arg root "$REPO_CANON" '{version:1, workspaces:[{root:$root, priority:100}]}' > "$registry"
run_status LOOM_DAEMON_BIN="$bin" LOOM_WORKSPACES_PATH="$registry"
assert_contains "unreachable" "$OUT" "unreachable case reports the daemon as unreachable"
assert_contains "ExpectedButDead" "$OUT" "unreachable case surfaces the install_state classification"
assert_contains "this repo IS registered" "$OUT" "unreachable case still reports registry membership"

run_status_json LOOM_DAEMON_BIN="$bin" LOOM_WORKSPACES_PATH="$registry"
manages="$(echo "$OUT" | jq -r '.daemon.manages_repo' 2>/dev/null)"
reachable="$(echo "$OUT" | jq -r '.daemon.reachable' 2>/dev/null)"
install_state="$(echo "$OUT" | jq -r '.daemon.install_state' 2>/dev/null)"
assert_eq "true" "$manages" "unreachable --json: daemon.manages_repo is true via the registry fallback"
assert_eq "false" "$reachable" "unreachable --json: daemon.reachable is false"
assert_eq "ExpectedButDead" "$install_state" "unreachable --json: daemon.install_state is ExpectedButDead"

# ===================================================================
# 8. Work-queue label counting: the four proposal labels
#    (loom:architect/hermit/curated/auditor) are counted and rendered
#    alongside the original three, never all-zero when the fixture gh
#    stub reports non-zero counts.
# ===================================================================
echo "Test 8: work queue counts + renders the proposal labels"
GH_LABEL_COUNTS="loom:issue=1 loom:review-requested=2 loom:pr=3 loom:architect=4 loom:hermit=5 loom:curated=6 loom:auditor=7"
run_status LOOM_DAEMON_BIN=/nonexistent LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json"
assert_contains "Pending proposals:" "$OUT" "work queue prints the Pending proposals line"
assert_contains "loom:architect 4" "$OUT" "work queue reports the loom:architect count"
assert_contains "loom:hermit 5" "$OUT" "work queue reports the loom:hermit count"
assert_contains "loom:curated 6" "$OUT" "work queue reports the loom:curated count"
assert_contains "loom:auditor 7" "$OUT" "work queue reports the loom:auditor count"
unset GH_LABEL_COUNTS

run_status_json LOOM_DAEMON_BIN=/nonexistent LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json" GH_LABEL_COUNTS="loom:architect=9"
architect="$(echo "$OUT" | jq -r '.work_queue."loom:architect"' 2>/dev/null)"
assert_eq "9" "$architect" "work_queue --json includes loom:architect"

# ===================================================================
# 9. LOOM_DAEMON_BIN pointed at a non-executable path: treated identically
#    to "no binary resolvable" (never errors, never crashes the script).
# ===================================================================
echo "Test 9: LOOM_DAEMON_BIN set but not executable -- degrades gracefully"
run_status LOOM_DAEMON_BIN="$WORKDIR/does-not-exist" LOOM_WORKSPACES_PATH="$WORKDIR/no-such-registry.json"
assert_eq 0 "$RC" "non-executable LOOM_DAEMON_BIN still exits 0"
assert_contains "No agents running." "$OUT" "non-executable LOOM_DAEMON_BIN falls back to the original message"

fi # TMUX_MISSING

# ---------------------------------------------------------------------------
echo ""
echo "=== $TESTS_PASSED/$TESTS_RUN tests passed ==="
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    exit 1
fi
exit 0
