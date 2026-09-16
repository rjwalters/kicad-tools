#!/usr/bin/env bash
# test-sync-labels-check.sh - Unit tests for sync-labels.sh's --check flag
# (#6716).
#
# Why --check exists: nothing previously re-verified that a fleet repo's LIVE
# forge label set still matched .github/labels.yml after install --
# kicad-tools drifted 3 loom:operator* labels missing, silently corrupting a
# downstream operator census tool's bucketing (#6716). sync-labels.sh already
# had the additive, non-destructive create/update machinery; --check adds a
# read-only report mode resync-installed.sh can call to detect the drift
# before deciding whether to invoke the (already-existing, unchanged) mutating
# path to fix it.
#
# This is a black-box test: sync-labels.sh is a full CLI script, so we stub
# `gh` on PATH (the same stub shape as test-sync-labels-repo-flag.sh, plus a
# `label list --json name,color,description` case --check depends on), run
# the real script as a subprocess, and assert on exit codes, stdout/stderr,
# and the recorded `gh` argv log.
#
# The load-bearing assertions:
#   1. Fully in sync -> exit 0, no gh mutation call, reports "all in sync".
#   2. A declared label missing on the live repo -> exit 3, reported as
#      MISSING, no gh label create/edit/delete call (report-only).
#   3. A declared label present but with a stale color/description -> exit 3,
#      reported as STALE, still no mutation.
#   4. An unrecognized loom:-prefixed label present live but undeclared ->
#      exit 3, reported as an UNKNOWN EXTRA -- and never deleted.
#   5. A non-loom:-prefixed extra (e.g. a repo's own custom label) is not
#      reported at all -- --check only cares about the loom:* surface.
#   6. --check composes with --repo (retargets the read, same as the mutating
#      path already does).
#   7. --check is entirely read-only: exactly one `gh label list` call, zero
#      `gh label create/edit/delete` calls, in every scenario above.
#   8. A forge lookup failure (`gh label list` itself fails) is a loud error,
#      exit 1 -- distinct from the "drift found" exit 3.
#   9. --help documents --check.
#
# Usage:
#   ./defaults/scripts/tests/test-sync-labels-check.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SLS="$SCRIPTS_DIR/sync-labels.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$SLS" ]]; then
    echo -e "${RED}FATAL${NC}: $SLS not found or not executable" >&2
    exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" 2>/dev/null || true' EXIT

STUB_DIR="$TMP/stub"
mkdir -p "$STUB_DIR"

# --- Stub gh on PATH ---------------------------------------------------------
# Appends every invocation's argv to $LOOM_TEST_GH_LOG (one line per call).
#
#   gh repo view <NWO> ...        -> the --repo preflight (unused by --check,
#                                     which never mutates, but exercised here
#                                     to prove --check skips it too)
#   gh repo view ... (no target)  -> NWO resolution (no-flag path)
#   gh label list --json name --jq '.[].name' ...
#                                  -> the legacy "does this ONE label exist"
#                                     probe used by the mutating sync path
#   gh label list --json name,color,description --jq ... --limit ...
#                                  -> the --check snapshot: echoes
#                                     $LOOM_TEST_GH_LABELS verbatim (already
#                                     TSV: name<TAB>color<TAB>description, one
#                                     per line), or fails when
#                                     LOOM_TEST_GH_LABELS_FAIL=1
#   gh label create|edit|delete   -> exit 0 (should never be called by
#                                     --check; asserted against directly)
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${LOOM_TEST_GH_LOG:?stub gh: LOOM_TEST_GH_LOG not set}"

case "$1" in
  repo)
    if [[ "${2:-}" == "view" && -n "${3:-}" && "$3" != -* ]]; then
      printf 'ADMIN\n'
      exit 0
    fi
    if [[ -z "${LOOM_TEST_GH_NWO:-}" ]]; then
      echo "stub gh: no remote configured" >&2
      exit 1
    fi
    printf '%s\n' "$LOOM_TEST_GH_NWO"
    exit 0
    ;;
  label)
    case "$2" in
      list)
        if printf '%s\n' "$*" | grep -q -- '--json name,color,description'; then
          if [[ "${LOOM_TEST_GH_LABELS_FAIL:-0}" == "1" ]]; then
            echo "stub gh: simulated forge failure listing labels" >&2
            exit 1
          fi
          printf '%s\n' "${LOOM_TEST_GH_LABELS:-}"
          exit 0
        fi
        exit 0
        ;;
      create|edit|delete) exit 0 ;;
    esac
    echo "stub gh: unhandled label args: $*" >&2
    exit 3
    ;;
  api)
    exit 0
    ;;
esac
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

# --- Scratch source tree -----------------------------------------------------
SRC="$TMP/src"
mkdir -p "$SRC/.github"
cat > "$SRC/.github/labels.yml" <<'EOF'
# BEGIN LOOM LABELS
- name: loom:issue
  description: "Approved and ready for a Builder"
  color: "3B82F6"
- name: loom:pr
  description: "Approved pull request"
  color: "10B981"
- name: loom:operator-mechanical
  description: "Parked pending a mechanical human action"
  color: "F59E0B"
# END LOOM LABELS
EOF

GH_LOG="$TMP/gh.log"

# run_sls [--nwo NWO] [--labels TSV] [--labels-fail] -- <script args...>
#
#   --labels        the stub's `gh label list --json name,color,description`
#                    response: name<TAB>color<TAB>description per line
#   --labels-fail    make that same call fail (simulated forge error)
#
# Sets: RC, OUT (merged stdout+stderr), LOG (recorded gh argv lines).
RC=0
OUT=""
LOG=""
run_sls() {
    local nwo="" labels="" labels_fail="0"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --nwo) nwo="$2"; shift 2 ;;
            --labels) labels="$2"; shift 2 ;;
            --labels-fail) labels_fail="1"; shift ;;
            --) shift; break ;;
            *) break ;;
        esac
    done
    : > "$GH_LOG"
    OUT="$(
        cd "$SRC" || exit 99
        PATH="$STUB_DIR:$PATH" \
        LOOM_TEST_GH_LOG="$GH_LOG" \
        LOOM_TEST_GH_NWO="$nwo" \
        LOOM_TEST_GH_LABELS="$labels" \
        LOOM_TEST_GH_LABELS_FAIL="$labels_fail" \
        LOOM_CONFIG_DEFAULTS_FILE="" \
        bash "$SLS" "$@" 2>&1
    )"
    RC=$?
    LOG="$(cat "$GH_LOG")"
}

# A fully-in-sync live snapshot for the three declared labels above.
IN_SYNC_TSV=$'loom:issue\t3b82f6\tApproved and ready for a Builder\nloom:pr\t10b981\tApproved pull request\nloom:operator-mechanical\tf59e0b\tParked pending a mechanical human action'

echo ""
echo "=== --check: fully in sync ==="

run_sls --nwo owner/repo --labels "$IN_SYNC_TSV" -- --check
assert_eq "0" "$RC" "--check exits 0 when the live set fully matches"
assert_contains "$OUT" "all in sync" "--check reports the in-sync verdict"
assert_not_contains "$OUT" "MISSING" "in-sync run reports no MISSING labels"
assert_not_contains "$OUT" "STALE" "in-sync run reports no STALE labels"
assert_not_contains "$OUT" "UNKNOWN EXTRA" "in-sync run reports no unknown extras"
assert_not_contains "$LOG" "label create" "--check never creates a label"
assert_not_contains "$LOG" "label edit" "--check never edits a label"
assert_not_contains "$LOG" "label delete" "--check never deletes a label"
assert_eq "1" "$(printf '%s\n' "$LOG" | grep -c '^label list' || true)" \
    "--check makes exactly one 'gh label list' call"

echo ""
echo "=== --check: a declared label is missing on the live repo ==="

MISSING_TSV=$'loom:issue\t3b82f6\tApproved and ready for a Builder\nloom:operator-mechanical\tf59e0b\tParked pending a mechanical human action'
run_sls --nwo owner/repo --labels "$MISSING_TSV" -- --check
assert_eq "3" "$RC" "--check exits 3 when a declared label is missing"
assert_contains "$OUT" "MISSING       loom:pr" "the missing label is named"
assert_contains "$OUT" "1 missing" "the summary counts exactly one missing label"
assert_not_contains "$LOG" "label create" \
    "a missing label is reported, never auto-created by --check itself"

echo ""
echo "=== --check: a declared label is present but stale (color/description) ==="

STALE_TSV=$'loom:issue\tFF0000\tSomething else entirely\nloom:pr\t10b981\tApproved pull request\nloom:operator-mechanical\tf59e0b\tParked pending a mechanical human action'
run_sls --nwo owner/repo --labels "$STALE_TSV" -- --check
assert_eq "3" "$RC" "--check exits 3 when a declared label has drifted color/description"
assert_contains "$OUT" "STALE         loom:issue" "the stale label is named"
assert_contains "$OUT" "live=ff0000 vs declared=3b82f6" \
    "the stale report shows both the live and declared color, normalized to lowercase"
assert_contains "$OUT" "Something else entirely" \
    "the stale report shows the live description"
assert_not_contains "$LOG" "label edit" \
    "a stale label is reported, never auto-updated by --check itself"

echo ""
echo "=== --check: an unrecognized loom:-prefixed label is an unknown extra ==="

EXTRA_TSV=$'loom:issue\t3b82f6\tApproved and ready for a Builder\nloom:pr\t10b981\tApproved pull request\nloom:operator-mechanical\tf59e0b\tParked pending a mechanical human action\nloom:mystery\tabcdef\tNot declared anywhere'
run_sls --nwo owner/repo --labels "$EXTRA_TSV" -- --check
assert_eq "3" "$RC" "--check exits 3 when an undeclared loom:-prefixed label exists live"
assert_contains "$OUT" "UNKNOWN EXTRA loom:mystery" "the unknown extra is named"
assert_contains "$OUT" "never deleted automatically" \
    "the unknown-extra report states it is never auto-deleted"
assert_not_contains "$LOG" "label delete" \
    "an unknown extra is reported, never deleted by --check"

echo ""
echo "=== --check: a non-loom:-prefixed extra is ignored entirely ==="

NONLOOM_EXTRA_TSV=$'loom:issue\t3b82f6\tApproved and ready for a Builder\nloom:pr\t10b981\tApproved pull request\nloom:operator-mechanical\tf59e0b\tParked pending a mechanical human action\nbug\tD73A4A\tSomething is broken'
run_sls --nwo owner/repo --labels "$NONLOOM_EXTRA_TSV" -- --check
assert_eq "0" "$RC" "a non-loom:-prefixed extra alone does not count as drift"
assert_not_contains "$OUT" "UNKNOWN EXTRA" \
    "a non-loom:-prefixed label (e.g. GitHub's own 'bug') is never flagged"

echo ""
echo "=== --check composes with --repo ==="

run_sls --labels "$IN_SYNC_TSV" -- --repo octocat/hello-world --check
assert_eq "0" "$RC" "--check --repo exits 0 when in sync"
assert_contains "$LOG" "label list -R octocat/hello-world" \
    "--check --repo targets the override NWO"
assert_not_contains "$LOG" "repo view octocat/hello-world --json nameWithOwner,viewerPermission" \
    "--check skips the (mutation-only) --repo preflight"

echo ""
echo "=== --check: a forge lookup failure is a loud, distinct error ==="

# #7745: an unreachable forge exits 4, NOT 1. The two used to share exit 1,
# which left every caller unable to tell "I could not reach the forge"
# (benign) from "this script is broken" (a defect). resync-installed.sh's
# catch-all absorbed both as a skip and still exited 0, which is how a
# crashing checker went unnoticed on macOS for weeks. 1 now means only the
# unexpected case.
run_sls --nwo owner/repo --labels-fail -- --check
assert_eq "4" "$RC" "an unreachable forge exits 4 (distinct from drift's 3 and from a defect's 1)"
assert_contains "$OUT" "Could not list labels" "the lookup failure is reported"
assert_not_contains "$LOG" "label create" "a lookup failure performs no mutation"

echo ""
echo "=== --help documents --check ==="

run_sls -- --help
assert_eq "0" "$RC" "--help exits 0"
assert_contains "$OUT" "--check" "--help documents --check"
assert_contains "$OUT" "Report-only drift check" "--help explains what --check does"

echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
