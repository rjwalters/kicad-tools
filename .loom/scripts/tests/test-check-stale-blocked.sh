#!/usr/bin/env bash
# test-check-stale-blocked.sh - Unit tests for check-stale-blocked.sh (#8927),
# the fourth pre-wave advisory check.
#
# Mirrors test-dep-recheck-fingerprint.sh's style (a stubbed `gh` on PATH driving
# the real `loom-daemon` subcommand through the shipped stub) rather than
# test-check-quarantine-stashes.sh's, because the subject's inputs are forge
# reads, not local git state. What it pins:
#
#   (a) a STALE block   — the cited blocker has since closed/merged, as a prose
#                         `Blocked by #N` or a fully ticked `## Dependencies`
#                         checklist. An unticked box whose refs resolved is its
#                         own "BOXES UNTICKED" finding, and a linked closing PR
#                         is no blocker reference at all (#9274);
#   (b) an UNDOCUMENTED block — `loom:blocked` with no parseable blocker
#                         reference anywhere in body or comments (#8927's #180
#                         evidence row);
#   (c) a GENUINELY still-blocked issue — reports nothing, which is the whole
#                         reason this advisory can run on every sweep;
#   plus the advisory contract itself: always exit 0 (including with no
#   loom-daemon, no `gh`, and a forge read that fails), `--quiet` suppresses the
#   stdout one-liner, and the script never mutates a label.
#
# Usage:
#   ./.loom/scripts/tests/test-check-stale-blocked.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-stale-blocked.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

assert_contains() { # <haystack> <needle> <msg>
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else
        fail "$3"
        echo "    expected to contain: '$2'"
        echo "    actual: $(printf '%s' "$1" | head -40)"
    fi
}

assert_not_contains() { # <haystack> <needle> <msg>
    if [[ "$1" != *"$2"* ]]; then pass "$3"; else
        fail "$3"
        echo "    expected NOT to contain: '$2'"
        echo "    actual: $(printf '%s' "$1" | head -40)"
    fi
}

assert_eq() { # <expected> <actual> <msg>
    if [[ "$1" == "$2" ]]; then pass "$3"; else
        fail "$3"
        echo "    expected: '$1'"
        echo "    actual:   '$2'"
    fi
}

[[ -x "$SCRIPT" ]] || {
    echo -e "${RED}FATAL${NC}: $SCRIPT missing or not executable"
    exit 2
}
command -v jq >/dev/null 2>&1 || {
    echo -e "${RED}FATAL${NC}: jq required"
    exit 2
}

# One scratch root for every fixture this suite builds, so the EXIT trap has a
# single literal-rooted path to remove.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-stale-blocked.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# --- contract checks that need no daemon at all -----------------------------
# Run before the daemon pin, so a checkout with no build still proves the
# read-only and Shape-A guarantees rather than reporting green by running
# nothing.
echo "Group 0: static contract"

SRC="$(cat "$SCRIPT")"
assert_contains "$SRC" 'exec "$BIN" check-stale-blocked' \
    "T0a: the shipped script is a Shape-A stub execing the subcommand"
assert_not_contains "$SRC" "issue edit" \
    "T0b: the stub never shells out to \`gh issue edit\` (strictly read-only)"
assert_not_contains "$SRC" "--add-label" \
    "T0c: the stub never adds a label"
assert_not_contains "$SRC" "--remove-label" \
    "T0d: the stub never removes a label"
assert_contains "$SRC" "LOOM_SCRIPT_HELPER_MISSING_RC=0" \
    "T0e: the version-floor refusal lands on exit 0, not the usual 1 (advisory contract)"

# Exit 0 with no loom-daemon resolvable at all — the advisory must degrade, not
# fail. Isolation is what makes this assertion mean anything: run in place and
# `loom_locate_daemon_bin` finds this checkout's own `target/debug/loom-daemon`
# through its repo-relative candidates, so the test would pass for the wrong
# reason. The stub is copied into a throwaway tree with no build output, no
# Cargo.toml and no `loom-daemon` on PATH — the only arrangement in which
# nothing can answer.
ISOLATED="$WORKDIR/iso"
mkdir -p "$ISOLATED/scripts/lib"
cp "$SCRIPT" "$ISOLATED/scripts/"
cp "$HELPERS_DIR/lib/locate-daemon-bin.sh" "$ISOLATED/scripts/lib/"
rc=0
out="$(env -u LOOM_DAEMON_SELF_BIN -u LOOM_DAEMON_BIN -u CARGO_TARGET_DIR \
       LOOM_DAEMON_BIN_DIR="$ISOLATED/nowhere" \
       PATH=/usr/bin:/bin \
       "$ISOLATED/scripts/check-stale-blocked.sh" 2>&1)" || rc=$?
assert_eq "0" "$rc" "T0f: exits 0 when no loom-daemon resolves"
assert_contains "$out" "loom-daemon not found" \
    "T0g: says why it skipped when no loom-daemon resolves"

# --- pin the daemon under test ----------------------------------------------
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "check-stale-blocked"

# --- gh stub ----------------------------------------------------------------
# NOTE: require-daemon-bin.sh installs its own EXIT trap only when the suite has
# none, and this suite armed one above — so the snapshot it takes is reaped by
# that harness's own pid-keyed sweeper instead. That is the documented
# arrangement, not an oversight.
STUB_DIR="$WORKDIR/stub"
mkdir -p "$STUB_DIR"

cat >"$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# The check reads the forge in bulk (#10480): one REST `loom:blocked` listing
# (issues AND PRs), REST comment / single-issue / pull reads, and one aliased
# GraphQL query per 100 issues for the closing PRs. This stub answers each of
# those from the SAME fixture files the per-artifact `gh issue|pr view` shape
# used, so every case below keeps its meaning. Any `gh issue|pr view|list` is
# unhandled on purpose: the batch path must never make one.
set -uo pipefail
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
printf '%s\n' "$*" >>"$D/calls.log"

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

  # The free budget probe (#10480): `api rate_limit` (body only) and the
  # GraphQL `rateLimit` query (`-i`, head + body), both answered from
  # `rate_limit.json` ({"core":N,"graphql":N}) and unhandled without it, so
  # every case that does not set one sees a probe that did not answer.
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
# The batch path's ETag store must never touch the user's real cache (the stub
# sends no ETag, so nothing is stored, but keep it out of the real dir anyway).
export LOOM_LISTING_CACHE_DIR="$WORKDIR/etag-cache"

# run_check [extra args...] — invoke the subject against the stub fixtures.
# Stdout and stderr are captured separately, because which stream a message
# lands on IS the contract (warnings to stderr, the suppressible confirmation
# to stdout).
LAST_STDOUT=""
LAST_STDERR=""
LAST_RC=0
run_check() {
    LAST_RC=0
    : >"$STUB_DIR/calls.log"
    LAST_STDOUT="$("$SCRIPT" --repo owner/repo --repo-root "$STUB_DIR" "$@" \
        2>"$STUB_DIR/stderr.txt")" || LAST_RC=$?
    LAST_STDERR="$(cat "$STUB_DIR/stderr.txt")"
}

# set_population <jq-array-json> — the issue rows of the REST listing.
set_population() { printf '%s' "$1" >"$STUB_DIR/issue-list.json"; }

# set_pr_population <jq-array-json> — the PR rows of the REST listing (#8925).
set_pr_population() { printf '%s' "$1" >"$STUB_DIR/pr-list.json"; }

# issue_fixture <number> <body> [comments-json] [closing-prs-json]
issue_fixture() {
    jq -n --arg body "$2" \
          --argjson comments "${3:-[]}" \
          --argjson closed "${4:-[]}" \
          '{body: $body, comments: $comments, closedByPullRequestsReferences: $closed}' \
        >"$STUB_DIR/issue-$1.json"
}

# state_fixture <kind> <number> <state>
state_fixture() {
    jq -n --arg s "$3" --argjson n "$2" \
        '{number: $n, state: $s, labels: [], mergeable: "MERGEABLE", mergeStateStatus: "CLEAN"}' \
        >"$STUB_DIR/$1-$2.json"
}

# --- Group 1: a stale block, three shapes -----------------------------------
echo "Group 1: stale blocks"

# 1a. #8927's #178 row: a prose `Blocked by #7` whose blocker closed.
set_population '[{"number":178,"title":"Comment moderation"}]'
issue_fixture 178 "Blocked by #7 (user authentication)."
state_fixture issue 7 "CLOSED"
run_check
assert_eq "0" "$LAST_RC" "T1a: a stale prose block still exits 0"
assert_contains "$LAST_STDERR" "STALE BLOCK" "T1b: a closed prose blocker is reported as a stale block"
assert_contains "$LAST_STDERR" "#178" "T1c: names the suppressed issue"
assert_contains "$LAST_STDERR" "7:CLOSED" "T1d: names the blocker and its resolved state"
assert_contains "$LAST_STDOUT" "WARNING" "T1e: the stdout one-liner reports the warning"
assert_not_contains "$LAST_STDERR" "UNDOCUMENTED BLOCK" \
    "T1f: a documented-but-stale block is not also reported as undocumented"

# 1b. #8927's #179 row: a `## Dependencies` checklist whose refs all closed
# but whose boxes are still UNTICKED. #9274: an unchecked box is unmet until a
# human confirms its whole condition, so this is the "boxes unticked" finding,
# never a stale block.
set_population '[{"number":179,"title":"Reputation weighting"}]'
issue_fixture 179 "## Dependencies

- [ ] #176: rating infrastructure
- [ ] #177: vote plumbing
"
state_fixture issue 176 "CLOSED"
state_fixture issue 177 "CLOSED"
run_check
assert_eq "0" "$LAST_RC" "T1g: a resolved-but-unticked checklist still exits 0"
assert_contains "$LAST_STDERR" "CHECKLIST REFS RESOLVED, BOXES UNTICKED" \
    "T1h: unticked boxes with every ref closed are reported under BOXES UNTICKED"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" \
    "T1i: unticked boxes with every ref closed are NOT reported as a stale block"
assert_contains "$LAST_STDERR" "refs resolved: #176, #177" \
    "T1i2: names the resolved checklist refs"

run_check --quiet
assert_contains "$LAST_STDERR" "CHECKLIST REFS RESOLVED, BOXES UNTICKED" \
    "T1i3: --quiet still lists the unticked section"

run_check --json
assert_eq "179" "$(jq -r '.unticked[0].number' <<<"$LAST_STDOUT")" \
    "T1i4: --json carries the unticked bucket"
assert_eq "#176,#177" "$(jq -r '.unticked[0].resolved_refs | join(",")' <<<"$LAST_STDOUT")" \
    "T1i5: --json carries the resolved refs"
assert_eq "0" "$(jq -r '.stale | length' <<<"$LAST_STDOUT")" \
    "T1i6: --json does not list an unticked checklist as stale"

# 1b'. The same checklist with every box TICKED and every ref closed: still a
# stale block, and the checklist is the signal that fired.
set_population '[{"number":183,"title":"Every box ticked"}]'
issue_fixture 183 "## Dependencies

- [x] #176: rating infrastructure
- [x] #177: vote plumbing
"
run_check
assert_contains "$LAST_STDERR" "STALE BLOCK" \
    "T1h7: a fully ticked checklist with every ref closed is a stale block"
assert_contains "$LAST_STDERR" "checklist" "T1h8: names the checklist as the signal that fired"
assert_not_contains "$LAST_STDERR" "BOXES UNTICKED" \
    "T1h9: a fully ticked checklist is not reported as unticked"

# 1b''. A checklist whose only unchecked line carries no readable ref: the empty
# parseable set is vacuously resolved, so it is unticked with N unparsed lines.
set_population '[{"number":184,"title":"Unreadable condition"}]'
issue_fixture 184 "## Dependencies

- [ ] upstream vendor signs off on the pinout
"
run_check
assert_contains "$LAST_STDERR" "CHECKLIST REFS RESOLVED, BOXES UNTICKED" \
    "T1h10: an all-unparseable unchecked checklist is reported under BOXES UNTICKED"
assert_contains "$LAST_STDERR" "1 unchecked line(s) carry no readable ref" \
    "T1h11: counts the unparseable unchecked line"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" \
    "T1h12: an all-unparseable unchecked checklist is not a stale block"

# 1b-phrase. A dependency phrase after the box (`- [ ] Blocked by #N`) is a
# checklist entry, never also prose: its merged ref leaves the box unticked.
set_population '[{"number":185,"title":"Phrase after the box"}]'
issue_fixture 185 "## Dependencies

- [ ] Blocked by #176: ratification remains pending
"
run_check
assert_contains "$LAST_STDERR" "CHECKLIST REFS RESOLVED, BOXES UNTICKED" \
    "T1h13: an unticked 'Blocked by #N' checklist line is reported under BOXES UNTICKED"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" \
    "T1h14: an unticked 'Blocked by #N' checklist line is not also read as stale prose"

# 1c. A linked closing PR that has merged. #9274: a closing PR answers "what
# closes this issue", not "what blocks it", so it is no blocker reference.
# With nothing else cited the issue is undocumented, never stale.
set_population '[{"number":190,"title":"Shipped behind a merged PR"}]'
issue_fixture 190 "No prose blocker here." '[]' '[{"number":4743}]'
state_fixture pr 4743 "MERGED"
run_check
assert_eq "0" "$LAST_RC" "T1j: a merged closing PR still exits 0"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" \
    "T1k: a merged linked closing PR is NOT reported as a stale block"
assert_not_contains "$LAST_STDERR" "closing PR" \
    "T1k2: a merged linked closing PR is not cited as a stale signal"
assert_contains "$LAST_STDERR" "UNDOCUMENTED BLOCK" \
    "T1k3: with only a closing PR linked, the block is undocumented"

# 1d. #8927's Test Plan edge case: several references, only one closed.
set_population '[{"number":181,"title":"Partially unblocked"}]'
issue_fixture 181 "Blocked by #7. Depends on #9."
state_fixture issue 9 "OPEN"
run_check
assert_contains "$LAST_STDERR" "STALE BLOCK" \
    "T1l: a partially resolved reference set is reported (premise's own any-not-OPEN rule)"

# 1e. #8927's other Test Plan edge case: the only blocker mention is in a
# comment, not the body — extract-refs reads non-bot comments too.
set_population '[{"number":182,"title":"Blocker recorded in a comment"}]'
issue_fixture 182 "No reference in the body at all." \
    '[{"author":{"login":"a-human"},"body":"Blocked by #7 for now."}]'
run_check
assert_contains "$LAST_STDERR" "STALE BLOCK" \
    "T1m: a comment-only blocker reference is read, and its closure reported"
assert_not_contains "$LAST_STDERR" "UNDOCUMENTED BLOCK" \
    "T1n: a comment-only reference counts as documented"

# --- Group 2: an undocumented block -----------------------------------------
echo "Group 2: undocumented blocks"

# #8927's #180 row: loom:blocked carrying no blocker reference anywhere.
set_population '[{"number":180,"title":"Behavioural classification"}]'
issue_fixture 180 "This needs more thought before we can proceed."
run_check
assert_eq "0" "$LAST_RC" "T2a: an undocumented block still exits 0"
assert_contains "$LAST_STDERR" "UNDOCUMENTED BLOCK" \
    "T2b: loom:blocked with no parseable reference is reported as undocumented"
assert_contains "$LAST_STDERR" "#180" "T2c: names the undocumented issue"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" \
    "T2d: an undocumented block is not also reported as stale"
assert_contains "$LAST_STDERR" "Blocked by: #N" \
    "T2e: the remedy names the machine-checkable form to record"
assert_contains "$LAST_STDERR" "park-record render --blocked-by" \
    "T2e2: the remedy names the command that renders that form (#8925)"

# A bot's own re-check comment must not count as documentation — that is the
# #4507 self-perpetuating loop extract-refs already closes, inherited here.
set_population '[{"number":183,"title":"Only the bot mentioned a blocker"}]'
issue_fixture 183 "Nothing cited." \
    '[{"author":{"login":"loom-fleet-dispatch"},"body":"Blocked by #7, per the last pass."}]'
run_check
assert_contains "$LAST_STDERR" "UNDOCUMENTED BLOCK" \
    "T2f: a reference only the automation identity wrote does not count as documentation"

# --- Group 3: genuinely still blocked — reports nothing ---------------------
echo "Group 3: genuinely still blocked"

# The blocker is DECLARED in a park record, not merely mentioned in prose: since
# #8925 a prose-only park is reported regardless of its verdict, so "reports
# nothing" is only reachable for a block whose reference a parser can read. The
# prose-only half of that contract is T3i-T3k below.
set_population '[{"number":200,"title":"Waiting on a live dependency"}]'
issue_fixture 200 "Blocked by #9 (still in flight).

<!-- loom:park Blocked by: #9 by=curator at=2026-09-20T00:00:00Z -->"
state_fixture issue 9 "OPEN"
run_check
assert_eq "0" "$LAST_RC" "T3a: a genuine block exits 0"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" "T3b: an open blocker is not reported as stale"
assert_not_contains "$LAST_STDERR" "UNDOCUMENTED BLOCK" "T3c: an open blocker is not reported as undocumented"
assert_not_contains "$LAST_STDERR" "WARNING" "T3d: nothing at all is written to stderr for a genuine block"
assert_contains "$LAST_STDOUT" "no stale, superseded, undocumented or prose-only" \
    "T3e: the clear case prints the one-line stdout confirmation"

run_check --quiet
assert_eq "" "$LAST_STDOUT" "T3f: --quiet suppresses the stdout confirmation"
assert_eq "0" "$LAST_RC" "T3g: --quiet still exits 0"

# An open closing PR carrying a block label: `dep-recheck`'s own VERDICT would
# be `blocked`, which is a fact about the PR. The ISSUE's block is not stale
# while that PR is open, and reaching for that verdict here is the obvious
# wrong shortcut.
set_population '[{"number":201,"title":"Closing PR under review"}]'
issue_fixture 201 "See the linked PR." '[]' '[{"number":4744}]'
jq -n '{number: 4744, state: "OPEN", labels: [{"id":"x","name":"loom:changes-requested","color":"ABCDEF"}], mergeable: "CONFLICTING", mergeStateStatus: "CONFLICTING"}' \
    >"$STUB_DIR/pr-4744.json"
run_check
assert_not_contains "$LAST_STDERR" "STALE BLOCK" \
    "T3h: an open (even conflicting, even block-labelled) closing PR is not a stale block"

# The same live dependency, cited only in prose: reported REGARDLESS of the
# still-blocked verdict (#8925). A correct park whose blocker no parser can read
# is the #8314/#8852 shape — waiting for it to go stale is what kept #8852 parked.
set_population '[{"number":202,"title":"Parked in prose only"}]'
issue_fixture 202 "Blocked by #9 — noted here, never declared."
state_fixture issue 9 "OPEN"
run_check
assert_eq "0" "$LAST_RC" "T3i: a prose-only park still exits 0"
assert_contains "$LAST_STDERR" "PROSE-ONLY PARK" \
    "T3j: a cited blocker with no park record is reported as prose-only"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" \
    "T3k: a prose-only park whose blocker is still open is not also stale"

# An empty population — the healthy repo.
echo "Group 4: empty population and failed reads"
set_population '[]'
run_check
assert_eq "0" "$LAST_RC" "T4a: no open loom:blocked issues exits 0"
assert_contains "$LAST_STDOUT" "no stale, superseded, undocumented or prose-only" \
    "T4b: an empty population is the clear case"
assert_eq "" "$LAST_STDERR" "T4c: an empty population writes nothing to stderr"

# --- Group 4: a forge read that does not answer -----------------------------

# An issue in the population with no fixture at all: every read for it fails.
set_population '[{"number":999,"title":"Unreadable"}]'
rm -f "$STUB_DIR/issue-999.json"
run_check
assert_eq "0" "$LAST_RC" "T4d: a failed forge read still exits 0"
assert_contains "$LAST_STDERR" "NOT EVALUATED" \
    "T4e: an unreadable issue is its own category, not folded into clear or stale"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" "T4f: a failed read never produces a stale verdict"
assert_not_contains "$LAST_STDERR" "UNDOCUMENTED BLOCK" \
    "T4g: a failed read never produces an undocumented verdict (it is UNKNOWN, not absent)"

# No `gh` on PATH at all: the enumeration itself cannot run.
rc=0
out="$(PATH="/usr/bin:/bin" "$SCRIPT" --repo owner/repo --repo-root "$STUB_DIR" 2>&1)" || rc=$?
assert_eq "0" "$rc" "T4h: exits 0 when \`gh\` cannot be run at all"
assert_contains "$out" "could not enumerate" "T4i: says the enumeration failed rather than reporting clear"

# --- Group 5: --json -------------------------------------------------------
echo "Group 5: --json"
set_population '[{"number":178,"title":"Comment moderation"},{"number":180,"title":"No reason given"}]'
issue_fixture 178 "Blocked by #7 (user authentication)."
issue_fixture 180 "This needs more thought before we can proceed."
run_check --json
assert_eq "0" "$LAST_RC" "T5a: --json exits 0"
assert_eq "178" "$(jq -r '.stale[0].number' <<<"$LAST_STDOUT")" "T5b: --json lists the stale issue"
assert_eq "180" "$(jq -r '.undocumented[0].number' <<<"$LAST_STDOUT")" "T5c: --json lists the undocumented issue"
assert_eq "0" "$(jq -r '.unevaluated | length' <<<"$LAST_STDOUT")" "T5d: --json reports nothing unevaluated here"

# --- Group 6: the PR population (#8925) ------------------------------------
# The enumeration this suite had no stub case for at all, which is how the
# missing `gh pr list` case above surfaced as five unrelated failures rather
# than as absent coverage.
echo "Group 6: the PR population"

# pr_fixture <number> <body> <state> [labels-json] [mergeable] [mergeStateStatus]
# One file answers every `gh pr view` the check makes for a PR: `body,comments`
# for the reference read, and `number,state,labels,mergeable,mergeStateStatus`
# for the PR's own superseding-block gate.
pr_fixture() {
    jq -n --argjson n "$1" --arg body "$2" --arg state "$3" \
          --argjson labels "${4:-[]}" \
          --arg mergeable "${5:-MERGEABLE}" \
          --arg mss "${6:-CLEAN}" \
          '{number: $n, body: $body, comments: [], state: $state, labels: $labels,
            mergeable: $mergeable, mergeStateStatus: $mss}' \
        >"$STUB_DIR/pr-$1.json"
}

# A parked PR whose declared blocker has closed, and which can otherwise land:
# the unblock path #8925 adds. #8314's own shape.
set_population '[]'
set_pr_population '[{"number":8314,"title":"Parked PR with a cleared blocker"}]'
pr_fixture 8314 "<!-- loom:park Blocked by: #8322 by=doctor at=2026-09-19T12:09:00Z -->" "OPEN"
state_fixture issue 8322 "CLOSED"
run_check
assert_eq "0" "$LAST_RC" "T6a: the PR population is enumerated and exits 0"
assert_contains "$LAST_STDERR" "STALE BLOCK" \
    "T6b: a parked PR whose declared blocker closed is reported as a stale block"
assert_contains "$LAST_STDERR" "PR #8314" \
    "T6c: the finding names the artifact kind, not a bare number ambiguous across both populations"
assert_not_contains "$LAST_STDERR" "PROSE-ONLY PARK" \
    "T6d: a park record in the PR body is a declaration, not a prose-only park"

# The same PR, now carrying `loom:operator-only`: the #4634/#7267 superseding
# gate downgrades the unblock to held rather than clearing it.
pr_fixture 8314 "<!-- loom:park Blocked by: #8322 by=doctor at=2026-09-19T12:09:00Z -->" "OPEN" \
    '[{"id":"x","name":"loom:operator-only","color":"ABCDEF"}]'
run_check
assert_contains "$LAST_STDERR" "SUPERSEDED BLOCK" \
    "T6e: a PR held by its own label is superseded, not ready to unpark"
assert_not_contains "$LAST_STDERR" "STALE BLOCK" \
    "T6f: a superseded PR is not also reported as a plain stale block"

# `loom:changes-requested` is deliberately NOT in the self-block set: it is the
# ordinary review cycle, not a pending human decision.
pr_fixture 8314 "<!-- loom:park Blocked by: #8322 by=doctor at=2026-09-19T12:09:00Z -->" "OPEN" \
    '[{"id":"x","name":"loom:changes-requested","color":"ABCDEF"}]'
run_check
assert_contains "$LAST_STDERR" "STALE BLOCK" \
    "T6g: a review-cycle label does not supersede the PR's cleared blocker"

# --no-prs skips the PR enumeration entirely.
run_check --no-prs
assert_eq "" "$LAST_STDERR" "T6h: --no-prs does not enumerate the PR population"

# --- Group 7: the batched read shape (#10480) -------------------------------
# Three issues citing the same blocker, plus a parked PR: one listing, one
# GraphQL query for all the issues, one state read for the shared blocker, and
# never a per-artifact `gh issue view` / `gh pr view`.
echo "Group 7: batched reads"
set_population '[{"number":301,"title":"a"},{"number":302,"title":"b"},{"number":303,"title":"c"}]'
set_pr_population '[]'
for n in 301 302 303; do issue_fixture "$n" "Blocked by #7."; done
state_fixture issue 7 "CLOSED"
run_check --json
CALLS="$(cat "$STUB_DIR/calls.log")"
assert_eq "3" "$(jq -r '.stale | length' <<<"$LAST_STDOUT")" "T7a: every citer of the closed blocker is stale"
assert_not_contains "$CALLS" "issue view" "T7b: no per-artifact \`gh issue view\`"
assert_not_contains "$CALLS" "pr view" "T7c: no per-artifact \`gh pr view\`"
assert_eq "1" "$(grep -c '^api graphql' <<<"$CALLS")" "T7d: one GraphQL query for the whole issue population"
assert_eq "1" "$(grep -cE 'repos/owner/repo/issues/7( |$)' <<<"$CALLS")" \
    "T7e: a blocker cited three times is read once"
assert_eq "1" "$(grep -cE 'repos/owner/repo/issues\?labels=loom:blocked' <<<"$CALLS")" \
    "T7f: one REST listing covers both populations"

# A REST bot login (`name[bot]`) is the automation's own comment, exactly as the
# GraphQL spelling was (T2f).
set_population '[{"number":304,"title":"Only the bot (REST spelling) mentioned a blocker"}]'
issue_fixture 304 "Nothing cited." \
    '[{"author":{"login":"loom-fleet-dispatch[bot]"},"body":"Blocked by #7, per the last pass."}]'
run_check
assert_contains "$LAST_STDERR" "UNDOCUMENTED BLOCK" \
    "T7g: a \`[bot]\`-suffixed fleet comment does not count as documentation"

# --- Group 8: the budget floor and forge_cost (#10480) ----------------------
echo "Group 8: budget floor"
set_population '[{"number":178,"title":"Comment moderation"}]'
set_pr_population '[]'
issue_fixture 178 "Blocked by #7 (user authentication)."
state_fixture issue 7 "CLOSED"
printf '{"core":5000,"graphql":500}' >"$STUB_DIR/rate_limit.json"
run_check --json
CALLS="$(cat "$STUB_DIR/calls.log")"
assert_eq "0" "$LAST_RC" "T8a: a run refused by the budget floor still exits 0"
assert_eq "1" "$(jq -r '.unevaluated | length' <<<"$LAST_STDOUT")" \
    "T8b: the refused artifact is reported not evaluated, never clear"
assert_contains "$(jq -r '.unevaluated[0].reason' <<<"$LAST_STDOUT")" \
    "budget floor: graphql remaining 500, projected 1, floor 1000" "T8c: the reason names the floor"
assert_contains "$(jq -r '.forge_cost.budget_refused' <<<"$LAST_STDOUT")" "budget floor" \
    "T8d: forge_cost.budget_refused records the refusal"
assert_eq "0" "$(grep -c '^api graphql' <<<"$CALLS")" "T8e: no closing-reference query is sent"
assert_eq "0" "$(grep -cE 'repos/owner/repo/issues/7( |$)' <<<"$CALLS")" "T8f: no blocker is read"

run_check
assert_contains "$LAST_STDERR" "NOT EVALUATED" "T8g: the human report lists the refused artifact"
assert_contains "$LAST_STDERR" "forge cost: graphql 0 queries" "T8h: the stderr cost line is printed"

run_check --json --min-graphql-remaining 0
assert_eq "1" "$(jq -r '.stale | length' <<<"$LAST_STDOUT")" "T8i: a floor of 0 disables the check"
assert_eq "1" "$(jq -r '.forge_cost.graphql_queries' <<<"$LAST_STDOUT")" "T8j: forge_cost counts the query"
assert_eq "1" "$(jq -r '.forge_cost.graphql_points' <<<"$LAST_STDOUT")" \
    "T8k: forge_cost takes the points from rateLimit.cost"
assert_eq "500" "$(jq -r '.forge_cost.budget_before.graphql_remaining' <<<"$LAST_STDOUT")" \
    "T8l: forge_cost records the probe's reading"
assert_eq "1" "$(jq -r '.forge_cost.rest_requests' <<<"$LAST_STDOUT")" \
    "T8m: forge_cost counts the REST blocker read"

# Overwritten rather than removed: an empty fixture is unparseable, so both
# probe legs fail exactly as with no fixture at all.
: >"$STUB_DIR/rate_limit.json"
run_check --json
assert_eq "1" "$(jq -r '.stale | length' <<<"$LAST_STDOUT")" \
    "T8n: a probe that does not answer never refuses the run"
assert_eq "null" "$(jq -c '.forge_cost.budget_before' <<<"$LAST_STDOUT")" \
    "T8o: an unanswered probe is reported as budget_before: null"

HELP="$("$SCRIPT" --help 2>&1)"
assert_contains "$HELP" "--min-graphql-remaining" "T8p: --help documents --min-graphql-remaining"
assert_contains "$HELP" "--min-core-remaining" "T8q: --help documents --min-core-remaining"

# --- summary ---------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
