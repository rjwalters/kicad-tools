#!/usr/bin/env bash
# test-record-noop-release.sh — regression test for #6740 ("Wire a first
# in-repo call site for `loom-daemon noop-cooldown record`", follow-up to
# #6670/PR #6739).
#
# Covers `defaults/scripts/record-noop-release.sh`, the shell helper
# `/loom:sweep`'s Builder phase invokes right before releasing a `loom:building`
# claim back to `loom:issue` on a genuine "no actionable delta this pass"
# conclusion (see sweep.md -> "Genuine no-op conclusion vs. builder failure").
#
# Mirrors `test-build-gate-timeout.sh`'s Section 2 (dispatch-backoff record
# call-shape assertion, #6192/#4485) for the sibling `noop-cooldown record`
# mechanism:
#   - A recording stub daemon binary (driven via $LOOM_DAEMON_BIN) proves the
#     EXACT call shape: `noop-cooldown record <ISSUE> --reason <TEXT>` — a
#     positional issue argument, NOT an `--issue` flag (the actual
#     `loom-daemon noop-cooldown record` CLI takes ISSUE positionally; the
#     `--issue` shape sometimes seen in prose/docs does not exist).
#   - A missing/unreachable/erroring daemon must never fail the caller: the
#     script always exits 0 in that case (best-effort, #6670).
#   - A genuine usage error (missing/non-numeric ISSUE) is NOT swallowed —
#     that is a caller bug, not a daemon-availability condition.
#
# Hermetic: stubs the daemon binary via $LOOM_DAEMON_BIN, never touches a
# forge, a socket, or a real toolchain.
#
# Usage:
#   bash defaults/scripts/tests/test-record-noop-release.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Resolve a shipped script by its path relative to the scripts root, preferring
# the installed location over the source-tree one — same precedent as
# test-build-gate-timeout.sh (#6194): every subject here is a *shipped*
# script (scripts/* -> .loom/scripts/* per scripts/install/manifest.sh), so
# this suite is meaningful in an installed consumer repo too.
resolve_shipped_script() {
    local rel="$1"
    if [[ -f "$REPO_ROOT/.loom/scripts/$rel" ]]; then
        printf '%s\n' "$REPO_ROOT/.loom/scripts/$rel"
    else
        printf '%s\n' "$REPO_ROOT/defaults/scripts/$rel"
    fi
}

RECORD_NOOP="$(resolve_shipped_script "record-noop-release.sh")"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

passed=0
failed=0
pass() { echo -e "${GREEN}\xe2\x9c\x93${NC} $1"; passed=$((passed + 1)); }
fail() { echo -e "${RED}\xe2\x9c\x97${NC} $1"; failed=$((failed + 1)); }

if [[ ! -f "$RECORD_NOOP" ]]; then
    echo "ERROR: record-noop-release.sh not found at $REPO_ROOT/.loom/scripts/record-noop-release.sh or $REPO_ROOT/defaults/scripts/record-noop-release.sh" >&2
    exit 1
fi

STUB_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$STUB_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'cleanup; exit 1' INT TERM

# ---------------------------------------------------------------------------
# Section 1: exact call-shape assertion via a recording stub daemon binary
# ---------------------------------------------------------------------------

CALL_LOG="$STUB_DIR/calls.log"
cat > "$STUB_DIR/loom-daemon" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALL_LOG"
exit 0
EOF
chmod +x "$STUB_DIR/loom-daemon"

: > "$CALL_LOG"
output="$(cd "$REPO_ROOT" && LOOM_DAEMON_BIN="$STUB_DIR/loom-daemon" \
    bash "$RECORD_NOOP" 6740 --reason "no actionable delta this pass" 2>&1)"
rc=$?

if [[ "$rc" -eq 0 ]]; then
    pass "recording a no-op release exits 0"
else
    fail "expected exit 0, got $rc: $output"
fi

if grep -q "noop-cooldown record" "$CALL_LOG" 2>/dev/null; then
    pass "invokes 'loom-daemon noop-cooldown record' (#6670)"
else
    fail "expected a noop-cooldown record call, log was: $(cat "$CALL_LOG" 2>/dev/null)"
fi

# The issue number must be POSITIONAL (the real CLI has no --issue flag) —
# assert it appears as a bare token, not prefixed by --issue.
if grep -Eq '(^|[[:space:]])6740([[:space:]]|$)' "$CALL_LOG" 2>/dev/null; then
    pass "the issue number is passed positionally (matches the real CLI shape)"
else
    fail "expected a bare positional '6740' in the recorded call, log was: $(cat "$CALL_LOG" 2>/dev/null)"
fi

if grep -q -- "--issue" "$CALL_LOG" 2>/dev/null; then
    fail "recorded call used a non-existent '--issue' flag: $(cat "$CALL_LOG" 2>/dev/null)"
else
    pass "recorded call does not use the non-existent '--issue' flag"
fi

if grep -q -- "--reason no actionable delta this pass" "$CALL_LOG" 2>/dev/null; then
    pass "the reason text is forwarded verbatim"
else
    fail "expected the reason text in the recorded call, log was: $(cat "$CALL_LOG" 2>/dev/null)"
fi

# ---------------------------------------------------------------------------
# Section 2: missing/unreachable/erroring daemon is a SILENT no-op (#6670)
# ---------------------------------------------------------------------------

# No claim to arm anything with a genuinely nonexistent binary path.
: > "$CALL_LOG"
noop_output="$(cd "$REPO_ROOT" && LOOM_DAEMON_BIN="$STUB_DIR/does-not-exist" \
    bash "$RECORD_NOOP" 6741 --reason "x" 2>&1)"
noop_rc=$?

if [[ "$noop_rc" -eq 0 ]]; then
    pass "a nonexistent \$LOOM_DAEMON_BIN path still exits 0 (falls through to other resolution, never fails)"
else
    fail "expected exit 0 even when \$LOOM_DAEMON_BIN does not resolve, got $noop_rc: $noop_output"
fi

# An erroring/unreachable daemon binary must never change the caller's own
# verdict — the daemon-side failure is swallowed.
cat > "$STUB_DIR/err-daemon" <<'EOF'
#!/usr/bin/env bash
echo "Could not reach loom-daemon" >&2
exit 1
EOF
chmod +x "$STUB_DIR/err-daemon"

err_output="$(cd "$REPO_ROOT" && LOOM_DAEMON_BIN="$STUB_DIR/err-daemon" \
    bash "$RECORD_NOOP" 6742 --reason "x" 2>&1)"
err_rc=$?

if [[ "$err_rc" -eq 0 ]]; then
    pass "an erroring/unreachable daemon still exits 0 (best-effort, never fails the caller)"
else
    fail "expected exit 0 despite a failing daemon, got $err_rc: $err_output"
fi

# ---------------------------------------------------------------------------
# Section 3: --workspace-root auto-derivation (#6957)
#
# The script's caller (`/loom:sweep`'s Builder phase, see sweep.md) has never
# passed --workspace-root explicitly. On a single-workspace daemon that is
# harmless (the daemon's cwd-seeded default registry IS the one repo's
# registry), but on a multi-workspace daemon (the epic supervisor's
# multi-repo fan-out, #3928) the omission silently arms the cooldown on the
# WRONG registry — the daemon's `resolve_registry` falls back to its
# cwd-seeded "default" registry whenever `workspace_root` is absent, which is
# unlikely to be the calling repo's own per-repo registry. Fix: the script
# now defaults --workspace-root to this repo's own root ($_repo_root,
# resolved via `git rev-parse --show-toplevel`) whenever the caller doesn't
# supply one, so the daemon always arms the cooldown on the correct registry
# regardless of caller diligence.
# ---------------------------------------------------------------------------

EXPECTED_ROOT="$(cd "$REPO_ROOT" && git rev-parse --show-toplevel 2>/dev/null)"

if [[ -z "$EXPECTED_ROOT" ]]; then
    echo "ERROR: could not resolve REPO_ROOT's git toplevel — is $REPO_ROOT inside a git checkout?" >&2
    exit 1
fi

: > "$CALL_LOG"
autoroot_output="$(cd "$REPO_ROOT" && LOOM_DAEMON_BIN="$STUB_DIR/loom-daemon" \
    bash "$RECORD_NOOP" 6957 --reason "no explicit workspace-root supplied" 2>&1)"
autoroot_rc=$?

if [[ "$autoroot_rc" -eq 0 ]]; then
    pass "recording a no-op release with no --workspace-root still exits 0"
else
    fail "expected exit 0, got $autoroot_rc: $autoroot_output"
fi

if grep -Fq -- "--workspace-root $EXPECTED_ROOT" "$CALL_LOG" 2>/dev/null; then
    pass "with no explicit --workspace-root, the call auto-derives it from the repo root (#6957)"
else
    fail "expected '--workspace-root $EXPECTED_ROOT' to be auto-derived, log was: $(cat "$CALL_LOG" 2>/dev/null)"
fi

# An explicit --workspace-root must still win over the auto-derived default
# (e.g. a future caller that DOES know the right root explicitly).
: > "$CALL_LOG"
EXPLICIT_ROOT="$STUB_DIR/some-other-repo"
mkdir -p "$EXPLICIT_ROOT"
explicit_output="$(cd "$REPO_ROOT" && LOOM_DAEMON_BIN="$STUB_DIR/loom-daemon" \
    bash "$RECORD_NOOP" 6958 --reason "x" --workspace-root "$EXPLICIT_ROOT" 2>&1)"
explicit_rc=$?

if [[ "$explicit_rc" -eq 0 ]]; then
    pass "recording a no-op release with an explicit --workspace-root still exits 0"
else
    fail "expected exit 0, got $explicit_rc: $explicit_output"
fi

if grep -Fq -- "--workspace-root $EXPLICIT_ROOT" "$CALL_LOG" 2>/dev/null; then
    pass "an explicit --workspace-root overrides the auto-derived repo root"
else
    fail "expected '--workspace-root $EXPLICIT_ROOT' to be forwarded verbatim, log was: $(cat "$CALL_LOG" 2>/dev/null)"
fi

if grep -Fq -- "$EXPECTED_ROOT" "$CALL_LOG" 2>/dev/null; then
    fail "explicit --workspace-root call unexpectedly also mentions the auto-derived repo root: $(cat "$CALL_LOG" 2>/dev/null)"
else
    pass "explicit --workspace-root call does not also carry the auto-derived repo root"
fi

# ---------------------------------------------------------------------------
# Section 4: a genuine usage error is NOT swallowed (distinct from the
# daemon-availability cases above)
# ---------------------------------------------------------------------------

missing_output="$(cd "$REPO_ROOT" && bash "$RECORD_NOOP" 2>&1)"
missing_rc=$?

if [[ "$missing_rc" -ne 0 ]]; then
    pass "a missing <ISSUE> argument is a real usage error (non-zero exit)"
else
    fail "expected a non-zero exit for a missing <ISSUE> argument, got $missing_rc: $missing_output"
fi

nonnumeric_output="$(cd "$REPO_ROOT" && bash "$RECORD_NOOP" not-a-number 2>&1)"
nonnumeric_rc=$?

if [[ "$nonnumeric_rc" -ne 0 ]]; then
    pass "a non-numeric <ISSUE> argument is a real usage error (non-zero exit)"
else
    fail "expected a non-zero exit for a non-numeric <ISSUE> argument, got $nonnumeric_rc: $nonnumeric_output"
fi

echo ""
echo "=== Results: $passed passed, $failed failed ==="
if [[ "$failed" -gt 0 ]]; then
    exit 1
fi
exit 0
