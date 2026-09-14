#!/usr/bin/env bash
# test-create-pr-version-check.sh - Unit tests for create-pr.sh's automatic
# `scripts/version.sh check` pre-flight gate (#6730).
#
# Recurring failure mode this closes: a Builder's version-bump commit hand-
# edits (or mirrors via a partial script) the VERSION_FILES set but leaves a
# separate version-bearing file -- in practice always
# .loom/install-metadata.json -- stale. `scripts/version.sh check` already
# catches this; the gap was that nothing forced it to run before a PR was
# opened (builder-pr.md documented it as a manual checklist step a Builder
# could forget). create-pr.sh now runs the check itself, unconditionally,
# before creating/adopting a PR, and aborts with a BLOCKER:/Fix: message pair
# mirroring builder-pr.md's own "defaults/ VERSION-Bump Gate" style.
#
# Two layers of coverage:
#   - T1-T4 exercise create-pr.sh's OWN gating logic (abort on nonzero exit,
#     proceed on zero exit, skip when no version script is resolvable) using
#     a stubbed `LOOM_VERSION_CHECK_SCRIPT` -- so this suite stays hermetic
#     against this checkout's own ambient version-file sync state, same
#     concern noted in test-create-pr-superseded-issue.sh.
#   - T5-T7 run the REAL scripts/version.sh (copied verbatim, never modified
#     by this suite -- #6730 explicitly does not touch version.sh) against a
#     from-scratch fixture "repo" to verify the composed behavior end to end:
#     a hand-edited version-bearing file aborts create-pr.sh with the
#     mismatch reported (issue's first test-plan bullet), a correctly bumped
#     set proceeds with no false block (second bullet), and a fixture with no
#     .loom/install-metadata.json at all is not failed by the new check
#     (third bullet, mirrors version.sh check's own `-f` guard).
#
# Strategy: same as test-create-pr-superseded-issue.sh -- run create-pr.sh
# directly as a subprocess with a stub `gh` on PATH and LOOM_FORGE_TYPE=github
# forced. --head is always passed explicitly so the script never needs a real
# git checkout for branch detection; T5-T7 additionally `git init` a scratch
# fixture directory so `git rev-parse --show-toplevel` (create-pr.sh's own
# auto-detection path, exercised when LOOM_VERSION_CHECK_SCRIPT is unset)
# resolves inside the fixture instead of this real checkout.
#
# Usage:
#   ./.loom/scripts/tests/test-create-pr-version-check.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREATE_PR="$(cd "$SCRIPT_DIR/.." && pwd)/create-pr.sh"
REAL_VERSION_SCRIPT="$(cd "$SCRIPT_DIR/../../.." && pwd)/scripts/version.sh"

# Colors
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

if [[ ! -x "$CREATE_PR" ]]; then
  echo "ERROR: $CREATE_PR is not executable" >&2
  exit 1
fi
if [[ ! -f "$REAL_VERSION_SCRIPT" ]]; then
  echo "ERROR: $REAL_VERSION_SCRIPT not found" >&2
  exit 1
fi

# --- Stub gh on PATH (adopt-first and issue-freshness checks are not under
# test here -- always report "no existing PR" and succeed on create) ---
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
echo "$*" >> "$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "pr" && "$2" == "list" ]]; then
  # No existing open PR -- always fall through to create.
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "create" ]]; then
  echo "CREATED" >> "$STUB_DIR_FROM_ENV/created.log"
  echo "https://github.com/owner/repo/pull/9999"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export LOOM_FORGE_TYPE=github

created_count() {
  if [[ -f "$STUB_DIR/created.log" ]]; then
    grep -c "CREATED" "$STUB_DIR/created.log" 2>/dev/null || true
  else
    echo 0
  fi
}

run_create_pr() {
  : > "$STUB_DIR/created.log"
  set +e
  OUTPUT=$("$CREATE_PR" "$@" 2>&1)
  EXIT_CODE=$?
  set -e
}

# run_create_pr_in <dir> [create-pr.sh args...] -- same as run_create_pr, but
# runs create-pr.sh with its CWD set to <dir> (so `git rev-parse
# --show-toplevel`, create-pr.sh's own auto-detection path, resolves inside a
# scratch fixture instead of this real checkout). Uses a subshell for the cd
# so the caller's CWD is never disturbed, and writes the exit status to a
# temp file so it survives the subshell boundary (a bare $? after `(...)`
# would only ever reflect the subshell's own last command, not
# create-pr.sh's).
run_create_pr_in() {
  local dir="$1"
  shift
  : > "$STUB_DIR/created.log"
  local outfile exitfile
  outfile="$(mktemp)"
  exitfile="$(mktemp)"
  (
    cd "$dir"
    set +e
    "$CREATE_PR" "$@" > "$outfile" 2>&1
    echo "$?" > "$exitfile"
  )
  OUTPUT="$(cat "$outfile")"
  EXIT_CODE="$(cat "$exitfile")"
  rm -f "$outfile" "$exitfile"
}

echo "Testing create-pr.sh automatic version.sh check gate (#6730)..."
echo ""

# === T1-T4: create-pr.sh's own gating logic, via a stubbed version script ===

# T1: stub reports a MISMATCH (nonzero exit) -> create-pr.sh aborts, BLOCKER
# message printed, no PR created.
cat > "$STUB_DIR/version-mismatch.sh" <<'STUB'
#!/usr/bin/env bash
echo "MISMATCH  .loom/install-metadata.json: 0.18.130 (expected 0.18.131)"
exit 1
STUB
chmod +x "$STUB_DIR/version-mismatch.sh"
LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-mismatch.sh" \
  run_create_pr --title "fix: something" --body "Closes #200" --head "feature/issue-200"
assert_eq "1" "$EXIT_CODE" "version.sh check reports a mismatch -> non-zero exit"
assert_contains "$OUTPUT" "BLOCKER" "Abort message uses the BLOCKER: prefix (mirrors builder-pr.md gate)"
assert_contains "$OUTPUT" "Fix:" "Abort message uses the Fix: prefix (mirrors builder-pr.md gate)"
assert_contains "$OUTPUT" "MISMATCH" "Abort message surfaces the underlying MISMATCH line"
assert_eq "0" "$(created_count)" "No PR was created on a version mismatch"

# T2: stub reports all-clean (zero exit) -> create-pr.sh proceeds normally.
cat > "$STUB_DIR/version-ok.sh" <<'STUB'
#!/usr/bin/env bash
echo "All versions in sync: 0.18.131"
exit 0
STUB
chmod +x "$STUB_DIR/version-ok.sh"
LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-ok.sh" \
  run_create_pr --title "fix: something" --body "Closes #201" --head "feature/issue-201"
assert_eq "0" "$EXIT_CODE" "version.sh check reports clean -> exits 0"
assert_eq "1" "$(created_count)" "Clean version check -> PR IS created (no false block)"

# T3: LOOM_VERSION_CHECK_SCRIPT explicitly empty and auto-detection resolves
# to a scratch git repo with NO scripts/version.sh at all (simulates a
# non-dogfooded consumer checkout, where the script is never installed) ->
# the check is skipped outright, not treated as a failure.
NO_SCRIPT_REPO="$(mktemp -d)"
(cd "$NO_SCRIPT_REPO" && git init -q)
unset LOOM_VERSION_CHECK_SCRIPT
run_create_pr_in "$NO_SCRIPT_REPO" --title "fix: something" --body "Closes #202" --head "feature/issue-202"
assert_eq "0" "$EXIT_CODE" "No scripts/version.sh resolvable (consumer repo) -> not a failure, exits 0"
assert_eq "1" "$(created_count)" "No scripts/version.sh resolvable -> PR is still created"
rm -rf "$NO_SCRIPT_REPO"

# T4: explicit LOOM_VERSION_CHECK_SCRIPT override takes precedence over
# auto-detection, even when run from inside this real (dogfooded) checkout.
LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-mismatch.sh" \
  run_create_pr --title "fix: something" --body "Closes #203" --head "feature/issue-203"
assert_eq "1" "$EXIT_CODE" "Explicit LOOM_VERSION_CHECK_SCRIPT override is honored over auto-detection"

# === T5-T7: the REAL scripts/version.sh against a from-scratch fixture repo,
# proving create-pr.sh + version.sh compose correctly end to end. ===
#
# version.sh's `check` subcommand only reads files (package.json,
# mcp-loom/package.json, the two Cargo.tomls, CLAUDE.md, VERSION, Cargo.lock,
# mcp-loom/package-lock.json, and .loom/install-metadata.json when present)
# -- no cargo/npm invocation is needed to exercise it, unlike `bump`/`set`.

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
  (
    cd "$dir"
    git init -q
    git config user.email "test@example.com"
    git config user.name "Test"
    # version-check-gate.sh (#7417) now also fails on a version-bearing file
    # that's modified/untracked relative to git HEAD (a bump that was never
    # committed) -- committing the fixture's baseline here keeps these tests
    # focused on version.sh check's own file-vs-file comparison, not on that
    # separate uncommitted-bump check (covered by test-version-check-gate.sh's
    # T10/T11 instead).
    git add -A
    git commit -q -m "base at $version"
  )
}

# T5: package.json says 1.2.3 (the "expected" version); every VERSION_FILES
# entry correctly matches it, but .loom/install-metadata.json was left at the
# OLD version -- the exact incident shape from #6497/#6212 (a hand-edited
# metadata file, everything else bumped correctly). create-pr.sh must abort
# and surface the MISMATCH line for install-metadata.json specifically.
FIXTURE5="$(mktemp -d)"
make_fixture_repo "$FIXTURE5" "1.2.3" "1.2.2"
unset LOOM_VERSION_CHECK_SCRIPT
run_create_pr_in "$FIXTURE5" --title "fix: something" --body "Closes #204" --head "feature/issue-204"
assert_eq "1" "$EXIT_CODE" "Hand-edited .loom/install-metadata.json (real version.sh) -> abort"
assert_contains "$OUTPUT" "install-metadata.json" "Real mismatch output names install-metadata.json"
assert_contains "$OUTPUT" "1.2.2" "Real mismatch output shows the stale actual value"
assert_contains "$OUTPUT" "BLOCKER" "Real-version.sh abort still uses create-pr.sh's BLOCKER: message"
assert_eq "0" "$(created_count)" "Hand-edited metadata file -> no PR created"
rm -rf "$FIXTURE5"

# T6: every version-bearing file, INCLUDING .loom/install-metadata.json, was
# updated together (as `./scripts/version.sh bump`/`set` does) -> no false
# block, PR proceeds normally.
FIXTURE6="$(mktemp -d)"
make_fixture_repo "$FIXTURE6" "1.2.3" "1.2.3"
unset LOOM_VERSION_CHECK_SCRIPT
run_create_pr_in "$FIXTURE6" --title "fix: something" --body "Closes #205" --head "feature/issue-205"
assert_eq "0" "$EXIT_CODE" "version.sh bump/set-shaped fixture (real version.sh) -> exits 0"
assert_eq "1" "$(created_count)" "Correctly bumped fixture -> PR is created (no false block)"
rm -rf "$FIXTURE6"

# T7: .loom/install-metadata.json does not exist at all (non-dogfooded
# checkout shape) -- every OTHER version-bearing file matches. version.sh's
# own `check` guards this with `if [ -f "$INSTALL_METADATA_FILE" ]`; the new
# create-pr.sh gate must not turn that into a failure.
FIXTURE7="$(mktemp -d)"
make_fixture_repo "$FIXTURE7" "1.2.3"
unset LOOM_VERSION_CHECK_SCRIPT
run_create_pr_in "$FIXTURE7" --title "fix: something" --body "Closes #206" --head "feature/issue-206"
assert_eq "0" "$EXIT_CODE" "Missing .loom/install-metadata.json (real version.sh) -> not a failure, exits 0"
assert_eq "1" "$(created_count)" "Missing install-metadata.json -> PR is still created"
rm -rf "$FIXTURE7"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
