#!/usr/bin/env bash
# test-create-pr-rate-limit-fallback.sh - Unit tests for create-pr.sh's REST
# fallback when `gh pr create` is rejected by a GraphQL rate limit (#9226).
#
# `gh pr create` is GraphQL-backed; GitHub's GraphQL and REST quotas are
# independent, so a fleet can exhaust the GraphQL pool while REST sits idle.
# Before #9226 that left a Builder's pushed branch with no PR. create-pr.sh now
# retries the identical filing as `POST repos/OWNER/REPO/pulls` (+ a labels
# POST), gated on the shared `is_rate_limit_error` predicate ONLY.
#
# Strategy: same as test-create-pr-review-gate.sh -- run create-pr.sh as a
# subprocess with a stub `gh` on PATH (logging argv and each REST payload on
# stdin) and a stub daemon that predates every verb it is asked for, so the
# shell fallbacks (write scope: origin-only checkout) are what run.
#
# Usage:
#   ./.loom/scripts/tests/test-create-pr-rate-limit-fallback.sh

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

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT

# --- Stub gh on PATH ---------------------------------------------------------
#   pr list                                  -> no open PR (adopt-first misses)
#   pr create                                -> fails with create-err.txt on
#                                               stderr when present, else a URL
#   api repos/owner/repo --jq .default_branch -> "trunk" (fails: default-fail)
#   api --method POST repos/owner/repo/pulls  -> stdin -> payload-pulls.json;
#                                               html_url + number (fails: rest-fail)
#   api --method POST .../issues/N/labels     -> stdin -> payload-labels.json
#                                               (fails: labels-fail)
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
echo "$*" >> "$D/gh-calls.log"
if [[ "$1" == "pr" && "$2" == "list" ]]; then exit 0; fi
if [[ "$1" == "pr" && "$2" == "create" ]]; then
  if [[ -f "$D/create-err.txt" ]]; then cat "$D/create-err.txt" >&2; exit 1; fi
  echo "https://github.com/owner/repo/pull/1111"; exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo" ]]; then
  [[ -f "$D/default-fail" ]] && { echo "HTTP 502: Bad Gateway" >&2; exit 1; }
  if [[ "${3:-}" == "--jq" ]]; then echo "trunk"; else echo '{"push":true,"default_branch":"trunk"}'; fi
  exit 0
fi
if [[ "$1 $2 $3" == "api --method POST" && "$4" == "repos/owner/repo/pulls" ]]; then
  cat > "$D/payload-pulls.json"
  [[ -f "$D/rest-fail" ]] && { echo "HTTP 422: Validation Failed (https://docs.github.com/rest)" >&2; exit 1; }
  printf '%s\n%s\n' "https://github.com/owner/repo/pull/4242" "4242"; exit 0
fi
if [[ "$1 $2 $3" == "api --method POST" && "$4" == repos/owner/repo/issues/*/labels ]]; then
  cat > "$D/payload-labels.json"
  [[ -f "$D/labels-fail" ]] && { echo "HTTP 403: label write denied" >&2; exit 1; }
  echo '[]'; exit 0
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

# A daemon predating every verb create-pr.sh asks for: check-open-pr fails
# open, provenance falls back to its all-unknown marker, and the write scope
# falls back to the origin-only-checkout rule.
cat > "$STUB_DIR/daemon" <<'STUB'
#!/usr/bin/env bash
echo "error: unrecognized subcommand" >&2
exit 2
STUB
chmod +x "$STUB_DIR/daemon"

cat > "$STUB_DIR/version-check-ok.sh" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$STUB_DIR/version-check-ok.sh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export LOOM_FORGE_TYPE=github
export LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-check-ok.sh"
export LOOM_DAEMON_SELF_BIN="$STUB_DIR/daemon"
export LOOM_DAEMON_BIN="$STUB_DIR/daemon"
export LOOM_GH_BIN="$STUB_DIR/gh"
export LOOM_GH_NO_POLICY_LAUNCHER=1  # no host egress-policy launcher over the stub (#9995)
export LOOM_WRITE_SCOPE_CACHE_DIR="$STUB_DIR/write-scope-cache"
unset GH_REPO LOOM_REPO

FIXTURE_REPO="$STUB_DIR/repo"
git init -q "$FIXTURE_REPO"
git -C "$FIXTURE_REPO" remote add origin https://github.com/owner/repo.git
mkdir -p "$FIXTURE_REPO/.loom"

RATE_LIMIT_ERR="GraphQL: API rate limit already exceeded for installation ID 1"

reset_fixtures() {
  rm -f "$STUB_DIR"/gh-calls.log "$STUB_DIR"/create-err.txt "$STUB_DIR"/payload-*.json
  rm -f "$STUB_DIR"/default-fail "$STUB_DIR"/rest-fail "$STUB_DIR"/labels-fail
  : > "$STUB_DIR/gh-calls.log"
}

run_create_pr() {
  set +e
  STDOUT=$(cd "$FIXTURE_REPO" && "$CREATE_PR" "$@" 2>"$STUB_DIR/stderr.log")
  EXIT_CODE=$?
  set -e
  STDERR="$(cat "$STUB_DIR/stderr.log")"
}

calls_matching() { grep -c -- "$1" "$STUB_DIR/gh-calls.log" || true; }
payload() { jq -r "$1" "$STUB_DIR/payload-pulls.json"; }

BODY_FILE="$STUB_DIR/body.md"
# shellcheck disable=SC2016  # the literal $HOME/backticks are the point: they must reach the payload unexpanded
printf 'Closes #77\n\nQuotes "x" and $HOME and `ticks`.\n' > "$BODY_FILE"

echo "Testing create-pr.sh GraphQL rate-limit REST fallback (#9226)..."
echo ""

# T1: GraphQL rate-limited -> exit 0, REST URL on stdout, exactly one pulls
#     POST and one labels POST, payload carries the exact title/body/head and
#     the repo default branch as base (no --base given).
reset_fixtures
echo "$RATE_LIMIT_ERR" > "$STUB_DIR/create-err.txt"
run_create_pr --title 'fix: a "quoted" title' --body-file "$BODY_FILE" --head feature/issue-77 --label loom:review-requested
assert_eq "0" "$EXIT_CODE" "T1: rate-limited create falls back to REST -> exit 0"
assert_eq "https://github.com/owner/repo/pull/4242" "$STDOUT" "T1: stdout is exactly the REST html_url"
assert_eq "1" "$(calls_matching 'api --method POST repos/owner/repo/pulls')" "T1: exactly one POST repos/owner/repo/pulls"
assert_eq "1" "$(calls_matching 'api --method POST repos/owner/repo/issues/4242/labels')" "T1: exactly one POST .../issues/4242/labels"
assert_eq 'fix: a "quoted" title' "$(payload .title)" "T1: payload title is exact"
assert_contains "$(payload .body)" "$(cat "$BODY_FILE")" "T1: payload body is the file's contents (not a literal @path)"
assert_eq "feature/issue-77" "$(payload .head)" "T1: payload head is the branch"
assert_eq "trunk" "$(payload .base)" "T1: omitted --base -> repo default branch (REST lookup)"
assert_eq "false" "$(payload .draft)" "T1: draft defaults to false"
assert_eq '["loom:review-requested"]' "$(jq -c .labels "$STUB_DIR/payload-labels.json")" "T1: labels payload names the requested label"
assert_contains "$STDERR" "$RATE_LIMIT_ERR" "T1: the GraphQL rejection is still surfaced on stderr"
assert_contains "$STDERR" "#9226" "T1: stderr announces the REST retry"

# T2: explicit --base and --draft carry through to the REST payload.
reset_fixtures
echo "$RATE_LIMIT_ERR" > "$STUB_DIR/create-err.txt"
run_create_pr --title "t" --body "b" --head feature/issue-78 --base release --draft
assert_eq "0" "$EXIT_CODE" "T2: rate-limited --draft create -> exit 0"
assert_eq "release" "$(payload .base)" "T2: --base is used verbatim"
assert_eq "true" "$(payload .draft)" "T2: --draft -> draft:true"
assert_eq "0" "$(calls_matching '^api repos/owner/repo --jq')" "T2: no default-branch lookup when --base is given"
assert_eq "0" "$(calls_matching '/labels')" "T2: no labels requested -> no labels POST"

# T3/T4: a NON-rate-limit failure makes no REST call and keeps today's exit 1.
for err in "HTTP 422: Validation Failed" "HTTP 403: Resource not accessible by integration"; do
  reset_fixtures
  echo "$err" > "$STUB_DIR/create-err.txt"
  run_create_pr --title "t" --body "b" --head feature/issue-79 --label loom:review-requested
  assert_eq "1" "$EXIT_CODE" "'$err' -> exit 1"
  assert_eq "0" "$(calls_matching 'api --method POST')" "'$err' -> no REST POST"
  assert_contains "$STDERR" "do NOT rebuild" "'$err' -> existing failure message"
done

# T5: REST create succeeds but labeling fails -> exit 0, URL on stdout,
#     stderr names the unapplied label.
reset_fixtures
echo "$RATE_LIMIT_ERR" > "$STUB_DIR/create-err.txt"
touch "$STUB_DIR/labels-fail"
run_create_pr --title "t" --body "b" --head feature/issue-80 --label loom:review-requested
assert_eq "0" "$EXIT_CODE" "T5: label POST failure after REST create -> still exit 0"
assert_eq "https://github.com/owner/repo/pull/4242" "$STDOUT" "T5: stdout is the URL"
assert_contains "$STDERR" "NOT applied: loom:review-requested" "T5: stderr names the unapplied label"
assert_contains "$STDERR" "label write denied" "T5: stderr carries the label error"

# T6: both GraphQL and REST fail -> exit 1, stderr has both error texts.
reset_fixtures
echo "$RATE_LIMIT_ERR" > "$STUB_DIR/create-err.txt"
touch "$STUB_DIR/rest-fail"
run_create_pr --title "t" --body "b" --head feature/issue-81 --label loom:review-requested
assert_eq "1" "$EXIT_CODE" "T6: GraphQL and REST both fail -> exit 1"
assert_eq "" "$STDOUT" "T6: no URL on stdout"
assert_contains "$STDERR" "$RATE_LIMIT_ERR" "T6: stderr has the GraphQL error"
assert_contains "$STDERR" "HTTP 422: Validation Failed" "T6: stderr has the REST error"
assert_contains "$STDERR" "do NOT rebuild" "T6: existing failure message kept"

# T7: the default-branch lookup fails -> no pulls POST, exit 1, error surfaced.
reset_fixtures
echo "$RATE_LIMIT_ERR" > "$STUB_DIR/create-err.txt"
touch "$STUB_DIR/default-fail"
run_create_pr --title "t" --body "b" --head feature/issue-82
assert_eq "1" "$EXIT_CODE" "T7: default-branch lookup failure -> exit 1"
assert_eq "0" "$(calls_matching 'api --method POST')" "T7: no pulls POST without a base"
assert_contains "$STDERR" "Bad Gateway" "T7: stderr names the lookup failure"

# T8: the GraphQL path still works untouched (no rate limit -> no REST).
reset_fixtures
run_create_pr --title "t" --body "b" --head feature/issue-83 --label loom:review-requested
assert_eq "0" "$EXIT_CODE" "T8: plain success -> exit 0"
assert_eq "https://github.com/owner/repo/pull/1111" "$STDOUT" "T8: stdout is gh pr create's URL"
assert_eq "0" "$(calls_matching 'api --method POST')" "T8: no REST POST on success"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
  echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
  exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
