#!/usr/bin/env bash
# test-install-sync-labels.sh - Unit tests for the source-only
# scripts/install/sync-labels.sh's --prune-defaults / --force flags (#5066).
#
# scripts/install/sync-labels.sh is the pre-install/bootstrap counterpart of
# defaults/scripts/sync-labels.sh (used before .loom/ exists on a target, and
# from a checked-out loom source tree) — it has its own DEFAULT_LABELS
# deletion loop, independently maintained from the installed-tree copy
# (whose own coverage lives in test-sync-labels-repo-flag.sh). #5066 made
# default-label deletion opt-in on BOTH copies; this suite proves that this
# copy holds the same invariants:
#   1. A bare run creates/updates Loom labels and deletes nothing.
#   2. --prune-defaults restores the pre-#5066 unconditional deletion.
#   3. A default label still attached to an issue/PR is skipped (warned, with
#      the affected numbers) unless --force is also given.
#
# This is a black-box test: the script is a full CLI, so we stub `gh` on
# PATH, run the real script as a subprocess against a scratch git repo (a
# real git remote is required — unlike the installed-tree copy, this script
# has no --repo override to bypass remote-based resolution), and assert on
# exit codes, stdout/stderr, and the recorded `gh` argv log.
#
# Usage:
#   ./defaults/scripts/tests/test-install-sync-labels.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$TEST_DIR/../../.." && pwd)"
SLS="$REPO_ROOT/scripts/install/sync-labels.sh"

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
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
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
    if ! printf '%s' "$haystack" | grep -qF -- "$needle"; then
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
# Same contract as test-sync-labels-repo-flag.sh's stub:
#   gh label list ...           -> exit 0 (script takes the `create` branch)
#   gh label create|edit|delete -> exit 0
#   gh api repos/.../issues -f labels=<L> ... -> echoes
#       $LOOM_TEST_GH_USAGE_NUMBERS (one number per line) when <L> matches
#       $LOOM_TEST_GH_USAGE_LABEL, otherwise empty (label not in use).
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${LOOM_TEST_GH_LOG:?stub gh: LOOM_TEST_GH_LOG not set}"

case "$1" in
  label)
    case "$2" in
      list)   exit 0 ;;
      create|edit|delete) exit 0 ;;
    esac
    echo "stub gh: unhandled label args: $*" >&2
    exit 3
    ;;
  api)
    label=""
    prev=""
    for arg in "$@"; do
      if [[ "$prev" == "-f" && "$arg" == labels=* ]]; then
        label="${arg#labels=}"
      fi
      prev="$arg"
    done
    if [[ -n "$label" && "$label" == "${LOOM_TEST_GH_USAGE_LABEL:-}" ]]; then
      printf '%s\n' "${LOOM_TEST_GH_USAGE_NUMBERS:-}" | sed '/^$/d'
    fi
    exit 0
    ;;
esac
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

# --- Scratch git repo ---------------------------------------------------------
# Unlike the installed-tree copy, scripts/install/sync-labels.sh has no --repo
# override — it always resolves the target from `git config remote.origin.url`
# via forge-detect.sh's detect_forge_and_repo. A real (if fake-remote) git repo
# is therefore required, not just a directory.
SRC="$TMP/src"
mkdir -p "$SRC/.github"
git -C "$TMP" init -q "$SRC" 2>/dev/null || git init -q "$SRC"
git -C "$SRC" remote add origin "https://github.com/octocat/hello-world.git"
cat > "$SRC/.github/labels.yml" <<'EOF'
# BEGIN LOOM LABELS
- name: loom:issue
  description: "Approved and ready for a Builder"
  color: "3B82F6"
- name: loom:pr
  description: "Approved pull request"
  color: "10B981"
# END LOOM LABELS
EOF

GH_LOG="$TMP/gh.log"

# run_sls [--] <script args...>
# Sets: RC, OUT (merged stdout+stderr), LOG (recorded gh argv lines).
RC=0
OUT=""
LOG=""
run_sls() {
    : > "$GH_LOG"
    OUT="$(
        PATH="$STUB_DIR:$PATH" \
        LOOM_TEST_GH_LOG="$GH_LOG" \
        LOOM_TEST_GH_USAGE_LABEL="${LOOM_TEST_GH_USAGE_LABEL:-}" \
        LOOM_TEST_GH_USAGE_NUMBERS="${LOOM_TEST_GH_USAGE_NUMBERS:-}" \
        bash "$SLS" "$@" 2>&1
    )"
    RC=$?
    LOG="$(cat "$GH_LOG")"
}

echo ""
echo "=== Additive by default: no deletion without --prune-defaults (#5066) ==="

run_sls "$SRC"
assert_eq "0" "$RC" "a bare run against the scratch repo exits 0"
assert_contains "$OUT" "Target repository: octocat/hello-world (github)" \
    "the target repo resolves from the git remote, unchanged"
assert_not_contains "$LOG" "label delete" \
    "a bare run performs no default-label deletion"
assert_contains "$OUT" "Leaving default labels untouched" \
    "a bare run explains that default labels were left alone"
assert_contains "$LOG" "label create loom:issue -R octocat/hello-world" \
    "a bare run still creates/updates the Loom labels"
assert_contains "$OUT" "Synced 2 labels" \
    "both labels.yml entries were synced"

echo ""
echo "=== --prune-defaults restores the old (pre-#5066) deletion behavior ==="

run_sls --prune-defaults "$SRC"
assert_eq "0" "$RC" "--prune-defaults run exits 0"
assert_contains "$OUT" "Pruning default labels (--prune-defaults)..." \
    "--prune-defaults announces that it is pruning"
assert_contains "$LOG" "label delete bug -R octocat/hello-world" \
    "--prune-defaults deletes GitHub's default labels"
assert_contains "$LOG" "label delete wontfix -R octocat/hello-world" \
    "--prune-defaults deletes every default label, not just the first"

echo ""
echo "=== --prune-defaults skips an in-use default label unless --force ==="

LOOM_TEST_GH_USAGE_LABEL="bug" LOOM_TEST_GH_USAGE_NUMBERS=$'1\n2' \
    run_sls --prune-defaults "$SRC"
assert_eq "0" "$RC" "an in-use default label does not fail the whole run"
assert_contains "$OUT" "Refusing to delete in-use default label 'bug'" \
    "the in-use label is refused, not silently deleted"
assert_contains "$OUT" "#1 #2" \
    "the refusal names the affected issue/PR numbers"
assert_not_contains "$LOG" "label delete bug " \
    "the in-use label is NOT deleted without --force"
assert_contains "$LOG" "label delete wontfix -R octocat/hello-world" \
    "an unrelated, unused default label is still deleted normally"

LOOM_TEST_GH_USAGE_LABEL="bug" LOOM_TEST_GH_USAGE_NUMBERS=$'1\n2' \
    run_sls --prune-defaults --force "$SRC"
assert_eq "0" "$RC" "--force run exits 0"
assert_contains "$OUT" "Deleting in-use default label 'bug' (--force)" \
    "--force warns before deleting an in-use label"
assert_contains "$LOG" "label delete bug -R octocat/hello-world" \
    "--force actually deletes the in-use label"

echo ""
echo "=== --prune-defaults / --force are no-ops without the other flag combos ==="

run_sls --force "$SRC"
assert_eq "0" "$RC" "--force without --prune-defaults exits 0"
assert_not_contains "$LOG" "label delete" \
    "--force alone (no --prune-defaults) still performs no deletion"

echo ""
echo "=== Argument parsing ==="

run_sls --bogus "$SRC"
assert_eq "2" "$RC" "unknown option exits 2"
assert_contains "$OUT" "Unknown option: --bogus" "unknown option is named"

run_sls "$SRC" extra
assert_eq "2" "$RC" "a second positional argument exits 2"
assert_contains "$OUT" "Unexpected extra argument: extra" \
    "the extra positional is named"

echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
