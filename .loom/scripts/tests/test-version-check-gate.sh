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
# not, and a missing install-metadata.json is not treated as a failure
# (T5's fixture also exercises the --fix-hint flag being honored in the
# printed message).
#
# T8-T9 (#7351) run an actual `git rebase origin/main` -- not a hand-edited
# fixture -- through the exact recipe defaults/.claude/commands/loom/judge.md
# executes for a DIRTY PR: `git rebase origin/main` then this gate. #7351
# investigated a report that this drops a version-bearing edit via a git
# 3-way-merge "adjacent line" heuristic; the repro instead isolated the real
# mechanism as this repo's own `.loom/install-metadata.json merge=ours`
# .gitattributes driver (#4528, deliberately "always keep our side" for this
# machine-local install stamp) -- during a rebase "ours" is the upstream side
# being rebased onto, so if upstream ALSO touched the file, the driver
# unconditionally discards the replayed commit's edit to it (any line, not
# just an adjacent one -- see the issue for the isolating variants tried).
# git's rebase then finds the resulting diff empty and silently drops the
# commit ("dropping <sha> ... -- patch contents already upstream"), with no
# conflict ever raised. T8 reproduces this end to end and confirms the gate
# (already wired into judge.md's DIRTY-rebase recipe by #7344/bfb096f2) does
# catch it today; T9 is the false-positive guard -- an ordinary rebase where
# upstream never touches install-metadata.json passes the gate cleanly.
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

# === T8-T9: a REAL `git rebase origin/main` against a two-branch fixture --
# proves the gate catches (T8) / doesn't false-positive on (T9) an actual
# rebase, not just a hand-assembled post-rebase file state (#7351).

# make_rebase_base <dir> <old_version> -- shared base commit for T8/T9: every
# VERSION_FILES entry plus .loom/install-metadata.json at $old_version, the
# real merge=ours .gitattributes driver for install-metadata.json (#4528)
# wired up exactly as install.sh/resync-installed.sh wire it into every real
# checkout, and the real scripts/version.sh + version-check-gate.sh copied in
# so the whole recipe runs unmodified.
make_rebase_base() {
  local dir="$1" version="$2"
  mkdir -p "$dir/mcp-loom" "$dir/loom-daemon" "$dir/loom-api" "$dir/scripts" "$dir/.loom"
  cp "$REAL_VERSION_SCRIPT" "$dir/scripts/version.sh"
  chmod +x "$dir/scripts/version.sh"
  cp "$GATE" "$dir/version-check-gate.sh"
  chmod +x "$dir/version-check-gate.sh"

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
  printf '{\n  "loom_version": "%s",\n  "loom_commit": "0788dcd8"\n}\n' "$version" > "$dir/.loom/install-metadata.json"
  echo '.loom/install-metadata.json merge=ours' > "$dir/.gitattributes"

  (
    cd "$dir"
    git init -q -b main
    git config user.email "test@example.com"
    git config user.name "Test"
    # Local (never-committed) config -- exactly what install.sh/resync-
    # installed.sh set on every real checkout so the merge=ours attribute
    # above actually activates (an attribute alone is inert, #4528).
    git config merge.ours.driver true
    git add -A
    git commit -q -m "base at $version"
  )
}

bump_version_files() {
  local dir="$1" old="$2" new="$3"
  sed -i.bak "s/\"version\": \"$old\"/\"version\": \"$new\"/" "$dir/package.json" "$dir/mcp-loom/package.json"
  sed -i.bak "s/version = \"$old\"/version = \"$new\"/" "$dir/loom-daemon/Cargo.toml" "$dir/loom-api/Cargo.toml"
  sed -i.bak "s/\*\*Loom Version\*\*: $old/\*\*Loom Version\*\*: $new/" "$dir/CLAUDE.md"
  printf '%s\n' "$new" > "$dir/VERSION"
  sed -i.bak "s/version = \"$old\"/version = \"$new\"/g" "$dir/Cargo.lock"
  sed -i.bak "s/\"version\": \"$old\"/\"version\": \"$new\"/g" "$dir/mcp-loom/package-lock.json"
  rm -f "$dir"/*.bak "$dir"/mcp-loom/*.bak "$dir"/loom-daemon/*.bak "$dir"/loom-api/*.bak
}

# T8: local branch bumps every VERSION_FILES entry in one commit, then a
# SEPARATE dedicated commit resyncs only .loom/install-metadata.json's
# loom_version -- the exact real-world shape (a4bb32c07d534b3a5647b1d4b92384748a98549f,
# "chore(version): resync install-metadata after rebase to 0.18.206", cited in
# #7351's incident report). Meanwhile origin/main independently resyncs only
# install-metadata.json's loom_commit field. Rebasing local onto origin/main
# should silently drop the dedicated loom_version commit via the merge=ours
# driver -- reproducing the incident -- and the gate must catch it.
OLD_V="0.18.205"
NEW_V="0.18.206"
FIXTURE8="$(mktemp -d)"
make_rebase_base "$FIXTURE8" "$OLD_V"
(
  cd "$FIXTURE8"
  git checkout -q -b local main
  bump_version_files "$FIXTURE8" "$OLD_V" "$NEW_V"
  git commit -aq -m "chore: bump VERSION to $NEW_V for defaults/ change"
  sed -i.bak "s/\"loom_version\": \"$OLD_V\"/\"loom_version\": \"$NEW_V\"/" .loom/install-metadata.json
  rm -f .loom/*.bak
  git commit -aq -m "chore(version): resync install-metadata after rebase to $NEW_V"

  git checkout -q main
  sed -i.bak 's/"loom_commit": "0788dcd8"/"loom_commit": "680bb81a"/' .loom/install-metadata.json
  rm -f .loom/*.bak
  git commit -aq -m "chore: resync installed Loom surfaces"

  git checkout -q local
  git remote add origin . 2>/dev/null || true
  git fetch -q . main:refs/remotes/origin/main
)
REBASE_OUTPUT="$(cd "$FIXTURE8" && git rebase origin/main 2>&1)"
REBASE_EXIT=$?
assert_eq "0" "$REBASE_EXIT" "T8 setup: git rebase origin/main itself reports success (no real conflict)"
assert_contains "$REBASE_OUTPUT" "already upstream" "T8 setup: rebase silently drops the install-metadata.json-only commit (merge=ours, #4528) -- reproduces #7351's incident message"
POST_REBASE_LOOM_VERSION="$(jq -r '.loom_version' "$FIXTURE8/.loom/install-metadata.json")"
assert_eq "$OLD_V" "$POST_REBASE_LOOM_VERSION" "T8 setup: post-rebase .loom/install-metadata.json still has the stale loom_version -- the drop is real, not just a log message"

run_gate_in "$FIXTURE8" --fix-hint "then push."
assert_eq "1" "$EXIT_CODE" "T8: version-check-gate.sh run immediately post-rebase (real judge.md DIRTY-PR recipe) catches the silent drop"
assert_contains "$OUTPUT" "MISMATCH" "T8: gate output includes a MISMATCH line"
assert_contains "$OUTPUT" "install-metadata.json" "T8: gate output names install-metadata.json as the mismatched file"
assert_contains "$OUTPUT" "BLOCKER" "T8: gate output still uses the BLOCKER: message on a real-rebase-produced mismatch"
rm -rf "$FIXTURE8"

# T9: false-positive guard. local branch bumps everything (VERSION_FILES +
# install-metadata.json's loom_version) in ONE commit; origin/main advances
# with a commit that never touches install-metadata.json at all (the common
# case -- most PRs' rebases hit this, not T8's shape). The rebase must
# succeed with no drop and the gate must pass cleanly.
FIXTURE9="$(mktemp -d)"
make_rebase_base "$FIXTURE9" "$OLD_V"
(
  cd "$FIXTURE9"
  git checkout -q -b local main
  bump_version_files "$FIXTURE9" "$OLD_V" "$NEW_V"
  sed -i.bak "s/\"loom_version\": \"$OLD_V\"/\"loom_version\": \"$NEW_V\"/" .loom/install-metadata.json
  rm -f .loom/*.bak
  git commit -aq -m "chore: bump version to $NEW_V (single commit, includes install-metadata.json)"

  git checkout -q main
  # A file untouched by bump_version_files/install-metadata.json, so this
  # commit is guaranteed not to collide with local's changes above.
  echo "unrelated change" > UNRELATED.txt
  git add UNRELATED.txt
  git commit -q -m "docs: unrelated change, does not touch install-metadata.json"

  git checkout -q local
  git remote add origin . 2>/dev/null || true
  git fetch -q . main:refs/remotes/origin/main
)
REBASE_OUTPUT9="$(cd "$FIXTURE9" && git rebase origin/main 2>&1)"
REBASE_EXIT9=$?
assert_eq "0" "$REBASE_EXIT9" "T9 setup: ordinary rebase (no adjacent-file collision) succeeds"
if [[ "$REBASE_OUTPUT9" == *"already upstream"* ]]; then
  TESTS_RUN=$((TESTS_RUN + 1))
  TESTS_FAILED=$((TESTS_FAILED + 1))
  echo -e "  ${RED}FAIL${NC}: T9 setup: ordinary rebase should NOT drop any commit"
else
  TESTS_RUN=$((TESTS_RUN + 1))
  TESTS_PASSED=$((TESTS_PASSED + 1))
  echo -e "  ${GREEN}PASS${NC}: T9 setup: ordinary rebase should NOT drop any commit"
fi

run_gate_in "$FIXTURE9"
assert_eq "0" "$EXIT_CODE" "T9: gate does not false-positive on an ordinary rebase with no install-metadata.json collision"
rm -rf "$FIXTURE9"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
