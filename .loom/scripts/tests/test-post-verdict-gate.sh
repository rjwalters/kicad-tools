#!/usr/bin/env bash
# test-post-verdict-gate.sh - post-verdict.sh end to end against the REAL
# `loom-daemon forge verdict-gate` / `verdict-labels` (#10581), with a stateful
# stub `gh` that models one PR's comments and labels.
#
# Covers #10581's test plan:
#   - same-head contradiction (changes-requested, then approve) is refused
#   - the same with an --overrules-prior rationale is allowed
#   - loom:ci-failure blocks an approve
#   - label exclusivity after each verdict
#   - a label write failure after the post exits non-zero with a repair hint
#   - a head that moved since the changes-requested verdict approves normally
#   - a second identical verdict at the same head is deduped (no 2nd comment)
#
# test-post-verdict.sh pins the shell wiring with a mock daemon; the decision
# rules are unit-tested in loom-daemon/src/verdict_gate/tests.rs. This suite is
# the evidence the two halves agree.

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
POST_VERDICT="$SCRIPTS_DIR/post-verdict.sh"
# shellcheck source=lib/write-scope-fixture.sh
source "$TEST_DIR/lib/write-scope-fixture.sh"
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "forge"

PASSED=0
FAILED=0
pass() { PASSED=$((PASSED + 1)); echo "  PASS: $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  FAIL: $1"; [[ -n "${2:-}" ]] && printf '    %s\n' "$2"; }
check() { if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1" "expected '$2', got '$3'"; fi; }
contains() { if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1" "missing '$3' in: ${2:0:600}"; fi; }

command -v jq >/dev/null 2>&1 || { echo "FATAL: jq is required" >&2; exit 2; }

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export LOOM_VERDICT_LOCK_DIR="$STUB_DIR/locks"

# --- Stateful stub gh: one PR (owner/repo), comments.json + labels.txt -------
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="$LOOM_TEST_STUB_DIR"
args=("$@")
method=GET path="" body="" adds=()
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    -X) method="${args[i + 1]}" ;;
    -f) [[ "${args[i + 1]}" == labels\[\]=* ]] && adds+=("${args[i + 1]#labels[]=}") ;;
    --body) body="${args[i + 1]}" ;;
    repos/*) path="${args[i]}" ;;
  esac
done
if [[ "${1:-}" == "api" ]]; then
  # the #10485 final head compare reads exactly pulls/<N> (not /reviews, /comments)
  if [[ "$method" == GET && "$path" =~ ^repos/owner/repo/pulls/[0-9]+$ ]]; then cat "$D/cur-sha"; exit 0; fi
  case "$method $path" in
    "GET repos/owner/repo/issues/"*/comments)
      cat "$D/comments.json"; exit 0 ;;
    "GET repos/owner/repo/issues/"*/labels\?*)
      [[ -f "$D/labels-read-fail" ]] && { echo "HTTP 502" >&2; exit 1; }
      jq -R '{name: .}' < "$D/labels.txt" | jq -s .; exit 0 ;;
    "POST repos/owner/repo/issues/"*/labels)
      [[ -f "$D/labels-post-fail" || -f "$D/labels-write-fail" ]] && { echo "HTTP 403" >&2; exit 1; }
      for l in "${adds[@]}"; do grep -qxF "$l" "$D/labels.txt" || echo "$l" >> "$D/labels.txt"; done
      echo '[]'; exit 0 ;;
    "DELETE repos/owner/repo/issues/comments/"*)
      jq --argjson id "${path##*/}" 'map(select(.id != $id))' "$D/comments.json" > "$D/c.tmp" && mv "$D/c.tmp" "$D/comments.json"; exit 0 ;;
    "DELETE repos/owner/repo/issues/"*/labels/*)
      [[ -f "$D/labels-write-fail" ]] && { echo "HTTP 403" >&2; exit 1; }
      l="${path##*/}"; l="${l//%3A/:}"
      grep -qxF "$l" "$D/labels.txt" || { echo "HTTP 404" >&2; exit 1; }
      grep -vxF "$l" "$D/labels.txt" > "$D/labels.tmp"; mv "$D/labels.tmp" "$D/labels.txt"; exit 0 ;;
  esac
  # The #7647 review gate's reads: a clean, empty review state.
  if [[ "${2:-}" == "graphql" ]]; then
    printf '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}\n'
  fi
  exit 0
fi
if [[ "${1:-} ${2:-}" == "repo view" ]]; then echo "owner/repo"; exit 0; fi
# The legacy changes-requested path (#10581 round 2): one `gh pr edit`.
if [[ "${1:-} ${2:-}" == "pr edit" ]]; then
  [[ -f "$D/labels-write-fail" ]] && { echo "HTTP 403" >&2; exit 1; }
  for ((i = 3; i < ${#args[@]}; i++)); do
    case "${args[i]}" in
      --add-label) grep -qxF "${args[i + 1]}" "$D/labels.txt" || echo "${args[i + 1]}" >> "$D/labels.txt" ;;
      --remove-label) grep -vxF "${args[i + 1]}" "$D/labels.txt" > "$D/labels.tmp"; mv "$D/labels.tmp" "$D/labels.txt" ;;
    esac
  done
  exit 0
fi
if [[ "${1:-} ${2:-}" == "issue comment" ]]; then
  [[ -f "$D/post-delay" ]] && sleep 2
  # A rival Judge on ANOTHER host passed its gate read before either of us wrote
  # (the host lock cannot order it); its comment lands just before ours.
  if [[ -f "$D/rival-verdict" ]]; then
    jq --arg b "rival review\n\n<!-- loom:verdict-sha sha=$(cat "$D/cur-sha") verdict=$(cat "$D/rival-verdict") -->" --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '. + [{id: ((map(.id // 0) | max // 0) + 1), body: $b, created_at: $t, user: {login: "rival-judge", type: "User"}, author_association: "MEMBER"}]' \
      "$D/comments.json" > "$D/c.tmp" && mv "$D/c.tmp" "$D/comments.json"
  fi
  echo "$3" >> "$D/posted.log"
  jq --arg b "$body" --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '. + [{id: ((map(.id // 0) | max // 0) + 1), body: $b, created_at: $t, user: {login: "a-judge", type: "User"}, author_association: "MEMBER"}]' \
    "$D/comments.json" > "$D/c.tmp" && mv "$D/c.tmp" "$D/comments.json"
  echo "https://github.com/owner/repo/pull/$3#issuecomment-1"; exit 0
fi
echo "stub gh: unhandled: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export PATH="$STUB_DIR:$PATH"

# The comment itself goes over the gh ladder (pinned by test-post-verdict.sh);
# the gate and label verbs are the real binary's, through LOOM_DAEMON_BIN.
printf '#!/usr/bin/env bash\nexit 127\n' > "$STUB_DIR/self-daemon"
chmod +x "$STUB_DIR/self-daemon"
export LOOM_DAEMON_SELF_BIN="$STUB_DIR/self-daemon"
# #10485: approvals also pass the exact-head CI gate. Its reader is not under
# test here, so a wrapper answers `forge wait-checks` GREEN on the SHA under test
# (cur-sha) and hands every other verb to the real binary.
REAL_DAEMON="${LOOM_DAEMON_BIN:-loom-daemon}"
cat > "$STUB_DIR/daemon" <<WRAP
#!/usr/bin/env bash
[[ "\${1:-} \${2:-}" == "forge wait-checks" ]] && { echo "LOOM-CHECKS-GREEN \$(cat "$STUB_DIR/cur-sha")"; exit 0; }
exec "$REAL_DAEMON" "\$@"
WRAP
chmod +x "$STUB_DIR/daemon"
export LOOM_DAEMON_BIN="$STUB_DIR/daemon"
write_scope_register "$STUB_DIR/checkout" owner/repo
cd "$STUB_DIR/checkout" || exit 2

HEAD=846ed44c14e7dd4e87bf17efb554bca0d57c05b2
printf %s "$HEAD" > "$STUB_DIR/cur-sha"
MOVED=ed058db8c14e7dd4e87bf17efb554bca0d57c05b
OVERRULE="the flaky check was re-run green on this head; the size concern was withdrawn upstream"

# state COMMENTS_JSON LABEL...
state() {
  printf '%s' "$1" > "$STUB_DIR/comments.json"; shift
  printf '%s\n' "$@" | sed '/^$/d' > "$STUB_DIR/labels.txt"
  rm -f "$STUB_DIR"/labels-*-fail "$STUB_DIR/posted.log" "$STUB_DIR/rival-verdict"
}
cr_at() { # a trusted changes-requested marker for $1, posted $2 seconds ago
  local t; t="$(date -u -d "@$(($(date +%s) - $2))" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$(($(date +%s) - $2))" +%Y-%m-%dT%H:%M:%SZ)"
  jq -n --arg s "$1" --arg t "$t" \
    '[{body: ("Changes requested.\n\n<!-- loom:verdict-sha sha=" + $s + " verdict=changes-requested -->"), created_at: $t, user: {login: "a-judge", type: "User"}, author_association: "MEMBER"}]'
}
pv() { printf %s "$3" > "$STUB_DIR/cur-sha"; OUT="$("$POST_VERDICT" "$@" 2>&1)"; RC=$?; LABELS="$(tr '\n' ' ' < "$STUB_DIR/labels.txt")"; POSTED="$(cat "$STUB_DIR/posted.log" 2>/dev/null)"; }

echo "Testing post-verdict.sh + forge verdict-gate/verdict-labels (#10581)..."

echo "== same-head contradiction is refused (the #10578 shape) =="
state "$(cr_at "$HEAD" 360)" loom:changes-requested loom:reviewing
pv 10578 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 7" 7 "$RC"
contains "names the overrule flag" "$OUT" "--overrules-prior"
check "nothing posted" "" "$POSTED"
contains "labels untouched" "$LABELS" "loom:changes-requested"

echo "== same-head contradiction with an overrule rationale is allowed =="
state "$(cr_at "$HEAD" 3600)" loom:changes-requested loom:reviewing
pv 10578 approved "$HEAD" --body "Approved: covered by the new tests." --overrules-prior "$OVERRULE"
check "exit 0" 0 "$RC"
check "comment posted" 10578 "$POSTED"
check "exactly loom:pr survives" "loom:pr " "$LABELS"

echo "== loom:ci-failure blocks an approve =="
state "[]" loom:review-requested loom:ci-failure
pv 10579 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 7" 7 "$RC"
contains "names the label" "$OUT" "loom:ci-failure"
check "nothing posted" "" "$POSTED"

echo "== a moved head approves normally; approve is exclusive =="
state "$(cr_at "$MOVED" 60)" loom:changes-requested loom:reviewing loom:review-requested loom:operator-priority
pv 10580 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 0" 0 "$RC"
check "loom:pr only, unrelated labels kept" "loom:operator-priority loom:pr " "$LABELS"

echo "== changes-requested is exclusive, keeps loom:ci-failure =="
state "[]" loom:pr loom:reviewing loom:ci-failure
pv 10581 changes-requested "$HEAD" --body "Please fix the failing unit test."
check "exit 0" 0 "$RC"
check "loom:changes-requested, never with loom:pr" "loom:ci-failure loom:changes-requested " "$LABELS"

echo "== a second identical verdict at the same head is deduped =="
rm -f "$STUB_DIR/posted.log"   # keep the first verdict's comment; reset only the post log
pv 10581 changes-requested "$HEAD" --body "Please fix (second Judge)."
check "exit 0" 0 "$RC"
check "no second comment" "" "$POSTED"
contains "says why" "$OUT" "not posting a duplicate"
check "one verdict comment on the PR" 1 "$(jq length "$STUB_DIR/comments.json")"
contains "labels still applied" "$LABELS" "loom:changes-requested"

echo "== label write failure after the post is loud (#10605) =="
state "[]" loom:review-requested loom:reviewing
touch "$STUB_DIR/labels-write-fail"
pv 10605 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 8" 8 "$RC"
check "the comment was posted" 10605 "$POSTED"
contains "repair command" "$OUT" "Repair: gh pr edit 10605 --repo owner/repo --add-label \"loom:pr\""

echo "== a failed label add leaves the queue/claim labels in place =="
state "[]" loom:review-requested loom:reviewing
touch "$STUB_DIR/labels-post-fail"
pv 10607 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 8" 8 "$RC"
contains "queue label kept" "$LABELS" "loom:review-requested"
contains "claim label kept" "$LABELS" "loom:reviewing"

echo "== the overrule rationale is published in the approval =="
state "$(cr_at "$HEAD" 360)" loom:changes-requested loom:reviewing
pv 10608 approved "$HEAD" --body "Approved: covered by the new tests." --overrules-prior "$OVERRULE"
check "exit 0" 0 "$RC"
contains "rationale in the posted body" "$(jq -r '.[-1].body' "$STUB_DIR/comments.json")" "$OVERRULE"

echo "== an unreadable label state never passes an approval =="
state "[]" loom:review-requested
touch "$STUB_DIR/labels-read-fail"
pv 10606 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 7" 7 "$RC"
check "nothing posted" "" "$POSTED"

echo "== concurrent identical callers post at most one verdict (#10581) =="
state "[]" loom:review-requested loom:reviewing
touch "$STUB_DIR/post-delay"
"$POST_VERDICT" 10700 changes-requested "$HEAD" --body "Please fix the failing test (A)." >/dev/null 2>&1 &
PA=$!
"$POST_VERDICT" 10700 changes-requested "$HEAD" --body "Please fix the failing test (B)." >/dev/null 2>&1 &
PB=$!
wait "$PA"; RA=$?; wait "$PB"; RB=$?
rm -f "$STUB_DIR/post-delay"
check "both callers exit 0" "0 0" "$RA $RB"
check "one verdict comment on the PR" 1 "$(jq length "$STUB_DIR/comments.json")"
check "lock released" "" "$(ls "$STUB_DIR/locks" 2>/dev/null)"

echo "== a stale approval cannot interleave with a rival verdict (#10581) =="
state "[]" loom:review-requested loom:reviewing
printf %s "$HEAD" > "$STUB_DIR/cur-sha"
mkdir -p "$STUB_DIR/locks/owner_repo-10701"   # a verdict transaction in flight
"$POST_VERDICT" 10701 approved "$HEAD" --body "Approved: covered by the new tests." >/dev/null 2>&1 &
PA=$!
sleep 2
check "approval waits on the lock, posts nothing" "" "$(cat "$STUB_DIR/posted.log" 2>/dev/null)"
printf '%s' "$(cr_at "$HEAD" 1)" > "$STUB_DIR/comments.json"   # the rival's rejection lands meanwhile
printf 'loom:changes-requested\n' > "$STUB_DIR/labels.txt"
rmdir "$STUB_DIR/locks/owner_repo-10701"
wait "$PA"; RA=$?
check "approval re-gated under the lock and refused" 7 "$RA"
contains "labels still the rejection's" "$(tr '\n' ' ' < "$STUB_DIR/labels.txt")" "loom:changes-requested"
check "nothing posted by the approval" "" "$(cat "$STUB_DIR/posted.log" 2>/dev/null)"

echo "== a held lock fails closed =="
state "[]" loom:review-requested
mkdir -p "$STUB_DIR/locks/owner_repo-10702"
LOOM_VERDICT_LOCK_WAIT_SECS=2 pv 10702 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 9" 9 "$RC"
check "nothing posted" "" "$POSTED"
check "foreign lock left alone" yes "$([[ -d "$STUB_DIR/locks/owner_repo-10702" ]] && echo yes)"
rmdir "$STUB_DIR/locks/owner_repo-10702"

# A binary that predates the #10581 verbs, answering the way an older clap
# build does (exit 2, "unrecognized subcommand"); every other verb is the real one.
cat > "$STUB_DIR/old-daemon" <<OLD
#!/usr/bin/env bash
case "\${1:-} \${2:-}" in
  "--version "*) echo "loom-daemon 0.19.870 (pre-10581)"; exit 0 ;;
  "forge wait-checks") echo "LOOM-CHECKS-GREEN \$(cat "$STUB_DIR/cur-sha")"; exit 0 ;;
  "forge verdict-"*) printf "error: unrecognized subcommand '%s'\n\nUsage: loom-daemon forge <COMMAND>\n" "\$2" >&2; exit 2 ;;
esac
exec "$REAL_DAEMON" "\$@"
OLD
chmod +x "$STUB_DIR/old-daemon"

echo "== a daemon without the verdict verbs posts an approval on the legacy path (round 4) =="
state "[]" loom:review-requested loom:reviewing loom:changes-requested loom:ci-failure
LOOM_DAEMON_BIN="$STUB_DIR/old-daemon" pv 10720 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 0 (the legacy approval posts)" 0 "$RC"
contains "loud warning" "$OUT" "legacy path"
contains "names the binary" "$OUT" "$STUB_DIR/old-daemon"
contains "names its --version" "$OUT" "loom-daemon 0.19.870 (pre-10581)"
contains "names the missing verbs" "$OUT" "forge verdict-lock forge verdict-gate forge verdict-labels forge verdict-reconcile"
contains "says roll the daemon" "$OUT" "roll loom-daemon to a build that includes #10684"
check "comment posted" 10720 "$POSTED"
contains "verdict marker on the posted body" "$(jq -r '.[-1].body' "$STUB_DIR/comments.json")" "verdict=approved -->"
check "loom:pr added; loom:changes-requested and loom:ci-failure left alone" "loom:changes-requested loom:ci-failure loom:pr " "$LABELS"
contains "a rival rejection's label survives, so merge-pr.sh's #8112 guard sees the contradiction" "$LABELS" "loom:changes-requested"
check "no lock taken" "" "$(ls "$STUB_DIR/locks" 2>/dev/null)"

echo "== the legacy approval still refuses a moved head (exit 5) =="
state "[]" loom:review-requested
printf %s "$HEAD" > "$STUB_DIR/cur-sha"
OUT="$(LOOM_DAEMON_BIN="$STUB_DIR/old-daemon" "$POST_VERDICT" 10723 approved "$MOVED" --body "Approved: covered by the new tests." 2>&1)"; RC=$?
check "exit 5" 5 "$RC"
check "nothing posted" "" "$(cat "$STUB_DIR/posted.log" 2>/dev/null)"
check "labels untouched" "loom:review-requested " "$(tr '\n' ' ' < "$STUB_DIR/labels.txt")"

echo "== a daemon without the verdict verbs still posts changes-requested (legacy path) =="
state "[]" loom:pr loom:reviewing loom:review-requested loom:ci-failure
LOOM_DAEMON_BIN="$STUB_DIR/old-daemon" pv 10721 changes-requested "$HEAD" --body "Please fix the failing unit test."
check "exit 0" 0 "$RC"
contains "loud warning" "$OUT" "legacy path"
check "comment posted" 10721 "$POSTED"
contains "verdict marker on the posted body" "$(jq -r '.[-1].body' "$STUB_DIR/comments.json")" "verdict=changes-requested -->"
check "exclusive changes-requested labels, loom:ci-failure kept" "loom:ci-failure loom:changes-requested " "$LABELS"

echo "== the legacy path's label failure is loud (exit 8, repair command) =="
state "[]" loom:pr loom:reviewing
touch "$STUB_DIR/labels-write-fail"
LOOM_DAEMON_BIN="$STUB_DIR/old-daemon" pv 10722 changes-requested "$HEAD" --body "Please fix the failing unit test."
check "exit 8" 8 "$RC"
check "the comment was posted" 10722 "$POSTED"
contains "repair command" "$OUT" "Repair: gh pr edit 10722 --repo owner/repo --add-label loom:changes-requested --remove-label loom:pr"

echo "== cross-host race: a rival changes-requested lands after our gate read (#10581) =="
state "[]" loom:review-requested loom:reviewing
printf changes-requested > "$STUB_DIR/rival-verdict"
pv 10710 approved "$HEAD" --body "Approved: covered by the new tests."
check "the losing approval exits 7" 7 "$RC"
contains "says it was superseded" "$OUT" "SUPERSEDED"
check "only the changes-requested label remains" "loom:changes-requested " "$LABELS"
LAST="$(jq -r '.[-1].body' "$STUB_DIR/comments.json")"
contains "newest marker is changes-requested" "$LAST" "verdict=changes-requested -->"

echo "== cross-host race: a rival approval lands after our changes-requested gate read =="
state "[]" loom:review-requested loom:reviewing
printf approved > "$STUB_DIR/rival-verdict"
pv 10711 changes-requested "$HEAD" --body "Please fix the failing unit test."
check "changes-requested exits 0" 0 "$RC"
check "changes-requested labels, never loom:pr" "loom:changes-requested " "$LABELS"

echo "== cross-host race: an identical approval lands first (lower comment id) =="
state "[]" loom:review-requested loom:reviewing
printf approved > "$STUB_DIR/rival-verdict"
pv 10713 approved "$HEAD" --body "Approved: covered by the new tests."
check "the duplicate exits 0" 0 "$RC"
contains "says it landed first" "$OUT" "landed first"
check "exactly one approval comment stands" 1 "$(jq '[.[] | select(.body | contains("verdict=approved -->"))] | length' "$STUB_DIR/comments.json")"
check "the survivor is the rival's (lowest id)" rival-judge "$(jq -r '.[0].user.login' "$STUB_DIR/comments.json")"
check "the duplicate touched no labels" "loom:review-requested loom:reviewing " "$LABELS"

echo "== cross-host race: an identical changes-requested lands first =="
state "[]" loom:review-requested loom:reviewing
printf changes-requested > "$STUB_DIR/rival-verdict"
pv 10714 changes-requested "$HEAD" --body "Please fix the failing unit test."
check "the duplicate exits 0" 0 "$RC"
check "exactly one changes-requested comment stands" 1 "$(jq '[.[] | select(.body | contains("verdict=changes-requested -->"))] | length' "$STUB_DIR/comments.json")"

echo "== no race: an unrelated earlier verdict at another head changes nothing =="
state "$(cr_at "$MOVED" 60)" loom:review-requested loom:reviewing
pv 10712 approved "$HEAD" --body "Approved: covered by the new tests."
check "exit 0" 0 "$RC"
check "loom:pr only" "loom:pr " "$LABELS"

echo "== #9258: a body that is no rationale is refused by the real forge verdict-body-check =="
printf '@-' > "$STUB_DIR/body-at-dash"; printf '@/tmp/x' > "$STUB_DIR/body-at-path"
for spelling in "--body -" "--body    " "--body @-" "--body-file $STUB_DIR/body-at-dash" "--body-file $STUB_DIR/body-at-path" "--body Approved."; do
  state "[]" loom:review-requested loom:reviewing
  read -r flag value <<< "$spelling"; [[ "$spelling" == "--body    " ]] && value="   "
  pv 10900 approved "$HEAD" "$flag" "$value"
  check "$spelling: exit 2" 2 "$RC"
  check "$spelling: nothing posted" "" "$POSTED"
  check "$spelling: labels untouched" "loom:review-requested loom:reviewing " "$LABELS"
done
state "[]" loom:review-requested loom:reviewing
OUT="$(printf '@-' | "$POST_VERDICT" 10901 approved "$HEAD" --body-file - 2>&1)"; RC=$?
check "--body-file - (stdin '@-'): exit 2" 2 "$RC"
contains "--body-file - (stdin '@-'): names the lone @ token" "$OUT" "lone '@' token"
check "--body-file - (stdin '@-'): nothing posted" "" "$(cat "$STUB_DIR/posted.log" 2>/dev/null)"
state "[]" loom:review-requested loom:reviewing
pv 10902 approved "$HEAD" --body "@reviewer this looks good because the tests cover the fix"
check "@mention prose still posts" 0 "$RC"
check "@mention prose: comment posted" 10902 "$POSTED"

echo ""
echo "test-post-verdict-gate: $PASSED passed, $FAILED failed"
[[ "$FAILED" -eq 0 ]]
