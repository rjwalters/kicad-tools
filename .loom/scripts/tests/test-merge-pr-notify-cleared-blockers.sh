#!/usr/bin/env bash
# test-merge-pr-notify-cleared-blockers.sh - Unit tests for the close-triggered
# `loom:blocked` re-check wired into merge-pr.sh (#9102, item 2 of #8927's own
# deferred "Suggested fix" list).
#
# #8927 shipped a per-repository, sweep-pre-flight advisory
# (`loom-daemon check-stale-blocked`) that re-checks every open `loom:blocked`
# artifact in one repository once per sweep. This closes the highest-value gap #8927 itself
# named: the moment an issue/PR closes, immediately check whether any OTHER
# open `loom:blocked` artifact cited it as a blocker, rather than waiting for
# the next sweep. `merge-pr.sh`'s `_notify_cleared_blockers` is a one-line
# call; resolving the merged PR's closed issues, the decision AND the comment
# all live in `loom-daemon notify-cleared-blockers --pr`, which this
# suite runs FOR REAL (unlike the `merge_pr` "decide from stdin" family, this
# subcommand makes its own `gh` calls, so there is no JSON-on-stdin contract to
# fixture instead).
#
# Strategy: mirrors test-merge-pr-closed-issue-cleanup.sh — extract
# `_notify_cleared_blockers` from merge-pr.sh and source it, resolve a REAL
# `loom-daemon` binary via lib/require-daemon-bin.sh (this suite's subject
# calls the binary, not a JSON-on-stdin decision function, so there is no
# separate mock to swap in), stub `gh` on PATH to serve canned issue/PR JSON
# and record mutating calls, then assert on the recorded calls.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-notify-cleared-blockers.sh

# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "notify-cleared-blockers"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" <<<"$haystack"; then
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
    if ! grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

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

# --- Minimal logging shims the extracted function calls ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }

# --- Extract the function under test from merge-pr.sh and source it ---
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT
awk '
  /^_notify_cleared_blockers\(\) \{/ { capture=1 }
  capture { print }
  capture && /(^}|; }$)/ { capture=0 }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q "^_notify_cleared_blockers() {" "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _notify_cleared_blockers from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub gh on PATH ---
# Since #10515 the daemon reads the blocked population through the batched
# REST + ETag gatherer `check-stale-blocked` uses (#10480): one REST listing,
# REST comment / issue / pull reads, one aliased GraphQL query per 100 citers.
# The REST half below is test-check-stale-blocked.sh's stub, answered from the
# same per-artifact fixture files. `gh issue|pr view` is REFUSED (exit 3), bar
# the merged PR's one `--json closingIssuesReferences` read: a per-artifact
# GraphQL view on the merge path is the regression #10515 removed.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$D/gh-calls.log"
printf '%s\n' "$*" >>"$D/all-calls.log"

case "${1:-}:${2:-}" in
  pr:view)
    if [[ " $* " == *" closingIssuesReferences "* ]]; then
      cat "$D/pr-${3:-}.json" 2>/dev/null || echo '{}'
      exit 0
    fi
    ;;
  issue:comment|pr:comment)
    entity="$1" num="${3:-}"
    echo "$entity comment $num" >> "$LOG"
    shift 3
    body=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --body) shift; body="${1:-}"; shift ;;
        *) shift ;;
      esac
    done
    printf '%s' "$body" > "$D/comment-body-$entity-$num.txt"
    exit 0
    ;;
esac

http() { # <status line> <body>
  printf 'HTTP/2.0 %s\r\nContent-Type: application/json\r\n\r\n%s' "$1" "$2"
}
not_found() { http "404 Not Found" '{"message":"Not Found"}'; echo "gh: Not Found (HTTP 404)" >&2; exit 1; }

# label_objs: normalise a fixture's labels (objects or names) to REST objects.
LABELS='(.labels // []) | map({name: (if type == "object" then .name else . end)})'

if [[ "${1:-}" == "api" ]]; then
  shift
  url="" query=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --include|-i) shift ;;
      -H|--hostname|--method|-X) shift 2 ;;
      -f|-F|--raw-field|--field) [[ "${2:-}" == query=* ]] && query="${2#query=}"; shift 2 ;;
      *) [[ -z "$url" ]] && url="$1"; shift ;;
    esac
  done

  # The free budget probe: unanswered unless `rate_limit.json` is set.
  RL="$D/rate_limit.json"
  if [[ "$url" == "rate_limit" ]]; then
    [[ -f "$RL" ]] || { echo "stub gh: no rate_limit fixture" >&2; exit 3; }
    jq -c '{resources: {core: {limit: 5000, used: (5000 - .core), remaining: .core, reset: 4102444800}}}' "$RL"
    exit 0
  fi
  if [[ "$url" == "graphql" && "$query" == *"rateLimit{limit"* ]]; then
    [[ -f "$RL" ]] || { echo "stub gh: no rate_limit fixture" >&2; exit 3; }
    http "200 OK" "$(jq -c '{data: {rateLimit: {limit: 5000, used: (5000 - .graphql),
      remaining: .graphql, resetAt: "2100-01-01T00:00:00Z"}}}' "$RL")"
    exit 0
  fi

  if [[ "$url" == "graphql" ]]; then
    repo='{}'
    for n in $(grep -oE 'i[0-9]+: issue' <<<"$query" | tr -dc '0-9\n'); do
      f="$D/issue-$n.json"
      if [[ ! -f "$f" ]]; then
        repo="$(jq -c --arg k "i$n" '. + {($k): null}' <<<"$repo")"
        continue
      fi
      nodes='[]'
      for m in $(jq -r '(.closedByPullRequestsReferences // [])[].number' "$f"); do
        st="$(jq -r '.state // "OPEN"' "$D/pr-$m.json" 2>/dev/null || echo OPEN)"
        nodes="$(jq -c --argjson m "$m" --arg s "$st" '. + [{number: $m, state: $s}]' <<<"$nodes")"
      done
      repo="$(jq -c --arg k "i$n" --argjson nodes "$nodes" \
        '. + {($k): {closedByPullRequestsReferences: {totalCount: ($nodes|length), nodes: $nodes}}}' <<<"$repo")"
    done
    left="$(jq -r '.graphql // 4999' "$RL" 2>/dev/null)"
    [[ "$left" =~ ^[0-9]+$ ]] || left=4999
    jq -c -n --argjson r "$repo" --argjson left "$left" \
      '{data: {rateLimit: {cost: 1, remaining: $left}, repository: $r}}'
    exit 0
  fi

  path="${url%%\?*}"
  case "$path" in
    repos/*/*/issues)
      [[ "$url" == *"&page="* ]] && { http "200 OK" '[]'; exit 0; }
      out='[]'
      for kind in issue pr; do
        lf="$D/$kind-list.json"
        [[ -f "$lf" ]] || continue
        for n in $(jq -r '.[].number' "$lf"); do
          title="$(jq -r --argjson n "$n" '.[] | select(.number == $n) | .title' "$lf")"
          fx='{}'
          [[ -f "$D/$kind-$n.json" ]] && fx="$(cat "$D/$kind-$n.json")"
          out="$(jq -c --argjson n "$n" --arg t "$title" --arg k "$kind" --argjson fx "$fx" \
            ". + [{number: \$n, title: \$t, state: \"open\", user: {login: \"someone\"},
                   body: (\$fx.body // \"\"), comments: ((\$fx.comments // []) | length),
                   labels: ([{name: \"loom:blocked\"}] + (\$fx | $LABELS))}
                  + (if \$k == \"pr\" then {pull_request: {url: \"x\"}} else {} end)]" <<<"$out")"
        done
      done
      http "200 OK" "$out"
      exit 0
      ;;
    repos/*/*/issues/*/comments)
      n="$(cut -d/ -f5 <<<"$path")"
      [[ "$url" == *"&page=1"* ]] || { http "200 OK" '[]'; exit 0; }
      f="$D/issue-$n.json"; [[ -f "$f" ]] || f="$D/pr-$n.json"
      [[ -f "$f" ]] || not_found
      http "200 OK" "$(jq -c '(.comments // []) | map({user: {login: .author.login}, body})' "$f")"
      exit 0
      ;;
    repos/*/*/issues/*)
      n="${path##*/}"
      for kind in issue pr; do
        f="$D/$kind-$n.json"
        [[ -f "$f" ]] && jq -e 'has("state")' "$f" >/dev/null || continue
        http "200 OK" "$(jq -c --arg k "$kind" \
          "{number, state: (if .state == \"OPEN\" then \"open\" else \"closed\" end),
            labels: ($LABELS)}
           + (if \$k == \"pr\" then {pull_request: {merged_at:
               (if .state == \"MERGED\" then \"2026-01-01T00:00:00Z\" else null end)}} else {} end)" "$f")"
        exit 0
      done
      not_found
      ;;
    repos/*/*/pulls/*)
      f="$D/pr-${path##*/}.json"
      [[ -f "$f" ]] || not_found
      http "200 OK" "$(jq -c '{number, state: (.state | ascii_downcase),
        mergeable: (if .mergeable == "MERGEABLE" then true elif .mergeable == "CONFLICTING" then false else null end),
        mergeable_state: ((.mergeStateStatus // "unknown") | ascii_downcase)}' "$f")"
      exit 0
      ;;
  esac
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
# Keep the gatherer's ETag store out of the user's real cache.
export LOOM_LISTING_CACHE_DIR="$STUB_DIR/etag-cache"

# --- Shared globals the function reads ---
REPO_NWO="owner/repo"
PR_NUMBER="999"
FORGE_TYPE="github"
GH="gh"

FORGE_CLOSE_TARGETS=""

# The daemon resolves the merged PR's closed issues itself (`gh pr view $PR
# --json closingIssuesReferences`), so serve FORGE_CLOSE_TARGETS through the
# stub's pr-$PR_NUMBER.json before each run.
run_notify() {
  local refs="" n
  for n in $FORGE_CLOSE_TARGETS; do refs+="${refs:+,}{\"number\":$n}"; done
  printf '{"closingIssuesReferences":[%s]}' "$refs" > "$STUB_DIR/pr-$PR_NUMBER.json"
  _notify_cleared_blockers
}

reset_fixtures() {
  : > "$STUB_DIR/gh-calls.log"
  : > "$STUB_DIR/all-calls.log"
  rm -f "$STUB_DIR"/issue-*.json "$STUB_DIR"/pr-*.json "$STUB_DIR"/issue-list.json \
        "$STUB_DIR"/pr-list.json "$STUB_DIR"/comment-body-*.txt
  echo '[]' > "$STUB_DIR/pr-list.json"
  FORGE_CLOSE_TARGETS=""
}
read_log()  { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }
# Every gh call of the last run: none may be a per-artifact `issue|pr view`.
assert_no_view() {
  local calls
  calls="$(grep -E '^(issue|pr) view' "$STUB_DIR/all-calls.log" | grep -v closingIssuesReferences || true)"
  assert_eq "" "$calls" "$1: no per-artifact gh issue|pr view (#10515)"
}
read_comment_body() { cat "$STUB_DIR/comment-body-issue-$1.txt" 2>/dev/null || true; }

# Run the extracted function from inside STUB_DIR so the daemon's own
# `std::env::current_dir()` default (no --repo-root is passed by the shell
# wrapper) resolves to a plain, harmless directory rather than this checkout.
cd "$STUB_DIR"

echo "Testing _notify_cleared_blockers behavior..."

# T1: one open loom:blocked issue (#201) cites the just-closed #200 via a
# prose `Blocked by #200` reference -> a comment is posted on #201, carrying
# the idempotency marker for #200.
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":201,"title":"Depends on the other thing"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/issue-201.json" <<'EOF'
{"body":"Blocked by #200: needs that first.","comments":[],"closedByPullRequestsReferences":[]}
EOF
FORGE_CLOSE_TARGETS="200"
run_notify
log="$(read_log)"
assert_contains "$log" "issue comment 201" \
  "#201 cites the just-closed #200 -> a comment is posted on #201"
body="$(read_comment_body 201)"
assert_contains "$body" "<!-- loom:blocker-cleared:#200 -->" \
  "Posted comment carries the idempotency marker for #200"
assert_contains "$body" "#200" \
  "Posted comment names the closed blocker"
assert_no_view "T1"

# T2: same population, but #201 does NOT cite #200 at all -> no comment.
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":202,"title":"Unrelated blocked issue"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/issue-202.json" <<'EOF'
{"body":"Blocked by #555: something else entirely.","comments":[],"closedByPullRequestsReferences":[]}
EOF
cat > "$STUB_DIR/issue-555.json" <<'EOF'
{"state":"OPEN"}
EOF
FORGE_CLOSE_TARGETS="200"
run_notify
assert_eq "" "$(read_log)" \
  "#202 cites an unrelated blocker (#555) -> no comment posted for #200's close"
assert_no_view "T2"

# T3: #201 cites #200, but #200 is still OPEN in this population (a stale
# fixture / a race) -> classify() reports StillBlocked, not Stale -> no
# comment (only the close of the CITED number should ever trigger a post).
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":203,"title":"Cites 200 but 200 reads open here"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"OPEN"}
EOF
cat > "$STUB_DIR/issue-203.json" <<'EOF'
{"body":"Blocked by #200: needs that first.","comments":[],"closedByPullRequestsReferences":[]}
EOF
FORGE_CLOSE_TARGETS="200"
run_notify
assert_eq "" "$(read_log)" \
  "#203 cites #200 but #200 still reads OPEN -> StillBlocked, no comment"

# T4: the PR closed no issue and nothing cites the PR itself -> no comment.
reset_fixtures
FORGE_CLOSE_TARGETS=""
run_notify
assert_eq "" "$(read_log)" "No close targets and no citation of the PR -> no comment"

# T5: FORGE_TYPE != github -> no-op (GitHub-only v1, mirrors the sibling
# closed-issue-cleanup pass's own gating).
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":201,"title":"Depends on the other thing"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/issue-201.json" <<'EOF'
{"body":"Blocked by #200: needs that first.","comments":[],"closedByPullRequestsReferences":[]}
EOF
FORGE_TYPE="gitea"
FORGE_CLOSE_TARGETS="200"
run_notify
assert_eq "" "$(read_log)" "FORGE_TYPE=gitea -> no-op (GitHub-only v1)"
FORGE_TYPE="github"

# T6: idempotency -- #201 already carries the marker for #200 (a prior run
# already notified it) -> re-running must NOT double-post.
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":201,"title":"Depends on the other thing"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/issue-201.json" <<'EOF'
{"body":"Blocked by #200: needs that first.","comments":[{"author":{"login":"loom-fleet-dispatch"},"body":"<!-- loom:blocker-cleared:#200 -->"}],"closedByPullRequestsReferences":[]}
EOF
FORGE_CLOSE_TARGETS="200"
run_notify
assert_eq "" "$(read_log)" \
  "#201 already carries the #200 marker -> no duplicate comment"

# T7: a PR (not just an issue) carrying loom:blocked also cites the closed
# issue -> the PR population is scanned too and gets its own comment.
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[]
EOF
cat > "$STUB_DIR/pr-list.json" <<'EOF'
[{"number":301,"title":"Parked PR"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/pr-301.json" <<'EOF'
{"body":"Blocked by #200: filed to track it, standing down.","comments":[],"number":301,"state":"OPEN","labels":[],"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN"}
EOF
FORGE_CLOSE_TARGETS="200"
run_notify
log="$(read_log)"
assert_contains "$log" "pr comment 301" \
  "A parked PR citing the just-closed #200 is also notified"
assert_no_view "T7"

# T8: the blocker is the merged PR ITSELF (`Blocked by #999`), with no issue
# closed by it -> the merged PR number is always part of the closed set.
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":204,"title":"Waits on the PR"}]
EOF
cat > "$STUB_DIR/issue-999.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/issue-204.json" <<'EOF'
{"body":"Blocked by #999 landing.","comments":[],"closedByPullRequestsReferences":[]}
EOF
FORGE_CLOSE_TARGETS=""
run_notify
assert_contains "$(read_log)" "issue comment 204" \
  "An issue citing the merged PR itself (#999) is notified"
assert_contains "$(read_comment_body 204)" "<!-- loom:blocker-cleared:#999 -->" \
  "The notification carries the merged PR's marker"

# T9: the blocker reference lives only in a comment, not the body.
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":205,"title":"Comment-only blocker"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/issue-205.json" <<'EOF'
{"body":"No blocker named here.","comments":[{"author":{"login":"a-human"},"body":"Depends on #200"}],"closedByPullRequestsReferences":[]}
EOF
FORGE_CLOSE_TARGETS="200"
run_notify
assert_contains "$(read_log)" "issue comment 205" \
  "A blocker cited only in a comment is found"

# T10: two open issues cite the same closed blocker in one close event ->
# both are notified, once each.
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":206,"title":"First"},{"number":207,"title":"Second"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/issue-206.json" <<'EOF'
{"body":"Blocked by #200","comments":[],"closedByPullRequestsReferences":[]}
EOF
cat > "$STUB_DIR/issue-207.json" <<'EOF'
{"body":"## Dependencies\n\n- [ ] #200: the prerequisite\n","comments":[],"closedByPullRequestsReferences":[]}
EOF
FORGE_CLOSE_TARGETS="200"
run_notify
log="$(read_log)"
assert_contains "$log" "issue comment 206" "First citer (prose) notified"
assert_contains "$log" "issue comment 207" "Second citer (## Dependencies) notified"
assert_eq "2" "$(grep -c 'comment' <<<"$log")" "Exactly one comment per citer"
assert_contains "$(read_comment_body 207)" "checklist box is still unticked" \
    "Second citer's comment names the unticked ## Dependencies box (#9274)"
assert_no_view "T10"

# T11: an issue cites two blockers; this merge closes one, the other stays
# OPEN. The partially resolved set is still reported (check-stale-blocked's
# own "any reference no longer OPEN" rule).
reset_fixtures
cat > "$STUB_DIR/issue-list.json" <<'EOF'
[{"number":208,"title":"Two blockers"}]
EOF
cat > "$STUB_DIR/issue-200.json" <<'EOF'
{"state":"CLOSED"}
EOF
cat > "$STUB_DIR/issue-555.json" <<'EOF'
{"state":"OPEN"}
EOF
cat > "$STUB_DIR/issue-208.json" <<'EOF'
{"body":"Blocked by #200\nBlocked by #555","comments":[],"closedByPullRequestsReferences":[]}
EOF
FORGE_CLOSE_TARGETS="200"
run_notify
assert_contains "$(read_log)" "issue comment 208" \
  "A partially cleared block is still notified"

# --- Invariant guard: merge-pr.sh actually wires this pass in at the
# confirmed-merge choke point, right after the loom:building cleanup ---
echo ""
echo "Testing merge-pr.sh wiring..."

src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_notify_cleared_blockers || true" \
  "merge-pr.sh invokes the close-triggered re-check at the confirmed-merge choke point"
assert_contains "$src" 'notify-cleared-blockers --pr "$PR_NUMBER"' \
  "merge-pr.sh delegates the decision and the comment-post to loom-daemon notify-cleared-blockers"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
