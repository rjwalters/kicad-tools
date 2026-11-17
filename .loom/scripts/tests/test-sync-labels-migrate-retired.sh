#!/usr/bin/env bash
# test-sync-labels-migrate-retired.sh - sync-labels.sh --migrate-retired
# (#11511), hermetic: a stub daemon and a stub gh, no network.
#
# The migration logic (relabel, prune, every safety rule) is
# `loom-daemon labels migrate`, unit-tested in Rust against a fake forge. This
# pins only the shell call-site:
#   1. --dry-run calls `labels migrate` WITHOUT --apply (the read-only preview).
#   2. A real run calls it WITH --apply, for the resolved repo, and only AFTER
#      the additive sync (so the replacement labels exist first).
#   3. Exit 3 from the verb ("something remains") warns but does not fail;
#      any other non-zero exit fails the run.
#   4. Without --migrate-retired the verb is never called.
#   5. A daemon without the verb is refused with a clear message.
#   6. --help documents the flag.
#
# Usage:
#   ./defaults/scripts/tests/test-sync-labels-migrate-retired.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLS="$(cd "$TEST_DIR/.." && pwd)/sync-labels.sh"

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
LOG="$TMP/calls.log"

# Stub gh: logs argv; every label read/write succeeds.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "${LOOM_TEST_LOG:?}"
case "$1" in
  repo) if [[ "$*" == *viewerPermission* ]]; then echo ADMIN; else echo owner/repo; fi ;;
esac
exit 0
STUB
chmod +x "$STUB_DIR/gh"

# Stub daemon: `forge may-write` admits owner/repo (the shell's #9548 gate);
# `labels migrate --help` succeeds unless FAKE_NO_VERB=1; a real `labels
# migrate` logs argv, prints a report line and exits FAKE_RC.
DAEMON="$STUB_DIR/loom-daemon"
cat > "$DAEMON" <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2" == "forge may-write" ]]; then echo owner/repo; exit 0; fi
if [[ "$1 $2" == "labels migrate" ]]; then
  if [[ " $* " == *" --help "* ]]; then [[ "${FAKE_NO_VERB:-0}" == 1 ]] && exit 2; exit 0; fi
  printf 'daemon %s\n' "$*" >> "${LOOM_TEST_LOG:?}"
  echo "RELABEL #7 old -> new"
  exit "${FAKE_RC:-0}"
fi
exit 0
STUB
chmod +x "$DAEMON"

SRC="$TMP/src"
mkdir -p "$SRC/.github"
cat > "$SRC/.github/labels.yml" <<'EOF'
# BEGIN LOOM LABELS
- name: loom:issue
  description: "Approved and ready for a Builder"
  color: "3B82F6"
# END LOOM LABELS
EOF

RC=0 OUT="" CALLS=""
run_sls() {
    : > "$LOG"
    OUT="$(
        cd "$SRC" || exit 99
        PATH="$STUB_DIR:$PATH" LOOM_GH_BIN="$STUB_DIR/gh" LOOM_TEST_LOG="$LOG" \
        LOOM_DAEMON_BIN="$DAEMON" LOOM_DAEMON_SELF_BIN="$DAEMON" \
        LOOM_CONFIG_DEFAULTS_FILE="" LOOM_FORGE_TYPE=github \
        bash "$SLS" "$@" 2>&1
    )"
    RC=$?
    CALLS="$(cat "$LOG")"
}

echo "=== --migrate-retired --dry-run previews (no --apply) ==="
run_sls --repo owner/repo --migrate-retired --dry-run
check "dry run exits 0" "0" "$RC"
contains "the verb is called for the repo" "$CALLS" "daemon labels migrate --repo owner/repo"
lacks "the dry run never passes --apply" "$CALLS" "--apply"
contains "the verb's report is shown" "$OUT" "RELABEL #7 old -> new"
lacks "the dry run makes no label write" "$CALLS" "gh label create"

echo "=== --migrate-retired applies, after the additive sync ==="
run_sls --repo owner/repo --migrate-retired
check "real run exits 0" "0" "$RC"
contains "a real run passes --apply" "$CALLS" "daemon labels migrate --repo owner/repo --apply"
sync_line="$(grep -n '^gh label ' "$LOG" | head -1 | cut -d: -f1)"
migrate_line="$(grep -n '^daemon labels migrate' "$LOG" | head -1 | cut -d: -f1)"
if [[ -n "$sync_line" && -n "$migrate_line" && "$sync_line" -lt "$migrate_line" ]]; then
    pass "the additive sync runs before the migration"
else
    fail "the additive sync runs before the migration" "sync=$sync_line migrate=$migrate_line"
fi
contains "nothing remaining is reported as done" "$OUT" "No retired labels left to migrate on owner/repo"

echo "=== exit 3 warns, other exits fail ==="
FAKE_RC=3 run_sls --repo owner/repo --migrate-retired
check "exit 3 is not a failure" "0" "$RC"
contains "exit 3 warns" "$OUT" "Retired-label migration not finished on owner/repo"
FAKE_RC=4 run_sls --repo owner/repo --migrate-retired
check "exit 4 fails the run" "1" "$RC"
contains "the failure names the verb" "$OUT" "loom-daemon labels migrate failed (exit 4)"

echo "=== without the flag the verb is never called ==="
run_sls --repo owner/repo
check "plain run exits 0" "0" "$RC"
lacks "no migration without --migrate-retired" "$CALLS" "labels migrate"

echo "=== a daemon without the verb is refused ==="
FAKE_NO_VERB=1 run_sls --repo owner/repo --migrate-retired --dry-run
check "refused run exits 1" "1" "$RC"
contains "the refusal names the verb" "$OUT" "--migrate-retired needs a loom-daemon with 'labels migrate'"

echo "=== --help documents the flag ==="
help_out="$(bash "$SLS" --help 2>&1)"
contains "--help lists --migrate-retired" "$help_out" "--migrate-retired"

echo ""
echo "Passed: $PASSED  Failed: $FAILED"
[[ "$FAILED" -eq 0 ]]
