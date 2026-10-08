#!/usr/bin/env bash
# test-create-pr-priority-labels.sh - create-pr.sh copies the closing issues'
# priority labels (the operator star and its levels) onto the PR it opens
# (#10518), on the GraphQL path and the REST rate-limit fallback alike.
#
# Two legs:
#
#   A. Shell wiring, always run. A stub daemon answers `forge priority-labels`
#      from a fixture, so what is tested is create-pr.sh's contract with the
#      verb: append its output to --label (deduplicated), pass its warnings
#      through, warn naming the issue when the verb is unavailable, and post
#      the audit comment only once the labels landed.
#   B. The real decision, run when a built loom-daemon is available
#      (LOOM_TEST_REAL_DAEMON_BIN, else target/{debug,release}/loom-daemon at
#      the repo root): the same create-pr.sh with the real binary over a stub
#      `gh` serving per-issue labels -- multi-ref union, `Part of #N`,
#      unstarred issue, failed lookup.
#
# Usage:
#   ./.loom/scripts/tests/test-create-pr-priority-labels.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREATE_PR="$(cd "$SCRIPT_DIR/.." && pwd)/create-pr.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; shift; printf '    %s\n' "$@"; }

assert_eq() {
  if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3" "Expected: '$1'" "Actual:   '$2'"; fi
}
assert_contains() {
  if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3" "Looking for: '$2'" "In output:   '$1'"; fi
}
assert_not_contains() {
  if [[ "$1" != *"$2"* ]]; then pass "$3"; else fail "$3" "Unexpected: '$2'" "In output:  '$1'"; fi
}

[[ -x "$CREATE_PR" ]] || { echo "ERROR: $CREATE_PR is not executable" >&2; exit 1; }

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT

# --- Stub gh -------------------------------------------------------------------
#   pr list                         -> no open PR (adopt-first misses)
#   pr create                       -> argv logged; fails with create-err.txt
#                                      when present, else a URL
#   api repos/owner/repo            -> write-scope probe / default branch
#   api --method POST .../pulls     -> REST create (html_url + number)
#   api --method POST .../labels    -> stdin -> payload-labels.json
#   api repos/owner/repo/issues/N   -> issue-N.labels (a JSON array), or
#                                      HTTP 502 when that file is absent
#   api -X POST .../comments        -> the audit comment (argv logged)
#   anything else (e.g. the timeline) -> logged, exit 1
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?}"
a="$*"; echo "${a//$'\n'/ }" >> "$D/gh-calls.log"  # one line per call, even with a multi-line body
if [[ "$1" == "pr" && "$2" == "list" ]]; then exit 0; fi
if [[ "$1" == "pr" && "$2" == "create" ]]; then
  if [[ -f "$D/create-err.txt" ]]; then cat "$D/create-err.txt" >&2; exit 1; fi
  echo "https://github.com/owner/repo/pull/1111"; exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo" ]]; then
  if [[ "${3:-}" == "--jq" ]]; then echo "trunk"; else echo '{"push":true,"default_branch":"trunk"}'; fi
  exit 0
fi
if [[ "$1 $2 $3" == "api --method POST" && "$4" == "repos/owner/repo/pulls" ]]; then
  cat > /dev/null
  printf '%s\n%s\n' "https://github.com/owner/repo/pull/4242" "4242"; exit 0
fi
if [[ "$1 $2 $3" == "api --method POST" && "$4" == repos/owner/repo/issues/*/labels ]]; then
  cat > "$D/payload-labels.json"
  [[ -f "$D/labels-fail" ]] && { echo "HTTP 403: label write denied" >&2; exit 1; }
  echo '[]'; exit 0
fi
if [[ "$1 $2 $3" == "api -X POST" && "$4" == repos/owner/repo/issues/*/comments ]]; then
  echo '{}'; exit 0
fi
if [[ "$1" == "api" && "$2" =~ ^repos/owner/repo/issues/([0-9]+)$ ]]; then
  f="$D/issue-${BASH_REMATCH[1]}.labels"
  [[ -f "$f" ]] || { echo "HTTP 502: Bad Gateway" >&2; exit 1; }
  cat "$f"; exit 0
fi
exit 1
STUB
chmod +x "$STUB_DIR/gh"

# --- Stub daemon (leg A) ---------------------------------------------------------
# `forge priority-labels` prints stars.txt and stars-err.txt (stderr); with
# --audit-pr it only logs. `daemon-old` makes the verb unknown, like a binary
# predating #10518. Every other verb predates create-pr.sh's asks, so the
# shell fallbacks run (see test-create-pr-rate-limit-fallback.sh).
cat > "$STUB_DIR/daemon" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?}"
if [[ "$1 $2" == "forge priority-labels" && ! -f "$D/daemon-old" ]]; then
  echo "$*" >> "$D/daemon-calls.log"
  cat > "$D/daemon-stdin.txt"
  [[ "$*" == *--audit-pr* ]] && exit 0
  [[ -f "$D/stars-err.txt" ]] && cat "$D/stars-err.txt" >&2
  [[ -f "$D/stars.txt" ]] && cat "$D/stars.txt"
  exit 0
fi
echo "error: unrecognized subcommand" >&2
exit 2
STUB
chmod +x "$STUB_DIR/daemon"

printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB_DIR/version-check-ok.sh"
chmod +x "$STUB_DIR/version-check-ok.sh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export LOOM_FORGE_TYPE=github
export LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-check-ok.sh"
export LOOM_GH_BIN="$STUB_DIR/gh"
export LOOM_GH_NO_POLICY_LAUNCHER=1
export LOOM_WRITE_SCOPE_CACHE_DIR="$STUB_DIR/write-scope-cache"
unset GH_REPO LOOM_REPO

use_daemon() { export LOOM_DAEMON_SELF_BIN="$1" LOOM_DAEMON_BIN="$1"; }
use_daemon "$STUB_DIR/daemon"

FIXTURE_REPO="$STUB_DIR/repo"
git init -q "$FIXTURE_REPO"
git -C "$FIXTURE_REPO" remote add origin https://github.com/owner/repo.git
mkdir -p "$FIXTURE_REPO/.loom"

RATE_LIMIT_ERR="GraphQL: API rate limit already exceeded for installation ID 1"

reset_fixtures() {
  rm -f "$STUB_DIR"/gh-calls.log "$STUB_DIR"/daemon-calls.log "$STUB_DIR"/daemon-stdin.txt \
    "$STUB_DIR"/create-err.txt "$STUB_DIR"/payload-*.json "$STUB_DIR"/labels-fail \
    "$STUB_DIR"/stars.txt "$STUB_DIR"/stars-err.txt "$STUB_DIR"/daemon-old "$STUB_DIR"/issue-*.labels
  : > "$STUB_DIR/gh-calls.log"; : > "$STUB_DIR/daemon-calls.log"
}

run_create_pr() {
  set +e
  STDOUT=$(cd "$FIXTURE_REPO" && "$CREATE_PR" "$@" 2>"$STUB_DIR/stderr.log")
  EXIT_CODE=$?
  set -e
  STDERR="$(cat "$STUB_DIR/stderr.log")"
}

create_argv() { grep '^pr create' "$STUB_DIR/gh-calls.log" || true; }
label_payload() { jq -c .labels "$STUB_DIR/payload-labels.json"; }
audit_calls() { grep -c -- '--audit-pr' "$STUB_DIR/daemon-calls.log" || true; }

echo "Leg A: create-pr.sh <-> forge priority-labels wiring (#10518)"

# A1: GraphQL path -- the verb's labels join --label on `gh pr create`.
reset_fixtures
printf 'loom:operator-priority\n' > "$STUB_DIR/stars.txt"
run_create_pr --title t --body "Closes #77" --head feature/issue-77 --label loom:review-requested
assert_eq "0" "$EXIT_CODE" "A1: exit 0"
assert_contains "$(create_argv)" "--label loom:review-requested --label loom:operator-priority" "A1: star added after the caller's label"
assert_contains "$(cat "$STUB_DIR/daemon-calls.log")" "--repo owner/repo" "A1: the verb is asked about the PR's own repo"
assert_eq "Closes #77" "$(head -1 "$STUB_DIR/daemon-stdin.txt")" "A1: the body reaches the verb on stdin"
assert_eq "1" "$(audit_calls)" "A1: one audit call once the PR exists"
assert_eq "https://github.com/owner/repo/pull/1111" "$STDOUT" "A1: stdout is only the URL"

# A2: REST fallback -- the star is in the one labels POST.
reset_fixtures
printf 'loom:operator-priority\nloom:high-priority-inherited\n' > "$STUB_DIR/stars.txt"
echo "$RATE_LIMIT_ERR" > "$STUB_DIR/create-err.txt"
run_create_pr --title t --body "Fixes #78" --head feature/issue-78 --label loom:review-requested
assert_eq "0" "$EXIT_CODE" "A2: rate-limited create -> exit 0"
assert_eq '["loom:review-requested","loom:operator-priority","loom:high-priority-inherited"]' "$(label_payload)" "A2: REST labels POST carries every copied label"
assert_eq "1" "$(audit_calls)" "A2: audit posted after the REST create"

# A3: REST labels POST fails -> no audit for a star that never landed.
reset_fixtures
printf 'loom:operator-priority\n' > "$STUB_DIR/stars.txt"
echo "$RATE_LIMIT_ERR" > "$STUB_DIR/create-err.txt"
touch "$STUB_DIR/labels-fail"
run_create_pr --title t --body "Closes #79" --head feature/issue-79 --label loom:review-requested
assert_eq "0" "$EXIT_CODE" "A3: exit 0"
assert_contains "$STDERR" "NOT applied: loom:review-requested loom:operator-priority" "A3: the unapplied star is named"
assert_eq "0" "$(audit_calls)" "A3: no audit comment when the labels did not land"

# A4: a caller that already passed the star gets no duplicate.
reset_fixtures
printf 'loom:operator-priority\n' > "$STUB_DIR/stars.txt"
run_create_pr --title t --body "Closes #80" --head feature/issue-80 --label loom:review-requested --label loom:operator-priority
assert_eq "1" "$(create_argv | grep -o -- '--label loom:operator-priority' | wc -l | tr -d ' ')" "A4: star passed once"

# A5: nothing to copy (unstarred / Part of) -> only the caller's labels, no audit.
reset_fixtures
run_create_pr --title t --body "Part of #81" --head feature/issue-81 --label loom:review-requested
assert_not_contains "$(create_argv)" "operator-priority" "A5: no star"
assert_eq "0" "$(audit_calls)" "A5: no audit call"
assert_not_contains "$STDERR" "WARNING" "A5: no warning"

# A6: the verb's own warning (a failed lookup) is passed through; PR opens.
reset_fixtures
echo "loom-daemon forge priority-labels: WARNING: could not read issue #82's labels (HTTP 502)" > "$STUB_DIR/stars-err.txt"
run_create_pr --title t --body "Closes #82" --head feature/issue-82 --label loom:review-requested
assert_eq "0" "$EXIT_CODE" "A6: failed lookup still opens the PR"
assert_contains "$STDERR" "could not read issue #82's labels" "A6: the warning names the issue"

# A7: a daemon predating the verb -> one warning naming the issue; PR opens.
reset_fixtures
touch "$STUB_DIR/daemon-old"
run_create_pr --title t --body "Closes #83" --head feature/issue-83 --label loom:review-requested
assert_eq "0" "$EXIT_CODE" "A7: no verb -> PR still opens"
assert_contains "$STDERR" "WARNING: issue #83 was NOT checked for priority labels" "A7: one warning naming the issue"
assert_eq "1" "$(grep -c 'NOT checked for priority labels' <<< "$STDERR")" "A7: exactly one warning line"
assert_contains "$(create_argv)" "--label loom:review-requested" "A7: caller's label kept"

# --- Leg B: the real binary ------------------------------------------------------
REAL_BIN="${LOOM_TEST_REAL_DAEMON_BIN:-}"
if [[ -z "$REAL_BIN" ]]; then
  for c in "$REPO_ROOT/target/debug/loom-daemon" "$REPO_ROOT/target/release/loom-daemon"; do
    [[ -x "$c" ]] && { REAL_BIN="$c"; break; }
  done
fi
if [[ -n "$REAL_BIN" ]] && "$REAL_BIN" forge priority-labels --help >/dev/null 2>&1; then
  echo ""
  echo "Leg B: real loom-daemon ($REAL_BIN)"
  use_daemon "$REAL_BIN"
  export LOOM_HOME="$STUB_DIR/loom-home" HOME="$STUB_DIR/home"
  mkdir -p "$HOME"
  # The real daemon also answers `forge may-write`: register the fixture as a
  # writable managed checkout, exactly as the other create-pr suites do.
  # shellcheck source=lib/write-scope-fixture.sh
  source "$SCRIPT_DIR/lib/write-scope-fixture.sh"
  write_scope_register "$FIXTURE_REPO" owner/repo

  # B1: multi-ref union over Closes/Fixes/Loom-Issue; Part-of and the
  #     unstarred issue add nothing; result is deduplicated, in level order.
  reset_fixtures
  echo '["loom:issue","loom:operator-priority"]' > "$STUB_DIR/issue-1.labels"
  echo '["loom:operator-high-priority","loom:operator-priority"]' > "$STUB_DIR/issue-2.labels"
  echo '["loom:building"]' > "$STUB_DIR/issue-3.labels"
  echo '["loom:high-priority-inherited"]' > "$STUB_DIR/issue-4.labels"
  echo '["loom:operator-priority"]' > "$STUB_DIR/issue-9.labels"
  printf 'Closes #1\nFixes #2\nResolves #3\nPart of #9\n\nLoom-Issue: owner/repo#4\n' > "$STUB_DIR/b1.md"
  run_create_pr --title t --body-file "$STUB_DIR/b1.md" --head feature/issue-1 --label loom:review-requested
  assert_eq "0" "$EXIT_CODE" "B1: exit 0"
  assert_contains "$(create_argv)" "--label loom:review-requested --label loom:operator-priority --label loom:operator-high-priority --label loom:high-priority-inherited" "B1: union of every closing ref's priority labels, in level order"
  assert_eq "0" "$(grep -c 'issues/9$' "$STUB_DIR/gh-calls.log" || true)" "B1: the Part-of issue is never read"
  audits="$(grep 'issues/1111/comments' "$STUB_DIR/gh-calls.log" || true)"
  assert_eq "3" "$(grep -c 'inherited_from=#' <<< "$audits" || true)" "B1: one audit comment per contributing issue (#1, #2, #4)"
  assert_contains "$audits" "inherit-1-1111 action=star inherited_from=#1 -->" "B1: the audit uses #10012 section 2's inherited-star marker"
  assert_not_contains "$STDERR" "WARNING" "B1: no warning"

  # B2: Part of only -> nothing copied, no warning.
  reset_fixtures
  echo '["loom:operator-priority"]' > "$STUB_DIR/issue-9.labels"
  run_create_pr --title t --body "Part of #9" --head feature/issue-9 --label loom:review-requested
  assert_not_contains "$(create_argv)" "operator-priority" "B2: Part of #9 copies nothing"
  assert_not_contains "$STDERR" "WARNING" "B2: no warning"

  # B3: REST fallback with the real verb.
  reset_fixtures
  echo '["loom:operator-priority"]' > "$STUB_DIR/issue-5.labels"
  echo "$RATE_LIMIT_ERR" > "$STUB_DIR/create-err.txt"
  run_create_pr --title t --body "Closes #5" --head feature/issue-5 --label loom:review-requested
  assert_eq '["loom:review-requested","loom:operator-priority"]' "$(label_payload)" "B3: REST path carries the star"

  # B4: one lookup fails (no fixture -> HTTP 502) -> warning names it; the
  #     other issue's star is still copied and the PR opens.
  reset_fixtures
  echo '["loom:operator-priority"]' > "$STUB_DIR/issue-6.labels"
  run_create_pr --title t --body $'Closes #6\nCloses #7' --head feature/issue-6 --label loom:review-requested
  assert_eq "0" "$EXIT_CODE" "B4: exit 0"
  assert_contains "$STDERR" "could not read issue #7's labels" "B4: warning names the failed issue"
  assert_eq "1" "$(grep -c "issue #7's labels" <<< "$STDERR")" "B4: one warning line for it"
  assert_contains "$(create_argv)" "--label loom:operator-priority" "B4: #6's star still copied"
else
  echo ""
  echo "Leg B skipped: no loom-daemon with \`forge priority-labels\` (set LOOM_TEST_REAL_DAEMON_BIN)"
fi

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
  echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
  exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
