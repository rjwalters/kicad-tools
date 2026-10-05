#!/usr/bin/env bash
# test-create-pr-review-gate.sh - Unit tests for create-pr.sh's 1:1
# issue-to-PR review-gate guard (#9453 phase 5).
#
# Adopt-first (#6074, tested in test-create-pr-superseded-issue.sh's T6) only
# catches a race when both builders share a head branch. This guard catches
# the race adopt-first cannot: two builders racing the SAME issue on
# DIFFERENT branches. Immediately before creating a brand-new PR,
# create-pr.sh re-runs `loom-daemon forge check-open-pr` on the issue this
# PR's own body references (a closing keyword OR `Part of` / `Contributes
# to`) and refuses (exit 6) when that probe finds an open linked PR on a
# DIFFERENT head branch than ours.
#
# Strategy: like test-create-pr-superseded-issue.sh, run create-pr.sh
# directly as a subprocess with a stub `gh` on PATH and LOOM_FORGE_TYPE
# forced to github. The daemon side of the guard (`forge check-open-pr`) is
# stubbed via $LOOM_DAEMON_SELF_BIN -- the same seam
# test-create-pr-provenance.sh's daemon-ok/daemon-old fixtures use -- so this
# suite pins create-pr.sh's OWN logic (parsing, exit code, message, fail-open
# posture) without depending on a real daemon build or live network calls.
#
# Usage:
#   ./.loom/scripts/tests/test-create-pr-review-gate.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREATE_PR="$(cd "$SCRIPT_DIR/.." && pwd)/create-pr.sh"

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

# --- Stub gh on PATH ---------------------------------------------------------
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh for test-create-pr-review-gate.sh.
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
echo "$*" >> "$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "pr" && "$2" == "list" ]]; then
  # Adopt-first lookup: no existing open PR for OUR head branch by default.
  cat "$STUB_DIR_FROM_ENV/adopt-url.txt" 2>/dev/null || true
  exit 0
fi

if [[ "$1" == "issue" && "$2" == "view" ]]; then
  # Only reached for a body with a closing keyword (the #6277 check, which
  # runs AFTER this guard) -- always report OPEN so it never interferes.
  echo "OPEN"
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "view" ]]; then
  pr_num="$3"
  cat "$STUB_DIR_FROM_ENV/pr-$pr_num-head.txt" 2>/dev/null || true
  exit 0
fi

if [[ "$1" == "api" && "$2" == "repos/owner/repo" ]]; then
  # The #9548 write-scope permission probe for the registered fixture repo.
  echo '{"push":true}'
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

# --- Stub daemon (forge check-open-pr) --------------------------------------
cat > "$STUB_DIR/daemon" <<'STUB'
#!/usr/bin/env bash
# Stub loom-daemon for test-create-pr-review-gate.sh. Answers
# `forge check-open-pr <issue>`, driven by per-issue fixture files (defaults
# to "verified no open PR", exit 1, matching
# loom-daemon/src/forge_check_open_pr.rs's contract when no fixture is
# present), and logs ONLY that call to daemon-forge-calls.log so the
# review-gate guard's own consultation can be asserted in isolation from
# create-pr.sh's UNRELATED daemon call for `provenance pr-marker` (#9027),
# which every successful "pr create" in this suite also triggers.
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub daemon: LOOM_TEST_STUB_DIR not set}"

if [[ "$1" == "forge" && "$2" == "check-open-pr" ]]; then
  echo "$*" >> "$STUB_DIR_FROM_ENV/daemon-forge-calls.log"
  issue="$3"
  rc=1
  [[ -f "$STUB_DIR_FROM_ENV/opr-$issue-rc.txt" ]] && rc="$(cat "$STUB_DIR_FROM_ENV/opr-$issue-rc.txt")"
  [[ -f "$STUB_DIR_FROM_ENV/opr-$issue-stdout.txt" ]] && cat "$STUB_DIR_FROM_ENV/opr-$issue-stdout.txt"
  exit "$rc"
fi

# Anything else (in practice, `provenance pr-marker`) is not this guard's
# concern -- respond like a daemon predating the subcommand so create-pr.sh's
# own all-unknown fallback kicks in, without failing the whole script.
echo "error: unrecognized subcommand" >&2
exit 2
STUB
chmod +x "$STUB_DIR/daemon"

cat > "$STUB_DIR/version-check-ok.sh" <<'STUB'
#!/usr/bin/env bash
echo "OK        (stub): all versions in sync"
exit 0
STUB
chmod +x "$STUB_DIR/version-check-ok.sh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export LOOM_FORGE_TYPE=github
export LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-check-ok.sh"
export LOOM_DAEMON_SELF_BIN="$STUB_DIR/daemon"

# create-pr.sh vets its target through the #9548 write scope (loom_write_repo
# -> `loom-daemon forge may-write`) before it opens anything. The fixture is
# registered as a repository this installation may write to, so the real
# decision admits it: a Loom-installed checkout (.loom/) whose only remote,
# origin, is owner/repo, with the stub gh above answering the permission probe
# with push. Without a daemon, the shell fallback admits it for the same
# reason (origin is the only remote). The probe cache stays in the fixture.
FIXTURE_REPO="$STUB_DIR/repo"
git init -q "$FIXTURE_REPO"
git -C "$FIXTURE_REPO" remote add origin https://github.com/owner/repo.git
mkdir -p "$FIXTURE_REPO/.loom"
export LOOM_GH_BIN="$STUB_DIR/gh"
export LOOM_GH_NO_POLICY_LAUNCHER=1  # no host egress-policy launcher over the stub (#9995)
export LOOM_WRITE_SCOPE_CACHE_DIR="$STUB_DIR/write-scope-cache"
unset GH_REPO LOOM_REPO

reset_fixtures() {
  : > "$STUB_DIR/gh-calls.log"
  : > "$STUB_DIR/daemon-forge-calls.log"
  : > "$STUB_DIR/created.log"
  rm -f "$STUB_DIR"/opr-*.txt "$STUB_DIR"/pr-*.txt "$STUB_DIR/adopt-url.txt"
}

run_create_pr() {
  set +e
  OUTPUT=$(cd "$FIXTURE_REPO" && "$CREATE_PR" "$@" 2>&1)
  EXIT_CODE=$?
  set -e
}

created_count() {
  if [[ -f "$STUB_DIR/created.log" ]]; then
    grep -c "CREATED" "$STUB_DIR/created.log" 2>/dev/null || true
  else
    echo 0
  fi
}

echo "Testing create-pr.sh 1:1 issue-to-PR review-gate guard (#9453 phase 5)..."
echo ""

# T1: `Part of #N` referencing an issue with an open linked PR on a
# DIFFERENT head branch -> refuse (exit 6), name the existing PR + branch,
# no PR created.
reset_fixtures
echo "0" > "$STUB_DIR/opr-200-rc.txt"
echo "555" > "$STUB_DIR/opr-200-stdout.txt"
echo "feature/issue-200-other" > "$STUB_DIR/pr-555-head.txt"
run_create_pr --title "feat: slice" --body "Part of #200" --head "feature/issue-200"
assert_eq "6" "$EXIT_CODE" "Part of #200 with a rival open PR on another branch -> exit 6"
assert_contains "$OUTPUT" "#200" "Refusal names the target issue"
assert_contains "$OUTPUT" "#555" "Refusal names the existing PR"
assert_contains "$OUTPUT" "feature/issue-200-other" "Refusal names the existing PR's branch"
assert_contains "$OUTPUT" "Stand down" "Refusal advises the losing builder to stand down"
assert_eq "0" "$(created_count)" "No PR was created"

# T2: `Closes #N` (a closing keyword) with a rival open PR on a different
# branch -> also refused; the 1:1 gate is not limited to partial references.
reset_fixtures
echo "0" > "$STUB_DIR/opr-201-rc.txt"
echo "556" > "$STUB_DIR/opr-201-stdout.txt"
echo "feature/issue-201-alt" > "$STUB_DIR/pr-556-head.txt"
run_create_pr --title "fix: something" --body "Closes #201" --head "feature/issue-201"
assert_eq "6" "$EXIT_CODE" "Closes #201 with a rival open PR on another branch -> exit 6"
assert_contains "$OUTPUT" "#556" "Refusal names the existing PR (closing-keyword body)"

# T3: the daemon reports an open PR whose head IS our own branch (the guard's
# own branch-comparison must not fire here -- adopt-first is what normally
# catches this case; this pins the guard's comparison in isolation).
reset_fixtures
echo "0" > "$STUB_DIR/opr-202-rc.txt"
echo "557" > "$STUB_DIR/opr-202-stdout.txt"
echo "feature/issue-202" > "$STUB_DIR/pr-557-head.txt"
run_create_pr --title "feat: slice" --body "Part of #202" --head "feature/issue-202"
assert_eq "0" "$EXIT_CODE" "Rival PR's head equals our own head branch -> not a collision, exits 0"
assert_eq "1" "$(created_count)" "Same-branch case -> PR is still created"

# T4: verified no open PR (daemon exit 1, the default) -> proceeds to create.
reset_fixtures
run_create_pr --title "feat: slice" --body "Part of #203" --head "feature/issue-203"
assert_eq "0" "$EXIT_CODE" "No rival open PR -> exits 0"
assert_eq "1" "$(created_count)" "No rival open PR -> PR is still created"

# T5: probe failed (daemon exit 5) -> fail OPEN, never fatal.
reset_fixtures
echo "5" > "$STUB_DIR/opr-204-rc.txt"
run_create_pr --title "feat: slice" --body "Part of #204" --head "feature/issue-204"
assert_eq "0" "$EXIT_CODE" "Probe failure (exit 5) -> fails open, exits 0"
assert_eq "1" "$(created_count)" "Probe failure -> PR is still created (fail-open)"

# T6: a daemon predating `forge check-open-pr` (unrecognized subcommand,
# non-0/1/3/5 exit) -> also fails open, never fatal.
reset_fixtures
echo "2" > "$STUB_DIR/opr-205-rc.txt"
run_create_pr --title "feat: slice" --body "Part of #205" --head "feature/issue-205"
assert_eq "0" "$EXIT_CODE" "Old daemon (unrecognized subcommand) -> fails open, exits 0"
assert_eq "1" "$(created_count)" "Old daemon -> PR is still created (fail-open)"

# T7: no issue reference in the body at all -> the guard never runs (no
# daemon call for the review-gate leg).
reset_fixtures
run_create_pr --title "docs: update readme" --body "Just a docs tweak." --head "feature/misc"
assert_eq "0" "$EXIT_CODE" "No issue reference -> no review-gate check, exits 0"
assert_eq "1" "$(created_count)" "No issue reference -> PR is still created"
assert_eq "" "$(cat "$STUB_DIR/daemon-forge-calls.log" 2>/dev/null || true)" "No issue reference -> forge check-open-pr never consulted"

# T8: same-head-branch adoption (#6074) takes precedence -- when an open PR
# already exists for OUR OWN head branch, adopt-first returns before the
# review-gate guard's daemon call ever runs.
reset_fixtures
echo "https://github.com/owner/repo/pull/9000" > "$STUB_DIR/adopt-url.txt"
run_create_pr --title "feat: slice" --body "Part of #206" --head "feature/issue-206"
assert_eq "0" "$EXIT_CODE" "Own branch already has an open PR -> adopts, exits 0"
assert_contains "$OUTPUT" "9000" "Adopts the existing PR for our own branch"
assert_eq "0" "$(created_count)" "Adoption -> no new PR created"
assert_eq "" "$(cat "$STUB_DIR/daemon-forge-calls.log" 2>/dev/null || true)" "Adoption short-circuits before the review-gate daemon call"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
