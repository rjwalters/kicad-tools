#!/usr/bin/env bash
# test-version-check-gate.sh - Unit tests for version-check-gate.sh, the
# shared version-bearing-file sync gate (#6730, #7168).
#
# This suite covers the gate script DIRECTLY (not through create-pr.sh --
# that composition is already covered by test-create-pr-version-check.sh).
# The direct coverage matters because #7168's second call site --
# Doctor's merge-conflict rebase recipes in defaults/roles/doctor.md -- calls
# version-check-gate.sh on its own, never through create-pr.sh, to catch a
# rebase that silently absorbs a stale version-bearing file value (no git
# conflict is raised for a file the branch's own commits never touched) before
# the recipe's `git push --force-with-lease` completes.
#
# T1-T4 exercise the gate's own logic (abort on mismatch, pass on clean, skip
# when no version.sh is resolvable, explicit override) via a stubbed
# LOOM_VERSION_CHECK_SCRIPT, hermetic against this checkout's own ambient
# version-file sync state. T5-T7 run the REAL scripts/version.sh (copied
# verbatim, never modified by this suite) against a from-scratch fixture
# "repo" -- mirroring test-create-pr-version-check.sh's T5-T7 -- to verify a
# hand-edited .loom/install-metadata.json is caught, a correctly bumped set is
# not, and a missing install-metadata.json is not treated as a failure. T8
# checks the --fix-hint flag is honored in the printed message.
#
# Usage:
#   ./.loom/scripts/tests/test-version-check-gate.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$(cd "$SCRIPT_DIR/.." && pwd)/version-check-gate.sh"
REAL_VERSION_SCRIPT="$(cd "$SCRIPT_DIR/../../.." && pwd)/scripts/version.sh"

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
  if [[ "$haystack" == *"$needle"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $msg"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $msg"
    echo "    Looking for: '$needle'"
    echo "    In output:   '$haystack'"
  fi
}

if [[ ! -x "$GATE" ]]; then
  echo "ERROR: $GATE is not executable" >&2
  exit 1
fi
if [[ ! -f "$REAL_VERSION_SCRIPT" ]]; then
  echo "ERROR: $REAL_VERSION_SCRIPT not found" >&2
  exit 1
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT

run_gate() {
  set +e
  OUTPUT=$("$GATE" "$@" 2>&1)
  EXIT_CODE=$?
  set -e
}

run_gate_in() {
  local dir="$1"
  shift
  local outfile exitfile
  outfile="$(mktemp)"
  exitfile="$(mktemp)"
  (
    cd "$dir"
    set +e
    "$GATE" "$@" > "$outfile" 2>&1
    echo "$?" > "$exitfile"
  )
  OUTPUT="$(cat "$outfile")"
  EXIT_CODE="$(cat "$exitfile")"
  rm -f "$outfile" "$exitfile"
}

echo "Testing version-check-gate.sh (#6730, #7168)..."
echo ""

# === T1-T4: the gate's own logic, via a stubbed version script ===

# T1: stub reports a MISMATCH (nonzero exit) -> gate aborts, BLOCKER message.
cat > "$STUB_DIR/version-mismatch.sh" <<'STUB'
#!/usr/bin/env bash
echo "MISMATCH  .loom/install-metadata.json: 0.18.130 (expected 0.18.131)"
exit 1
STUB
chmod +x "$STUB_DIR/version-mismatch.sh"
LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-mismatch.sh" run_gate
assert_eq "1" "$EXIT_CODE" "version.sh check reports a mismatch -> non-zero exit"
assert_contains "$OUTPUT" "BLOCKER" "Abort message uses the BLOCKER: prefix"
assert_contains "$OUTPUT" "Fix:" "Abort message uses the Fix: prefix"
assert_contains "$OUTPUT" "MISMATCH" "Abort message surfaces the underlying MISMATCH line"

# T2: stub reports all-clean (zero exit) -> gate exits 0.
cat > "$STUB_DIR/version-ok.sh" <<'STUB'
#!/usr/bin/env bash
echo "All versions in sync: 0.18.131"
exit 0
STUB
chmod +x "$STUB_DIR/version-ok.sh"
LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-ok.sh" run_gate
assert_eq "0" "$EXIT_CODE" "version.sh check reports clean -> exits 0"

# T3: LOOM_VERSION_CHECK_SCRIPT unset, auto-detection resolves to a scratch
# git repo with NO scripts/version.sh (simulates a non-dogfooded consumer
# checkout) -> skipped outright, not a failure.
NO_SCRIPT_REPO="$(mktemp -d)"
(cd "$NO_SCRIPT_REPO" && git init -q)
unset LOOM_VERSION_CHECK_SCRIPT
run_gate_in "$NO_SCRIPT_REPO"
assert_eq "0" "$EXIT_CODE" "No scripts/version.sh resolvable (consumer repo) -> not a failure, exits 0"
rm -rf "$NO_SCRIPT_REPO"

# T4: explicit LOOM_VERSION_CHECK_SCRIPT override is honored even when run
# from inside this real (dogfooded) checkout.
LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-mismatch.sh" run_gate
assert_eq "1" "$EXIT_CODE" "Explicit LOOM_VERSION_CHECK_SCRIPT override is honored over auto-detection"

# === T5-T7: the REAL scripts/version.sh against a from-scratch fixture repo
# -- proves the Doctor-rebase call site (real version.sh, no create-pr.sh in
# the loop) works end to end. Mirrors test-create-pr-version-check.sh T5-T7.

make_fixture_repo() {
  local dir="$1" version="$2" meta_version="${3-__omit__}"
  mkdir -p "$dir/mcp-loom" "$dir/loom-daemon" "$dir/loom-api" "$dir/scripts" "$dir/.loom"
  cp "$REAL_VERSION_SCRIPT" "$dir/scripts/version.sh"
  chmod +x "$dir/scripts/version.sh"

  printf '{"version": "%s"}\n' "$version" > "$dir/package.json"
  printf '{"version": "%s"}\n' "$version" > "$dir/mcp-loom/package.json"
  printf '[package]\nname = "loom-daemon"\nversion = "%s"\n' "$version" > "$dir/loom-daemon/Cargo.toml"
  printf '[package]\nname = "loom-api"\nversion = "%s"\n' "$version" > "$dir/loom-api/Cargo.toml"
  printf '**Loom Version**: %s\n' "$version" > "$dir/CLAUDE.md"
  printf '%s\n' "$version" > "$dir/VERSION"
  cat > "$dir/Cargo.lock" <<EOF
[[package]]
name = "loom-api"
version = "$version"
dependencies = []

[[package]]
name = "loom-daemon"
version = "$version"
dependencies = []
EOF
  cat > "$dir/mcp-loom/package-lock.json" <<EOF
{
  "name": "mcp-loom",
  "version": "$version",
  "packages": {
    "": {
      "version": "$version"
    }
  }
}
EOF
  if [[ "$meta_version" != "__omit__" ]]; then
    printf '{"loom_version": "%s"}\n' "$meta_version" > "$dir/.loom/install-metadata.json"
  fi
  (cd "$dir" && git init -q)
}

# T5: package.json (and every other VERSION_FILES entry) says 1.2.3, but
# .loom/install-metadata.json was left at the OLD version -- the exact
# incident shape from #6497/#6212/#7168 (a rebase that silently absorbed
# main's version bump into everything EXCEPT install-metadata.json, which the
# branch's own commits never touched so git raised no conflict on it).
FIXTURE5="$(mktemp -d)"
make_fixture_repo "$FIXTURE5" "1.2.3" "1.2.2"
unset LOOM_VERSION_CHECK_SCRIPT
run_gate_in "$FIXTURE5" --fix-hint "then push."
assert_eq "1" "$EXIT_CODE" "Hand-edited/rebase-stale .loom/install-metadata.json (real version.sh) -> abort"
assert_contains "$OUTPUT" "install-metadata.json" "Real mismatch output names install-metadata.json"
assert_contains "$OUTPUT" "1.2.2" "Real mismatch output shows the stale actual value"
assert_contains "$OUTPUT" "BLOCKER" "Real-version.sh abort still uses the BLOCKER: message"
assert_contains "$OUTPUT" "then push." "Custom --fix-hint text is appended to the Fix: message"
rm -rf "$FIXTURE5"

# T6: every version-bearing file, INCLUDING .loom/install-metadata.json, is
# in sync -> no false block.
FIXTURE6="$(mktemp -d)"
make_fixture_repo "$FIXTURE6" "1.2.3" "1.2.3"
unset LOOM_VERSION_CHECK_SCRIPT
run_gate_in "$FIXTURE6"
assert_eq "0" "$EXIT_CODE" "In-sync fixture (real version.sh) -> exits 0"
rm -rf "$FIXTURE6"

# T7: .loom/install-metadata.json does not exist at all (non-dogfooded
# checkout shape) -- every OTHER version-bearing file matches. Must not be
# treated as a failure (version.sh check itself guards this with `-f`).
FIXTURE7="$(mktemp -d)"
make_fixture_repo "$FIXTURE7" "1.2.3"
unset LOOM_VERSION_CHECK_SCRIPT
run_gate_in "$FIXTURE7"
assert_eq "0" "$EXIT_CODE" "Missing .loom/install-metadata.json (real version.sh) -> not a failure, exits 0"
rm -rf "$FIXTURE7"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
