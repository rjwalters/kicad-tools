#!/usr/bin/env bash
# test-verdict-staleness-guard.sh - Unit tests for verdict-staleness-guard.sh
# (#5686).
#
# verdict-staleness-guard.sh binds a PR's terminal review verdict
# (`loom:pr` / `loom:changes-requested`) to the head SHA it was rendered
# against, via the `<!-- loom:verdict-sha sha=... verdict=... -->` marker, and
# invalidates the verdict when the head SHA moves. It exists because a verdict
# used to outlive the tree it described: on rjwalters/repo#192 a correct
# `loom:changes-requested` survived a rebase+force-push that made CI green,
# and nothing re-queued the PR until an operator cleared the label by hand.
#
# This is a black-box test: the guard is a full CLI script (no functions to
# source), so `gh` is stubbed on PATH and the real script is invoked as a
# subprocess, asserting on stdout / exit code / the stub's recorded writes.
# Real `jq` is used unstubbed — its marker-extraction filter is exactly the
# logic under test. Mirrors the stubbing pattern in test-judge-fallback-cap.sh.
#
# Usage:
#   ./.loom/scripts/tests/test-verdict-staleness-guard.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
GUARD="$SCRIPTS_DIR/verdict-staleness-guard.sh"

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
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

if [[ ! -x "$GUARD" ]]; then
    echo -e "${RED}FATAL${NC}: $GUARD not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------
#   gh pr view <N> --json headRefOid,labels,state
#                                           -> cat $STUB_DIR/pr-<N>.json
#                                              (fails if pr-view-fail-<N> exists)
#   gh api repos/{owner}/{repo}/issues/<N>/comments --paginate
#                                           -> cat $STUB_DIR/comments-<N>.json (or "[]")
#                                              (fails if comments-fail-<N> exists)
#   gh pr comment <N> --body <b>            -> append to $STUB_DIR/comment-writes.log
#                                              (fails if comment-fail-<N> exists)
#   gh pr edit <N> ...                      -> append to $STUB_DIR/edit-writes.log
#                                              (fails if edit-fail-<N> exists)
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
case "$1" in
  pr)
    case "$2" in
      view)
        pr_num="$3"
        if [[ -f "$STUB_DIR_FROM_ENV/pr-view-fail-$pr_num" ]]; then
          echo "stub gh: pr view failed" >&2
          exit 1
        fi
        # Simulate `gh` emitting incidental content to stderr on a SUCCESSFUL
        # call (update-notifier banner, rate-limit hint) — the guard must
        # parse only stdout, never merge this into the JSON.
        if [[ -f "$STUB_DIR_FROM_ENV/pr-view-stderr-$pr_num" ]]; then
          echo "gh: A new release of gh is available: 2.0.0 -> 2.1.0" >&2
        fi
        canned="$STUB_DIR_FROM_ENV/pr-$pr_num.json"
        if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"headRefOid":"0000000000000000000000000000000000000000","state":"OPEN","merged":false,"labels":[]}'; fi
        exit 0
        ;;
      comment)
        pr_num="$3"
        if [[ -f "$STUB_DIR_FROM_ENV/comment-fail-$pr_num" ]]; then
          echo "stub gh: pr comment failed" >&2
          exit 1
        fi
        printf 'COMMENT %s %s\n' "$pr_num" "$*" >> "$STUB_DIR_FROM_ENV/comment-writes.log"
        exit 0
        ;;
      edit)
        pr_num="$3"
        if [[ -f "$STUB_DIR_FROM_ENV/edit-fail-$pr_num" ]]; then
          echo "stub gh: pr edit failed" >&2
          exit 1
        fi
        printf 'EDIT %s %s\n' "$pr_num" "$*" >> "$STUB_DIR_FROM_ENV/edit-writes.log"
        exit 0
        ;;
    esac
    echo "stub gh: unhandled pr args: $*" >&2
    exit 3
    ;;
  api)
    path="$2"
    if [[ "$path" == repos/*/issues/*/comments ]]; then
      num="${path#repos/*/issues/}"
      num="${num%/comments}"
      if [[ -f "$STUB_DIR_FROM_ENV/comments-fail-$num" ]]; then
        echo "stub gh: comments fetch failed" >&2
        exit 1
      fi
      if [[ -f "$STUB_DIR_FROM_ENV/comments-stderr-$num" ]]; then
        echo "gh: request took longer than expected" >&2
      fi
      canned="$STUB_DIR_FROM_ENV/comments-$num.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo "[]"; fi
      exit 0
    fi
    echo "stub gh: unhandled api args: $*" >&2
    exit 3
    ;;
  *)
    echo "stub gh: unhandled args: $*" >&2
    exit 3
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

SHA_A="1111111111111111111111111111111111111111"
SHA_B="2222222222222222222222222222222222222222"
SHA_C="3333333333333333333333333333333333333333"

labels_json() {
    # labels_json [label ...] -> the `labels` array body
    local labels="" l
    for l in "$@"; do
        [[ -n "$labels" ]] && labels="$labels,"
        labels="$labels{\"name\":\"$l\"}"
    done
    printf '%s' "$labels"
}

pr_json_state() {
    # pr_json_state <pr-number> <head-sha> <state> <merged:true|false> [label ...]
    local num="$1" sha="$2" state="$3" merged="$4"; shift 4
    printf '{"headRefOid":"%s","state":"%s","merged":%s,"labels":[%s]}' \
        "$sha" "$state" "$merged" "$(labels_json "$@")" > "$STUB_DIR/pr-$num.json"
}

pr_json() {
    # pr_json <pr-number> <head-sha> [label ...] — an OPEN, unmerged PR (the
    # only shape that may reach the FRESH/STALE comparison since #6781).
    local num="$1" sha="$2"; shift 2
    pr_json_state "$num" "$sha" "OPEN" "false" "$@"
}

pr_json_no_state() {
    # pr_json_no_state <pr-number> <head-sha> [label ...] — the pre-#6781 shape
    # a forge shim that does not report `state`/`merged` would return.
    local num="$1" sha="$2"; shift 2
    printf '{"headRefOid":"%s","labels":[%s]}' "$sha" "$(labels_json "$@")" > "$STUB_DIR/pr-$num.json"
}

pr_json_state_no_merged() {
    # pr_json_state_no_merged <pr-number> <head-sha> <state> [label ...] — the
    # REAL shape `gh pr view --json headRefOid,state,labels` returns: `state` is
    # populated and the `merged` key is entirely ABSENT (not `false`). Every
    # other helper here sets `merged` explicitly, which no real `gh` invocation
    # ever does now that the unsupported field has been dropped from the
    # request — so this is the only helper that exercises the production shape.
    local num="$1" sha="$2" state="$3"; shift 3
    printf '{"headRefOid":"%s","state":"%s","labels":[%s]}' \
        "$sha" "$state" "$(labels_json "$@")" > "$STUB_DIR/pr-$num.json"
}

verdict_comment() {
    # verdict_comment <created_at> <sha> <approved|changes-requested>
    printf '{"created_at":"%s","body":"Reviewed.\\n\\n<!-- loom:verdict-sha sha=%s verdict=%s -->"}' "$1" "$2" "$3"
}

plain_comment() {
    printf '{"created_at":"%s","body":"%s"}' "$1" "$2"
}

reset_state() {
    rm -f "$STUB_DIR"/pr-*.json "$STUB_DIR"/comments-*.json
    rm -f "$STUB_DIR"/pr-view-fail-* "$STUB_DIR"/comments-fail-*
    rm -f "$STUB_DIR"/pr-view-stderr-* "$STUB_DIR"/comments-stderr-*
    rm -f "$STUB_DIR"/comment-fail-* "$STUB_DIR"/edit-fail-*
    rm -f "$STUB_DIR"/comment-writes.log "$STUB_DIR"/edit-writes.log
}

run_guard() {
    OUT="$("$GUARD" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log" 2>/dev/null || true)"
    WRITES="$(cat "$STUB_DIR/edit-writes.log" 2>/dev/null || true)"
    COMMENTS_POSTED="$(cat "$STUB_DIR/comment-writes.log" 2>/dev/null || true)"
}

get_field() {
    printf '%s\n' "$1" | grep "^$2=" | head -n1 | cut -d= -f2-
}

echo "Testing verdict-staleness-guard.sh..."

# (a) No terminal verdict label at all -> NO_VERDICT, exit 10.
reset_state
pr_json 200 "$SHA_A" "loom:review-requested" "loom:reviewing"
run_guard 200
assert_eq "10" "$RC" "(a) No verdict label -> exit 10"
assert_eq "NO_VERDICT" "$(get_field "$OUT" DECISION)" "(a) DECISION=NO_VERDICT"
assert_eq "" "$(get_field "$OUT" VERDICT_LABEL)" "(a) VERDICT_LABEL empty"

# (b) changes-requested verdict, marker SHA == head SHA -> FRESH, exit 0.
reset_state
pr_json 201 "$SHA_A" "loom:changes-requested"
{ echo "["; verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"; echo "]"; } > "$STUB_DIR/comments-201.json"
run_guard 201
assert_eq "0" "$RC" "(b) Marker SHA == head SHA -> exit 0"
assert_eq "FRESH" "$(get_field "$OUT" DECISION)" "(b) DECISION=FRESH"
assert_eq "$SHA_A" "$(get_field "$OUT" MARKER_SHA)" "(b) MARKER_SHA reported"

# (b2) Regression: an unchanged-head PR must never be re-queued, even with
#      --clear (no spurious label writes on ordinary re-runs / comment-only
#      activity).
reset_state
pr_json 202 "$SHA_A" "loom:changes-requested"
{
  echo "["
  verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"
  echo ","
  plain_comment "2026-08-08T02:30:00Z" "Some later discussion, no push."
  echo "]"
} > "$STUB_DIR/comments-202.json"
run_guard 202 --clear
assert_eq "0" "$RC" "(b2) Unchanged head with --clear -> still exit 0"
assert_eq "FRESH" "$(get_field "$OUT" DECISION)" "(b2) DECISION=FRESH"
assert_eq "" "$WRITES" "(b2) No label writes on a fresh verdict"
assert_eq "" "$COMMENTS_POSTED" "(b2) No comment posted on a fresh verdict"

# (c) THE #5686 INCIDENT: changes-requested rendered at SHA_A, branch rebased
#     and force-pushed to SHA_B -> STALE, cleared, re-queued, comment names
#     both SHAs.
reset_state
pr_json 203 "$SHA_B" "loom:changes-requested" "loom:ci-failure"
{ echo "["; verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"; echo "]"; } > "$STUB_DIR/comments-203.json"
run_guard 203 --clear
assert_eq "12" "$RC" "(c) Head moved after changes-requested -> exit 12"
assert_eq "STALE" "$(get_field "$OUT" DECISION)" "(c) DECISION=STALE"
assert_eq "1" "$(get_field "$OUT" CLEARED)" "(c) CLEARED=1"
assert_eq "$SHA_A" "$(get_field "$OUT" MARKER_SHA)" "(c) MARKER_SHA is the pre-force-push SHA"
assert_eq "$SHA_B" "$(get_field "$OUT" HEAD_SHA)" "(c) HEAD_SHA is the post-force-push SHA"
assert_contains "$WRITES" "--remove-label loom:changes-requested" "(c) Stale verdict label removed"
assert_contains "$WRITES" "--add-label loom:review-requested" "(c) PR returned to the review queue"
assert_contains "$WRITES" "--remove-label loom:ci-failure" "(c) Per-tree companion label removed too"
assert_contains "$COMMENTS_POSTED" "$SHA_A" "(c) Audit comment names the OLD SHA"
assert_contains "$COMMENTS_POSTED" "$SHA_B" "(c) Audit comment names the NEW SHA"
assert_contains "$COMMENTS_POSTED" "loom:verdict-stale from=$SHA_A to=$SHA_B" "(c) Audit comment carries the transition marker"

# (d) THE DANGEROUS DIRECTION: an approving loom:pr verdict rendered at SHA_A,
#     head force-pushed to SHA_C -> STALE, approval cleared, re-queued.
reset_state
pr_json 204 "$SHA_C" "loom:pr"
{ echo "["; verdict_comment "2026-08-08T03:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-204.json"
run_guard 204 --clear
assert_eq "12" "$RC" "(d) Head moved after approval -> exit 12"
assert_eq "loom:pr" "$(get_field "$OUT" VERDICT_LABEL)" "(d) VERDICT_LABEL=loom:pr"
assert_eq "1" "$(get_field "$OUT" CLEARED)" "(d) Stale approval CLEARED=1"
assert_contains "$WRITES" "--remove-label loom:pr" "(d) Stale approval removed (never silently merged)"
assert_contains "$WRITES" "--add-label loom:review-requested" "(d) PR returned to the review queue"

# (d2) Same stale approval WITHOUT --clear -> still reported STALE, but no
#      writes at all (report-only mode is genuinely read-only).
reset_state
pr_json 205 "$SHA_C" "loom:pr"
{ echo "["; verdict_comment "2026-08-08T03:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-205.json"
run_guard 205
assert_eq "12" "$RC" "(d2) Report-only stale verdict -> exit 12"
assert_eq "0" "$(get_field "$OUT" CLEARED)" "(d2) CLEARED=0 without --clear"
assert_eq "" "$WRITES" "(d2) No label writes without --clear"
assert_eq "" "$COMMENTS_POSTED" "(d2) No comment without --clear"

# (e) Pre-migration PR: verdict label present, NO marker comment at all ->
#     UNVERIFIABLE (exit 11), fail safe. Must NOT force-clear on rollout.
reset_state
pr_json 206 "$SHA_B" "loom:pr"
{ echo "["; plain_comment "2026-08-01T00:00:00Z" "LGTM, approving."; echo "]"; } > "$STUB_DIR/comments-206.json"
run_guard 206 --clear
assert_eq "11" "$RC" "(e) Verdict with no marker -> exit 11"
assert_eq "UNVERIFIABLE" "$(get_field "$OUT" DECISION)" "(e) DECISION=UNVERIFIABLE"
assert_eq "" "$WRITES" "(e) Fail safe: no label writes for an unmarked verdict"
assert_eq "" "$COMMENTS_POSTED" "(e) Fail safe: no comment for an unmarked verdict"

# (f) Verdict-kind filtering: the PR was rejected at SHA_A (marked), then
#     approved at SHA_B (marked), and head is still SHA_B. The newest marker
#     matching the CURRENTLY-HELD loom:pr label is the SHA_B approval -> FRESH.
#     A naive "newest marker of any kind" reader would also get this right;
#     (f2) is the case that separates them.
reset_state
pr_json 207 "$SHA_B" "loom:pr"
{
  echo "["
  verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"
  echo ","
  verdict_comment "2026-08-08T03:10:00Z" "$SHA_B" "approved"
  echo "]"
} > "$STUB_DIR/comments-207.json"
run_guard 207 --clear
assert_eq "0" "$RC" "(f) Newest matching-kind marker equals head -> exit 0"
assert_eq "FRESH" "$(get_field "$OUT" DECISION)" "(f) DECISION=FRESH after a reject-then-approve history"
assert_eq "" "$WRITES" "(f) No writes when the current verdict is fresh"

# (f2) Verdict-kind filtering, the discriminating case: the newest marker of
#      ANY kind is an `approved` marker at the CURRENT head, but the PR now
#      carries loom:changes-requested (written by a host still on the older,
#      unmarked prompt). Matching on kind yields UNVERIFIABLE (fail safe);
#      ignoring kind would have wrongly reported FRESH and blessed a verdict
#      nothing recorded.
reset_state
pr_json 208 "$SHA_B" "loom:changes-requested"
{
  echo "["
  verdict_comment "2026-08-08T03:10:00Z" "$SHA_B" "approved"
  echo "]"
} > "$STUB_DIR/comments-208.json"
run_guard 208 --clear
assert_eq "11" "$RC" "(f2) Marker of the wrong verdict kind -> exit 11 (UNVERIFIABLE)"
assert_eq "UNVERIFIABLE" "$(get_field "$OUT" DECISION)" "(f2) DECISION=UNVERIFIABLE, not FRESH"
assert_eq "" "$WRITES" "(f2) No writes on UNVERIFIABLE"

# (g) Explicit operator/Champion hold: a STALE verdict on a loom:blocked PR is
#     still reported STALE, but --clear is suppressed so the hold is not
#     silently undone.
reset_state
pr_json 209 "$SHA_B" "loom:changes-requested" "loom:blocked"
{ echo "["; verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"; echo "]"; } > "$STUB_DIR/comments-209.json"
run_guard 209 --clear
assert_eq "12" "$RC" "(g) Stale verdict on a held PR -> still exit 12"
assert_eq "0" "$(get_field "$OUT" CLEARED)" "(g) CLEARED=0 on an explicit hold"
assert_contains "$OUT" "clear suppressed" "(g) REASON explains the suppression"
assert_eq "" "$WRITES" "(g) No label writes on a held PR"

# (g2) Same for loom:operator-only.
reset_state
pr_json 210 "$SHA_B" "loom:pr" "loom:operator-only"
{ echo "["; verdict_comment "2026-08-08T03:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-210.json"
run_guard 210 --clear
assert_eq "12" "$RC" "(g2) Stale approval on loom:operator-only -> exit 12"
assert_eq "0" "$(get_field "$OUT" CLEARED)" "(g2) CLEARED=0 on loom:operator-only"
assert_eq "" "$WRITES" "(g2) No label writes on loom:operator-only"

# (h) Idempotency: the old->new transition was already announced by an earlier
#     pass (daemon backstop and a Judge pass can both notice the same move).
#     Labels are still (re-)written, but no duplicate comment is posted.
reset_state
pr_json 211 "$SHA_B" "loom:changes-requested"
{
  echo "["
  verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"
  echo ","
  plain_comment "2026-08-08T02:56:00Z" "<!-- loom:verdict-stale from=$SHA_A to=$SHA_B --> already announced"
  echo "]"
} > "$STUB_DIR/comments-211.json"
run_guard 211 --clear
assert_eq "12" "$RC" "(h) Already-announced transition -> exit 12"
assert_eq "1" "$(get_field "$OUT" CLEARED)" "(h) Labels still cleared"
assert_eq "" "$COMMENTS_POSTED" "(h) No duplicate audit comment"
assert_contains "$WRITES" "--remove-label loom:changes-requested" "(h) Label transition still applied"

# (i) Contradictory verdict state (both loom:pr and loom:changes-requested):
#     the approving label is the dangerous one, so it is what gets reasoned
#     about.
reset_state
pr_json 212 "$SHA_B" "loom:pr" "loom:changes-requested"
{
  echo "["
  verdict_comment "2026-08-08T03:00:00Z" "$SHA_A" "approved"
  echo "]"
} > "$STUB_DIR/comments-212.json"
run_guard 212
assert_eq "loom:pr" "$(get_field "$OUT" VERDICT_LABEL)" "(i) Approving label takes precedence in a contradictory state"
assert_eq "12" "$RC" "(i) Stale approval detected despite the contradictory state"

# (i2) #7018: the SAME contradictory state, but with --clear. Only the
#      DETECTED label (loom:pr) must not be the only one removed — the stray
#      loom:changes-requested standing alongside it must be stripped too, or
#      it would be left behind next to the freshly re-added
#      loom:review-requested, reproducing the exact mutual-exclusion
#      violation this guard exists to prevent (PR #6817 incident).
reset_state
pr_json 239 "$SHA_C" "loom:pr" "loom:changes-requested"
{ echo "["; verdict_comment "2026-08-08T03:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-239.json"
run_guard 239 --clear
assert_eq "12" "$RC" "(i2) Stale approval detected despite the contradictory state"
assert_eq "1" "$(get_field "$OUT" CLEARED)" "(i2) CLEARED=1"
assert_contains "$WRITES" "--remove-label loom:pr" "(i2) Detected stale loom:pr removed"
assert_contains "$WRITES" "--remove-label loom:changes-requested" "(i2) Stray loom:changes-requested ALSO removed, not left behind (#7018)"
assert_contains "$WRITES" "--add-label loom:review-requested" "(i2) PR returned to the review queue"

# (j) Bad args: non-numeric PR number -> usage error, exit 1.
reset_state
run_guard not-a-number
assert_eq "1" "$RC" "(j) Non-numeric PR number -> exit 1"
assert_contains "$ERR" "numeric PR number is required" "(j) stderr explains the usage error"

# (k) `gh pr view` failure -> exit 1 (environment error). Callers must not read
#     this as "verdict is fine".
reset_state
touch "$STUB_DIR/pr-view-fail-213"
run_guard 213
assert_eq "1" "$RC" "(k) gh pr view failure -> exit 1"
assert_contains "$ERR" "gh pr view" "(k) stderr names the failing gh call"

# (k2) `gh api .../comments` failure -> exit 1, and no writes attempted.
reset_state
pr_json 214 "$SHA_B" "loom:pr"
touch "$STUB_DIR/comments-fail-214"
run_guard 214 --clear
assert_eq "1" "$RC" "(k2) gh api comments failure -> exit 1"
assert_eq "" "$WRITES" "(k2) No label writes when the comment fetch failed"

# (l) Benign stderr chatter on BOTH successful gh calls: the guard must parse
#     stdout only. A merged-stream bug would zero the marker list and silently
#     downgrade a real STALE verdict to UNVERIFIABLE (i.e. leave a stale
#     approval standing) — the same class of failure #5455 found in
#     judge-fallback-guard.sh.
reset_state
pr_json 215 "$SHA_B" "loom:pr"
{ echo "["; verdict_comment "2026-08-08T03:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-215.json"
touch "$STUB_DIR/pr-view-stderr-215"
touch "$STUB_DIR/comments-stderr-215"
run_guard 215
assert_eq "12" "$RC" "(l) Stale verdict still detected despite stderr chatter on both calls"
assert_eq "$SHA_A" "$(get_field "$OUT" MARKER_SHA)" "(l) MARKER_SHA parsed from stdout only"
assert_eq "$SHA_B" "$(get_field "$OUT" HEAD_SHA)" "(l) HEAD_SHA parsed from stdout only"

# (m) Label-write failure during --clear -> exit 1 with CLEARED=0, so the
#     caller retries rather than believing the PR was re-queued.
reset_state
pr_json 216 "$SHA_B" "loom:changes-requested"
{ echo "["; verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"; echo "]"; } > "$STUB_DIR/comments-216.json"
touch "$STUB_DIR/edit-fail-216"
run_guard 216 --clear
assert_eq "1" "$RC" "(m) Label-write failure -> exit 1"
assert_eq "0" "$(get_field "$OUT" CLEARED)" "(m) CLEARED=0 when the label write failed"
assert_contains "$ERR" "failed to clear" "(m) stderr names the failed clear"

# (m2) Comment-write failure during --clear -> exit 1 and NO label write, so
#      the transition is never applied without its audit trail.
reset_state
pr_json 217 "$SHA_B" "loom:changes-requested"
{ echo "["; verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"; echo "]"; } > "$STUB_DIR/comments-217.json"
touch "$STUB_DIR/comment-fail-217"
run_guard 217 --clear
assert_eq "1" "$RC" "(m2) Comment-write failure -> exit 1"
assert_eq "" "$WRITES" "(m2) Labels untouched when the audit comment could not be posted"

# (n) Ordinary new commits (not a rebase) also invalidate the verdict — the
#     guard deliberately has no force-push-vs-fast-forward detector, because
#     an appended commit is just as much "not the tree I reviewed".
reset_state
pr_json 218 "$SHA_C" "loom:changes-requested"
{
  echo "["
  verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"
  echo ","
  plain_comment "2026-08-08T02:40:00Z" "Pushed one more commit on top."
  echo "]"
} > "$STUB_DIR/comments-218.json"
run_guard 218
assert_eq "12" "$RC" "(n) Appended commit also invalidates the verdict -> exit 12"
assert_not_contains "$OUT" "DECISION=FRESH" "(n) Not reported FRESH"

# --- #6319: --anchor, the UNVERIFIABLE remediation -------------------------
#
# The gap: the marker exists only because judge.md ASKS the model to append it
# at ~19 separate verdict-write sites, and production dropped it on roughly
# one verdict in four. Every dropped marker leaves a verdict label standing
# that nothing can ever invalidate — the full pre-#5686 hazard, silently, for
# the life of the label. Until #6319 the unmarked path had exactly one test
# ((e) above) and no remediation at all.

# (o) An unmarked verdict with --anchor -> ANCHORED (exit 13): a marker is
#     posted for the CURRENT head, and — critically — NO label is written.
#     Anchoring is not a verdict; it only makes the standing verdict checkable.
reset_state
pr_json 219 "$SHA_B" "loom:pr"
{ echo "["; plain_comment "2026-08-15T00:00:00Z" "LGTM, approving."; echo "]"; } > "$STUB_DIR/comments-219.json"
run_guard 219 --clear --anchor
assert_eq "13" "$RC" "(o) Unmarked verdict with --anchor -> exit 13"
assert_eq "ANCHORED" "$(get_field "$OUT" DECISION)" "(o) DECISION=ANCHORED"
assert_eq "1" "$(get_field "$OUT" ANCHORED)" "(o) ANCHORED=1"
assert_eq "0" "$(get_field "$OUT" CLEARED)" "(o) CLEARED=0 — anchoring is not a clear"
assert_eq "$SHA_B" "$(get_field "$OUT" MARKER_SHA)" "(o) MARKER_SHA is now the current head"
assert_eq "" "$WRITES" "(o) NO label writes — the verdict label is left exactly as it was"
assert_contains "$COMMENTS_POSTED" "<!-- loom:verdict-sha sha=$SHA_B verdict=approved -->" \
  "(o) Anchor comment carries the marker in the exact scanned format"
assert_contains "$COMMENTS_POSTED" "not** a review" "(o) Anchor comment disclaims being a review"

# (o2) Idempotency: the anchor comment is exactly what step 3 scans for, so a
#      second pass reads FRESH and never posts a duplicate. (A guard that
#      re-anchored every pass would comment-spam every unmarked verdict.)
reset_state
pr_json 220 "$SHA_B" "loom:pr"
{
  echo "["
  plain_comment "2026-08-15T00:00:00Z" "LGTM, approving."
  echo ","
  verdict_comment "2026-08-15T00:05:00Z" "$SHA_B" "approved"
  echo "]"
} > "$STUB_DIR/comments-220.json"
run_guard 220 --clear --anchor
assert_eq "0" "$RC" "(o2) Re-run after anchoring -> exit 0 (FRESH)"
assert_eq "FRESH" "$(get_field "$OUT" DECISION)" "(o2) DECISION=FRESH on the second pass"
assert_eq "" "$COMMENTS_POSTED" "(o2) No duplicate anchor comment"
assert_eq "" "$WRITES" "(o2) Still no label writes"

# (o3) --anchor is suppressed on an explicit hold, exactly like --clear: a PR
#      a human parked should not collect automated comments either.
reset_state
pr_json 221 "$SHA_B" "loom:pr" "loom:blocked"
{ echo "["; plain_comment "2026-08-15T00:00:00Z" "LGTM."; echo "]"; } > "$STUB_DIR/comments-221.json"
run_guard 221 --clear --anchor
assert_eq "11" "$RC" "(o3) Unmarked verdict on a held PR -> still exit 11"
assert_eq "UNVERIFIABLE" "$(get_field "$OUT" DECISION)" "(o3) DECISION stays UNVERIFIABLE on a hold"
assert_eq "0" "$(get_field "$OUT" ANCHORED)" "(o3) ANCHORED=0 on a hold"
assert_contains "$OUT" "anchor suppressed" "(o3) REASON explains the suppression"
assert_eq "" "$COMMENTS_POSTED" "(o3) No comment posted on a held PR"
assert_eq "" "$WRITES" "(o3) No label writes on a held PR"

# (o4) THE REGRESSION THAT MATTERS: a verdict that ALREADY carries a marker
#      must behave byte-for-byte as before --anchor existed. Passing --anchor
#      alongside --clear must not divert a STALE verdict into the anchor path
#      (which would re-bless a stale approval by stamping it at the new head).
reset_state
pr_json 222 "$SHA_C" "loom:pr"
{ echo "["; verdict_comment "2026-08-08T03:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-222.json"
run_guard 222 --clear --anchor
assert_eq "12" "$RC" "(o4) Marked stale approval with --anchor -> still exit 12 (STALE)"
assert_eq "1" "$(get_field "$OUT" CLEARED)" "(o4) Stale approval still cleared"
assert_eq "0" "$(get_field "$OUT" ANCHORED)" "(o4) ANCHORED=0 — a marked verdict is never anchored"
assert_eq "$SHA_A" "$(get_field "$OUT" MARKER_SHA)" "(o4) MARKER_SHA is still the ORIGINAL recorded SHA"
assert_contains "$WRITES" "--remove-label loom:pr" "(o4) Stale approval removed as before"
assert_not_contains "$COMMENTS_POSTED" "<!-- loom:verdict-sha sha=$SHA_C" \
  "(o4) No anchor marker stamped at the new head"

# (o5) --anchor on a FRESH verdict is a no-op (nothing to remediate).
reset_state
pr_json 223 "$SHA_A" "loom:changes-requested"
{ echo "["; verdict_comment "2026-08-08T02:22:00Z" "$SHA_A" "changes-requested"; echo "]"; } > "$STUB_DIR/comments-223.json"
run_guard 223 --anchor
assert_eq "0" "$RC" "(o5) --anchor on a fresh verdict -> exit 0"
assert_eq "0" "$(get_field "$OUT" ANCHORED)" "(o5) ANCHORED=0"
assert_eq "" "$COMMENTS_POSTED" "(o5) No comment on a fresh verdict"

# (o6) Anchor comment-write failure -> exit 1 with ANCHORED=0, so the caller
#      knows the verdict is STILL unverifiable rather than believing it was
#      remediated. No labels touched either way.
reset_state
pr_json 224 "$SHA_B" "loom:changes-requested"
{ echo "["; plain_comment "2026-08-15T00:00:00Z" "Please fix."; echo "]"; } > "$STUB_DIR/comments-224.json"
touch "$STUB_DIR/comment-fail-224"
run_guard 224 --clear --anchor
assert_eq "1" "$RC" "(o6) Anchor comment failure -> exit 1"
assert_eq "0" "$(get_field "$OUT" ANCHORED)" "(o6) ANCHORED=0 when the anchor write failed"
assert_eq "UNVERIFIABLE" "$(get_field "$OUT" DECISION)" "(o6) Still reported UNVERIFIABLE"
assert_eq "" "$WRITES" "(o6) No label writes when the anchor failed"

# (o7) Anchoring is per verdict KIND: the PR holds loom:changes-requested but
#      only an `approved` marker exists (the (f2) mixed-fleet case). The
#      anchor must be stamped under the CURRENTLY-HELD kind, or the verdict
#      stays unverifiable forever.
reset_state
pr_json 225 "$SHA_B" "loom:changes-requested"
{ echo "["; verdict_comment "2026-08-08T03:10:00Z" "$SHA_B" "approved"; echo "]"; } > "$STUB_DIR/comments-225.json"
run_guard 225 --anchor
assert_eq "13" "$RC" "(o7) Marker of the wrong kind is still unmarked -> anchored, exit 13"
assert_contains "$COMMENTS_POSTED" "<!-- loom:verdict-sha sha=$SHA_B verdict=changes-requested -->" \
  "(o7) Anchor stamped under the currently-held verdict kind"

# (o8) Default (no --anchor) is unchanged: an unmarked verdict is reported
#      UNVERIFIABLE and remediated by nothing. Existing callers that have not
#      opted in see byte-for-byte the pre-#6319 behavior.
reset_state
pr_json 226 "$SHA_B" "loom:pr"
{ echo "["; plain_comment "2026-08-15T00:00:00Z" "LGTM."; echo "]"; } > "$STUB_DIR/comments-226.json"
run_guard 226 --clear
assert_eq "11" "$RC" "(o8) No --anchor -> exit 11 as before"
assert_eq "0" "$(get_field "$OUT" ANCHORED)" "(o8) ANCHORED=0 without --anchor"
assert_eq "" "$COMMENTS_POSTED" "(o8) No comment without --anchor"
assert_eq "" "$WRITES" "(o8) No label writes without --anchor"

# --- #6781: NOT_OPEN, the merged/closed short-circuit ----------------------
#
# THE #6781 INCIDENT (2026-08-23, PR #6772): a sweep read PR #6772 as open and
# `loom:pr`, then waited out a GraphQL rate-limit reset for ~2.5 minutes. In
# that window the daemon's Champion merged it. The sweep then ran the guard
# with --clear; the guard never looked at `state`, compared the approval's
# marker SHA against the merged head SHA (different — a commit had landed
# between approval and merge), concluded STALE, and stripped `loom:pr` +
# re-added `loom:review-requested` on an ALREADY-MERGED PR.
#
# A merged PR's head SHA differing from the SHA its approval was rendered
# against is normal, not suspicious — so without the state check this misfires
# on the ordinary "completed externally" race (#4884), not on an exotic one.

# (p) The incident, reproduced: merged PR, approval marker at the pre-merge
#     SHA, --clear passed -> NOT_OPEN (exit 14) and NOTHING is written.
reset_state
pr_json_state 227 "$SHA_B" "MERGED" "true" "loom:pr"
{ echo "["; verdict_comment "2026-08-23T06:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-227.json"
run_guard 227 --clear
assert_eq "14" "$RC" "(p) Merged PR with a mismatched marker -> exit 14"
assert_eq "NOT_OPEN" "$(get_field "$OUT" DECISION)" "(p) DECISION=NOT_OPEN, not STALE"
assert_eq "" "$WRITES" "(p) No label writes on a merged PR"
assert_eq "" "$COMMENTS_POSTED" "(p) No comment posted on a merged PR"
assert_eq "0" "$(get_field "$OUT" CLEARED)" "(p) CLEARED=0"
assert_eq "0" "$(get_field "$OUT" ANCHORED)" "(p) ANCHORED=0"
assert_eq "loom:pr" "$(get_field "$OUT" VERDICT_LABEL)" "(p) VERDICT_LABEL still reported for the operator"
assert_eq "$SHA_B" "$(get_field "$OUT" HEAD_SHA)" "(p) HEAD_SHA reported"

# (p1) The specific criterion from #6781: `loom:review-requested` must NEVER be
#      added to a merged PR. Asserted directly against the recorded writes.
assert_not_contains "$WRITES" "loom:review-requested" "(p1) Merged PR is never re-queued for review"
assert_not_contains "$WRITES" "--remove-label loom:pr" "(p1) Merged PR's approval label is never stripped"

# (p2) Closed WITHOUT merging (state=CLOSED, merged=false): same short-circuit.
#      An abandoned PR is just as finished as a merged one.
reset_state
pr_json_state 228 "$SHA_C" "CLOSED" "false" "loom:changes-requested" "loom:ci-failure"
{ echo "["; verdict_comment "2026-08-23T06:00:00Z" "$SHA_A" "changes-requested"; echo "]"; } > "$STUB_DIR/comments-228.json"
run_guard 228 --clear
assert_eq "14" "$RC" "(p2) Closed-unmerged PR -> exit 14"
assert_eq "NOT_OPEN" "$(get_field "$OUT" DECISION)" "(p2) DECISION=NOT_OPEN"
assert_contains "$OUT" "closed without merging" "(p2) REASON distinguishes closed from merged"
assert_eq "" "$WRITES" "(p2) No label writes on a closed PR"
assert_eq "" "$COMMENTS_POSTED" "(p2) No comment posted on a closed PR"

# (p3) --anchor is short-circuited too: an unmarked verdict on a merged PR gets
#      no anchor comment. Anchoring a finished PR would be pure comment spam —
#      there is no future force-push left to catch.
reset_state
pr_json_state 229 "$SHA_B" "MERGED" "true" "loom:pr"
{ echo "["; plain_comment "2026-08-23T06:00:00Z" "LGTM."; echo "]"; } > "$STUB_DIR/comments-229.json"
run_guard 229 --clear --anchor
assert_eq "14" "$RC" "(p3) Unmarked verdict on a merged PR -> exit 14, not 13"
assert_eq "NOT_OPEN" "$(get_field "$OUT" DECISION)" "(p3) DECISION=NOT_OPEN, not ANCHORED"
assert_eq "" "$COMMENTS_POSTED" "(p3) No anchor comment on a merged PR"
assert_eq "" "$WRITES" "(p3) No label writes on a merged PR"

# (p4) NOT_OPEN outranks NO_VERDICT: a merged PR with no verdict label at all
#      still reports NOT_OPEN, so the state answer is never masked by the
#      label answer.
reset_state
pr_json_state 230 "$SHA_B" "MERGED" "true" "loom:review-requested"
run_guard 230 --clear
assert_eq "14" "$RC" "(p4) Merged PR with no verdict label -> exit 14, not 10"
assert_eq "NOT_OPEN" "$(get_field "$OUT" DECISION)" "(p4) DECISION=NOT_OPEN takes priority over NO_VERDICT"
assert_eq "" "$(get_field "$OUT" VERDICT_LABEL)" "(p4) VERDICT_LABEL empty"

# (p5) The short-circuit happens BEFORE the comments fetch: with the comment
#      API rigged to fail, a guard that still fetched comments would exit 1.
#      Exit 14 proves the finished PR costs exactly one API call.
reset_state
pr_json_state 231 "$SHA_B" "MERGED" "true" "loom:pr"
touch "$STUB_DIR/comments-fail-231"
run_guard 231 --clear
assert_eq "14" "$RC" "(p5) Short-circuits before the comments fetch"
assert_eq "" "$WRITES" "(p5) No label writes"

# (p6) REST-shaped state (a forge shim returning lowercase `closed` alongside
#      `merged: true`, the shape quoted in #6781) is recognized too.
reset_state
pr_json_state 232 "$SHA_B" "closed" "true" "loom:pr"
{ echo "["; verdict_comment "2026-08-23T06:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-232.json"
run_guard 232 --clear
assert_eq "14" "$RC" "(p6) Lowercase REST state -> exit 14"
assert_eq "" "$WRITES" "(p6) No label writes"

# (p7) merged=true with state still reported OPEN (a forge that reports the
#      merge before flipping the state) is not open either.
reset_state
pr_json_state 233 "$SHA_B" "OPEN" "true" "loom:pr"
{ echo "["; verdict_comment "2026-08-23T06:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-233.json"
run_guard 233 --clear
assert_eq "14" "$RC" "(p7) merged=true wins over a stale state=OPEN -> exit 14"
assert_eq "" "$WRITES" "(p7) No label writes"

# (p8) An OPEN, unmerged PR is completely unaffected: the stale approval is
#      still cleared and re-queued exactly as before #6781.
reset_state
pr_json_state 234 "$SHA_C" "OPEN" "false" "loom:pr"
{ echo "["; verdict_comment "2026-08-23T06:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-234.json"
run_guard 234 --clear
assert_eq "12" "$RC" "(p8) Open PR with a stale approval -> still exit 12"
assert_eq "1" "$(get_field "$OUT" CLEARED)" "(p8) Still cleared on an open PR"
assert_contains "$WRITES" "--add-label loom:review-requested" "(p8) Still re-queued on an open PR"

# (p9) Fail-open on a MISSING state field: a forge shim that reports neither
#      `state` nor `merged` keeps the pre-#6781 behavior. Failing closed here
#      would silently disable stale-verdict clearing on such a forge, which
#      reinstates the #5686 hazard (a stale approval standing on a LIVE PR) —
#      strictly worse than mislabeling PRs that are already finished.
reset_state
pr_json_no_state 235 "$SHA_C" "loom:pr"
{ echo "["; verdict_comment "2026-08-23T06:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-235.json"
run_guard 235 --clear
assert_eq "12" "$RC" "(p9) Absent state field -> pre-#6781 behavior (exit 12)"
assert_eq "1" "$(get_field "$OUT" CLEARED)" "(p9) Stale approval still cleared when state is unknown"

# (p10) The PRODUCTION shape, which no other case above reproduces: real
#       `gh pr view --json headRefOid,state,labels` populates `state` and omits
#       the `merged` key entirely, so `PR_MERGED` is permanently "false". Keying
#       the human-readable distinction on `PR_MERGED` alone therefore reports a
#       genuinely merged PR as "closed without merging". Every other MERGED case
#       here sets `"merged":true` in the stub and so passes against that bug too
#       — this one is what actually pins the NOT_OPEN_WHAT fix.
reset_state
pr_json_state_no_merged 236 "$SHA_B" "MERGED" "loom:pr"
{ echo "["; verdict_comment "2026-08-23T06:00:00Z" "$SHA_A" "approved"; echo "]"; } > "$STUB_DIR/comments-236.json"
run_guard 236 --clear
assert_eq "14" "$RC" "(p10) state=MERGED with no merged key -> exit 14"
assert_eq "NOT_OPEN" "$(get_field "$OUT" DECISION)" "(p10) DECISION=NOT_OPEN"
assert_contains "$OUT" "PR is merged" "(p10) REASON says merged, not closed-without-merging"
assert_not_contains "$OUT" "closed without merging" "(p10) REASON does not mislabel a merged PR"
assert_eq "" "$WRITES" "(p10) No label writes on a merged PR"

# (p11) The counterpart: state=CLOSED with the `merged` key likewise absent is
#       still reported as closed-without-merging, so (p10) is a real distinction
#       and not just "always say merged".
reset_state
pr_json_state_no_merged 237 "$SHA_C" "CLOSED" "loom:changes-requested"
{ echo "["; verdict_comment "2026-08-23T06:00:00Z" "$SHA_A" "changes-requested"; echo "]"; } > "$STUB_DIR/comments-237.json"
run_guard 237 --clear
assert_eq "14" "$RC" "(p11) state=CLOSED with no merged key -> exit 14"
assert_contains "$OUT" "closed without merging" "(p11) REASON still distinguishes an abandoned PR"
assert_eq "" "$WRITES" "(p11) No label writes on a closed PR"

# --- Summary -------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
