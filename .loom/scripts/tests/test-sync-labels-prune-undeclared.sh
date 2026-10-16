#!/usr/bin/env bash
# test-sync-labels-prune-undeclared.sh - sync-labels.sh --prune-undeclared
# (#11105).
#
# A label that leaves labels.yml stays on the forge: the additive sync never
# deletes a Loom label, so retired and never-declared loom:* names pile up in
# the label picker. --prune-undeclared reports them (--dry-run) and deletes
# the unused ones; the logic is `loom-daemon labels undeclared`, so this suite
# pins the working-tree binary (lib/require-daemon-bin.sh) and stubs only
# `gh`. No network, no real label mutation.
#
# The load-bearing assertions:
#   1. --dry-run reports every undeclared loom:* label (and only those: a
#      declared label, a non-loom: label and a loom: label on an open issue
#      are not "would delete"), and deletes nothing.
#   2. A label on an open issue/PR is KEPT and named with its numbers, in both
#      modes; it is never deleted.
#   3. Without --dry-run the unused undeclared labels are deleted, nothing
#      else is, and the run still succeeds (exit 0) while warning that a kept
#      label remains.
#   4. A bare --dry-run (no --prune-undeclared) stays forge-free.
#   5. --help documents the flag.
#   6. The real run passes the real #9548 write-scope gate (the fixture is
#      registered, nothing is stubbed), and a target the credential cannot
#      write gets no `label delete` at all.
#
# Usage:
#   ./defaults/scripts/tests/test-sync-labels-prune-undeclared.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SLS="$SCRIPTS_DIR/sync-labels.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "labels"
# The real run vets its write target twice (#9548): the shell's
# `loom_write_repo` and `labels undeclared --prune`'s own may_write_from. The
# fixture is registered so the real gate admits it (write-scope-fixture.sh).
# shellcheck source=lib/write-scope-fixture.sh
source "$TEST_DIR/lib/write-scope-fixture.sh"
# The permission probe must ask the fixture's gh, not a host's cached GitHub
# App installation snapshot (which never lists owner/repo): the snapshot's own
# kill switch restores the probe's two-leg path. The gate itself still runs.
export LOOM_INSTALLATION_SNAPSHOT=0

PASSED=0
FAILED=0
pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1"; [[ -n "${2:-}" ]] && printf '    %s\n' "$2"; }
check() { if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1" "expected '$2', got '$3'"; fi; }
contains() { if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1" "missing '$3' in: ${2:0:900}"; fi; }
lacks() { if [[ "$2" != *"$3"* ]]; then pass "$1"; else fail "$1" "unexpected '$3' in: ${2:0:900}"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" 2>/dev/null || true' EXIT
STUB_DIR="$TMP/stub"
mkdir -p "$STUB_DIR"

# --- Stub gh ------------------------------------------------------------------
# Logs every argv to $LOOM_TEST_GH_LOG. The live label set is
# $LOOM_TEST_GH_LIVE (one name per line); `loom:in-progress` is on open
# issue #7. Every label write succeeds (and is only ever logged).
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${LOOM_TEST_GH_LOG:?}"
case "$1" in
  repo) if [[ "$*" == *viewerPermission* ]]; then echo ADMIN; else echo owner/repo; fi; exit 0 ;;
  label) exit 0 ;;
  api)
    args="$*"
    if [[ "$args" == *"repos/owner/repo/labels"* ]]; then
      printf '%s\n' "${LOOM_TEST_GH_LIVE:-}"; exit 0
    fi
    if [[ "$args" == *"repos/owner/repo/issues"* ]]; then
      [[ "$args" == *"labels=loom:in-progress"* ]] && printf '7\n'
      exit 0
    fi
    exit 0 ;;
esac
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

SRC="$TMP/src"
mkdir -p "$SRC/.github"
cat > "$SRC/.github/labels.yml" <<'EOF'
# BEGIN LOOM LABELS
- name: loom:issue
  description: "Approved and ready for a Builder"
  color: "3B82F6"
# END LOOM LABELS
EOF

WRITE_SCOPE_INNER_GH="$STUB_DIR/gh" write_scope_register "$SRC" owner/repo
FIXTURE_GH="$LOOM_GH_BIN"

LIVE=$'loom:issue\nloom:healing\nloom:failed:judge\nloom:in-progress\nbug\nrust\npriority:high'
GH_LOG="$TMP/gh.log"
RC=0 OUT="" LOG=""
run_sls() {
    : > "$GH_LOG"
    OUT="$(
        cd "$SRC" || exit 99
        PATH="$STUB_DIR:$PATH" LOOM_GH_BIN="$FIXTURE_GH" \
        LOOM_TEST_GH_LOG="$GH_LOG" LOOM_TEST_GH_LIVE="$LIVE" \
        LOOM_CONFIG_DEFAULTS_FILE="" LOOM_FORGE_TYPE=github \
        bash "$SLS" "$@" 2>&1
    )"
    RC=$?
    LOG="$(cat "$GH_LOG")"
}

echo "=== --prune-undeclared --dry-run reports, deletes nothing ==="
run_sls --repo owner/repo --prune-undeclared --dry-run
check "dry run exits 0" "0" "$RC"
contains "unused undeclared label is reported" "$OUT" "UNDECLARED loom:healing (unused: would delete)"
contains "second unused undeclared label is reported" "$OUT" "UNDECLARED loom:failed:judge (unused: would delete)"
contains "an in-use undeclared label is kept, with its issue" "$OUT" "KEPT loom:in-progress (open: #7)"
lacks "a declared label is not reported" "$OUT" "loom:issue (unused"
lacks "a non-loom: label is never reported" "$OUT" "UNDECLARED rust"
lacks "priority:high is not a loom: label" "$OUT" "UNDECLARED priority:high"
contains "the run warns that undeclared labels remain" "$OUT" "Undeclared loom:* labels remain on owner/repo"
lacks "dry run deletes nothing" "$LOG" "label delete"
contains "the open-usage check asks for OPEN items only" "$LOG" "state=open"

echo "=== --prune-undeclared deletes only the unused undeclared labels ==="
run_sls --repo owner/repo --prune-undeclared
check "real run exits 0" "0" "$RC"
contains "unused label deleted" "$LOG" "label delete loom:healing --repo owner/repo --yes"
contains "second unused label deleted" "$LOG" "label delete loom:failed:judge --repo owner/repo --yes"
lacks "label on an open issue is not deleted" "$LOG" "label delete loom:in-progress"
lacks "a declared label is not deleted" "$LOG" "label delete loom:issue"
lacks "a non-loom: label is not deleted" "$LOG" "label delete rust"
contains "pruned labels are reported" "$OUT" "PRUNED loom:healing"
contains "kept label is reported" "$OUT" "KEPT loom:in-progress (open: #7)"

echo "=== a credential that cannot write the target deletes nothing ==="
rm -rf "$LOOM_WRITE_SCOPE_CACHE_DIR"
WRITE_SCOPE_INNER_GH="$STUB_DIR/gh" write_scope_register "$SRC" owner/repo pull
run_sls --repo owner/repo --prune-undeclared
if [[ "$RC" -ne 0 ]]; then pass "a refused prune exits non-zero"; else fail "a refused prune exits non-zero" "got 0"; fi
lacks "a refused prune makes no label delete" "$LOG" "label delete"
rm -rf "$LOOM_WRITE_SCOPE_CACHE_DIR"
WRITE_SCOPE_INNER_GH="$STUB_DIR/gh" write_scope_register "$SRC" owner/repo

echo "=== a bare --dry-run stays forge-free ==="
run_sls --repo owner/repo --dry-run
check "bare dry run exits 0" "0" "$RC"
check "bare dry run makes no gh call" "" "$LOG"
contains "bare dry run points at the flag" "$OUT" "pass --prune-undeclared to list undeclared loom:* labels"

echo "=== --help documents the flag ==="
run_sls --help
contains "--help names --prune-undeclared" "$OUT" "--prune-undeclared"

echo ""
echo "Results: $PASSED passed, $FAILED failed"
[[ "$FAILED" -eq 0 ]]
