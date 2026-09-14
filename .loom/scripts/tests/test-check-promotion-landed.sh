#!/usr/bin/env bash
# test-check-promotion-landed.sh - Unit tests for check-promotion-landed.sh
# (#6862).
#
# check-promotion-landed.sh finds a single issue that carries a
# "Champion Review: APPROVED" verdict comment but is missing `loom:issue`
# (Step 3b's promotion write silently failing to land, as it did on issue
# #6464 for 6 days), and with --apply either completes the promotion by
# recovering the tier the original verdict named, or escalates to an
# operator when the tier cannot be safely recovered.
#
# This is a black-box test: the script is a full CLI (no functions to
# source), so `gh` is stubbed on PATH and the real script is invoked as a
# subprocess, asserting on stdout / exit code / the stub's recorded writes.
# Real `jq` is used unstubbed. Mirrors the stubbing pattern in
# test-verdict-staleness-guard.sh and test-check-evaluating-staleness.sh.
#
# Usage:
#   ./.loom/scripts/tests/test-check-promotion-landed.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SUT="$SCRIPTS_DIR/check-promotion-landed.sh"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (mirrors test-champion-critical-file-check.sh, #6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-issue-promo.md"

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

assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (missing literal in $file: $needle)"
    fi
}

if [[ ! -x "$SUT" ]]; then
    echo -e "${RED}FATAL${NC}: $SUT not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------
#   gh issue view <N> --json state,labels,comments -> cat $STUB_DIR/issue-<N>.json
#                                                       (fails if issue-fail-<N> exists)
#   gh issue view <N> --json labels                -> cat $STUB_DIR/verify-<N>.json
#                                                       (post-edit read-back; fails
#                                                        if verify-fail-<N> exists;
#                                                        falls back to issue-<N>.json's
#                                                        labels when no verify- fixture
#                                                        was staged, so tests that don't
#                                                        care about the read-back need
#                                                        no second fixture)
#   gh issue comment <N> --body <b>                 -> append to $STUB_DIR/comment-writes.log
#                                                       (fails if comment-fail-<N> exists)
#   gh issue edit <N> ...                           -> append to $STUB_DIR/edit-writes.log
#                                                       (fails if edit-fail-<N> exists)
#   gh api repos/{owner}/{repo}/issues/<N>/timeline --paginate
#                                                    -> cat $STUB_DIR/timeline-<N>.json (or
#                                                       "[]" when no fixture staged, i.e. the
#                                                       #6933 timeline check finds nothing and
#                                                       the pre-existing MISMATCH/COMPLETED/
#                                                       ESCALATED behavior is unchanged; fails
#                                                       if timeline-fail-<N> exists)
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
case "$1" in
  issue)
    case "$2" in
      view)
        num="$3"
        # Distinguish the initial combined read from the post-edit
        # read-back by the requested --json fields.
        json_fields=""
        for a in "$@"; do
          if [[ "$prev" == "--json" ]]; then json_fields="$a"; fi
          prev="$a"
        done
        if [[ "$json_fields" == "labels" ]]; then
          if [[ -f "$STUB_DIR_FROM_ENV/verify-fail-$num" ]]; then
            echo "stub gh: issue view (verify) failed" >&2
            exit 1
          fi
          canned="$STUB_DIR_FROM_ENV/verify-$num.json"
          # Every test that reaches the completing edit's read-back stages
          # its own verify-<N>.json (via stage_verify) -- the read-back is
          # only ever reached on the --apply + tier-recovered path, which
          # this suite always pairs with an explicit fixture. Default to
          # "still missing" (mirrors #6464's silent-drop) if one is somehow
          # absent, rather than guessing loom:issue landed.
          if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"labels":[]}'; fi
          exit 0
        fi
        if [[ -f "$STUB_DIR_FROM_ENV/issue-fail-$num" ]]; then
          echo "stub gh: issue view failed" >&2
          exit 1
        fi
        canned="$STUB_DIR_FROM_ENV/issue-$num.json"
        if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"state":"OPEN","labels":[],"comments":[]}'; fi
        exit 0
        ;;
      comment)
        num="$3"
        if [[ -f "$STUB_DIR_FROM_ENV/comment-fail-$num" ]]; then
          echo "stub gh: issue comment failed" >&2
          exit 1
        fi
        printf 'COMMENT %s %s\n' "$num" "$*" >> "$STUB_DIR_FROM_ENV/comment-writes.log"
        exit 0
        ;;
      edit)
        num="$3"
        if [[ -f "$STUB_DIR_FROM_ENV/edit-fail-$num" ]]; then
          echo "stub gh: issue edit failed" >&2
          exit 1
        fi
        printf 'EDIT %s %s\n' "$num" "$*" >> "$STUB_DIR_FROM_ENV/edit-writes.log"
        # Whether this edit "landed" is decided entirely by whatever the test
        # staged in verify-<N>.json for the subsequent read-back (see
        # stage_verify) -- this stub does not infer landing from the edit's
        # own args, matching the real-world failure mode under test (an edit
        # can exit 0 while its effect silently does not land, #6862).
        exit 0
        ;;
    esac
    echo "stub gh: unhandled issue args: $*" >&2
    exit 3
    ;;
  api)
    path="$2"
    if [[ "$path" == repos/*/issues/*/timeline ]]; then
      num="${path#repos/*/issues/}"
      num="${num%/timeline}"
      if [[ -f "$STUB_DIR_FROM_ENV/timeline-fail-$num" ]]; then
        echo "stub gh: timeline fetch failed" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/timeline-$num.json"
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

labels_json() {
    # labels_json [label ...] -> the `[{"name":...}, ...]` array body
    local labels="" l
    for l in "$@"; do
        [[ -n "$labels" ]] && labels="$labels,"
        labels="$labels{\"name\":\"$l\"}"
    done
    printf '[%s]' "$labels"
}

approved_comment() {
    # approved_comment <created_at> <goal-alignment-line>
    printf '{"createdAt":"%s","body":"**Champion Review: APPROVED**\\n\\nAll criteria passed.\\n\\n%s\\n\\n**Ready for Builder to claim.**"}' "$1" "$2"
}

plain_comment() {
    printf '{"createdAt":"%s","body":"%s"}' "$1" "$2"
}

# self_generated_comment <created_at> <marker> -- one of THIS SCRIPT's own
# follow-up comments (the completed/mismatch templates near the bottom of
# check-promotion-landed.sh). Both templates quote the literal phrase
# "Champion Review: APPROVED" in their own narrative text, which is exactly
# what let a prior run's own comment get mistaken for a fresh Champion
# verdict on issue #7287 (#7299).
self_generated_comment() {
    printf '{"createdAt":"%s","body":"<!-- champion:promotion-landed-%s -->\\n**Champion: reconciliation note**\\n\\nThis issue carried a `Champion Review: APPROVED` verdict comment, but loom:issue was handled by a prior pass."}' "$1" "$2"
}

issue_json() {
    # issue_json <state> <labels-json-array> <comments-json-array>
    printf '{"state":"%s","labels":%s,"comments":%s}' "$1" "$2" "$3"
}

# stage_verify <N> <labels-json-array> -- what the post-edit read-back sees.
stage_verify() {
    printf '{"labels":%s}' "$2" > "$STUB_DIR/verify-$1.json"
}

# labeled_event <created_at> -- a `labeled loom:issue` timeline event.
labeled_event() {
    printf '{"event":"labeled","created_at":"%s","label":{"name":"loom:issue"}}' "$1"
}

# stage_timeline <N> <events-json-array> -- what `gh api .../timeline` returns.
stage_timeline() {
    printf '%s' "$2" > "$STUB_DIR/timeline-$1.json"
}

reset_state() {
    rm -f "$STUB_DIR"/issue-*.json "$STUB_DIR"/verify-*.json "$STUB_DIR"/timeline-*.json
    rm -f "$STUB_DIR"/issue-fail-* "$STUB_DIR"/verify-fail-* "$STUB_DIR"/timeline-fail-*
    rm -f "$STUB_DIR"/comment-fail-* "$STUB_DIR"/edit-fail-*
    rm -f "$STUB_DIR"/comment-writes.log "$STUB_DIR"/edit-writes.log
}

run_sut() {
    OUT="$("$SUT" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log" 2>/dev/null || true)"
    EDITS="$(cat "$STUB_DIR/edit-writes.log" 2>/dev/null || true)"
    COMMENTS_POSTED="$(cat "$STUB_DIR/comment-writes.log" 2>/dev/null || true)"
}

get_field() {
    printf '%s\n' "$1" | grep "^$2=" | head -n1 | cut -d= -f2-
}

echo "Testing check-promotion-landed.sh..."

# (a) No APPROVED comment at all -> OK, exit 0, nothing written.
reset_state
issue_json "OPEN" "$(labels_json "loom:curated")" "[$(plain_comment "2026-08-20T00:00:00Z" "just a note")]" > "$STUB_DIR/issue-200.json"
run_sut --issue 200
assert_eq "0" "$RC" "(a) no APPROVED comment -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(a) DECISION=OK"
assert_eq "" "$EDITS" "(a) no label edit issued"
assert_eq "" "$COMMENTS_POSTED" "(a) no comment posted"

# (b) APPROVED comment present AND loom:issue already present -> OK, exit 0.
reset_state
issue_json "OPEN" "$(labels_json "loom:issue" "tier:maintenance")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance) - cleanup")]" \
  > "$STUB_DIR/issue-201.json"
run_sut --issue 201
assert_eq "0" "$RC" "(b) loom:issue already present -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(b) DECISION=OK"

# (c) Issue closed -> NOT_OPEN, exit 10, regardless of comment/label state.
reset_state
issue_json "CLOSED" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 1")]" \
  > "$STUB_DIR/issue-202.json"
run_sut --issue 202
assert_eq "10" "$RC" "(c) closed issue -> exit 10"
assert_eq "NOT_OPEN" "$(get_field "$OUT" DECISION)" "(c) DECISION=NOT_OPEN"

# (d) The #6464 shape: APPROVED comment present, loom:issue missing, no --apply
#     -> MISMATCH, exit 11, report-only (no writes).
reset_state
issue_json "OPEN" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance) - cleanup")]" \
  > "$STUB_DIR/issue-6464.json"
run_sut --issue 6464
assert_eq "11" "$RC" "(d) APPROVED-without-loom:issue, no --apply -> exit 11"
assert_eq "MISMATCH" "$(get_field "$OUT" DECISION)" "(d) DECISION=MISMATCH"
assert_eq "" "$EDITS" "(d) report-only: no label edit issued"
assert_eq "" "$COMMENTS_POSTED" "(d) report-only: no comment posted"

# (e) Same shape, WITH --apply, and the completing edit's read-back confirms
#     loom:issue landed -> COMPLETED, exit 12, tier recovered from the verdict
#     comment's Goal Alignment line, comment posted.
reset_state
issue_json "OPEN" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance) - cleanup")]" \
  > "$STUB_DIR/issue-6464.json"
stage_verify 6464 "$(labels_json "loom:issue" "tier:maintenance")"
run_sut --issue 6464 --apply
assert_eq "12" "$RC" "(e) --apply completes when the read-back confirms the label landed -> exit 12"
assert_eq "COMPLETED" "$(get_field "$OUT" DECISION)" "(e) DECISION=COMPLETED"
assert_eq "tier:maintenance" "$(get_field "$OUT" TIER)" "(e) TIER recovered as tier:maintenance from 'Tier 3'"
assert_contains "$EDITS" "loom:issue" "(e) the completing edit adds loom:issue"
assert_contains "$EDITS" "tier:maintenance" "(e) the completing edit adds the recovered tier label"
assert_contains "$COMMENTS_POSTED" "6464" "(e) a reconciliation comment is posted on the issue"

# (f) Tier 1 -> tier:goal-advancing.
reset_state
issue_json "OPEN" "$(labels_json "loom:architect")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 1 - directly implements the milestone")]" \
  > "$STUB_DIR/issue-300.json"
stage_verify 300 "$(labels_json "loom:issue" "tier:goal-advancing")"
run_sut --issue 300 --apply
assert_eq "COMPLETED" "$(get_field "$OUT" DECISION)" "(f) DECISION=COMPLETED"
assert_eq "tier:goal-advancing" "$(get_field "$OUT" TIER)" "(f) TIER recovered as tier:goal-advancing from 'Tier 1'"

# (g) Tier 2 -> tier:goal-supporting.
reset_state
issue_json "OPEN" "$(labels_json "loom:hermit")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 2 - supports milestone infra")]" \
  > "$STUB_DIR/issue-301.json"
stage_verify 301 "$(labels_json "loom:issue" "tier:goal-supporting")"
run_sut --issue 301 --apply
assert_eq "COMPLETED" "$(get_field "$OUT" DECISION)" "(g) DECISION=COMPLETED"
assert_eq "tier:goal-supporting" "$(get_field "$OUT" TIER)" "(g) TIER recovered as tier:goal-supporting from 'Tier 2'"

# (h) --apply but the tier cannot be recovered from the comment text (no
#     "Goal Alignment" line at all) -> ESCALATED, exit 13, operator labels added.
reset_state
issue_json "OPEN" "$(labels_json "loom:curated")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "no tier info here")]" \
  > "$STUB_DIR/issue-302.json"
run_sut --issue 302 --apply
assert_eq "13" "$RC" "(h) tier unrecoverable -> exit 13"
assert_eq "ESCALATED" "$(get_field "$OUT" DECISION)" "(h) DECISION=ESCALATED"
assert_eq "" "$(get_field "$OUT" TIER)" "(h) TIER is empty when unrecoverable"
assert_contains "$EDITS" "loom:operator-only" "(h) escalates to loom:operator-only"
assert_contains "$EDITS" "loom:operator-mechanical" "(h) escalates with loom:operator-mechanical sub-kind"
assert_contains "$COMMENTS_POSTED" "302" "(h) an escalation comment is posted on the issue"

# (h2) #6942: re-invoking --apply against the SAME issue after case (h) already
#     escalated it (loom:operator-only is now present) must NOT post a second
#     escalation comment or re-run the label edit -- DECISION=ALREADY_ESCALATED,
#     exit 0, distinguishable from a fresh ESCALATED (exit 13).
reset_state
issue_json "OPEN" "$(labels_json "loom:curated" "loom:operator-only" "loom:operator-mechanical")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "no tier info here")]" \
  > "$STUB_DIR/issue-302.json"
run_sut --issue 302 --apply
assert_eq "0" "$RC" "(h2) already loom:operator-only -> exit 0, not 13"
assert_eq "ALREADY_ESCALATED" "$(get_field "$OUT" DECISION)" "(h2) DECISION=ALREADY_ESCALATED, distinguishable from fresh ESCALATED"
assert_eq "" "$EDITS" "(h2) no duplicate label edit is issued"
assert_eq "" "$COMMENTS_POSTED" "(h2) no duplicate escalation comment is posted"

# (i) --apply, tier recoverable, but the completing edit's OWN read-back still
#     shows loom:issue missing (the exact #6464 failure mode recurring on the
#     reconciliation attempt itself) -> ESCALATED, exit 13, never silently
#     reports success it cannot verify.
reset_state
issue_json "OPEN" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance)")]" \
  > "$STUB_DIR/issue-303.json"
stage_verify 303 "$(labels_json "loom:auditor")"   # loom:issue still absent post-edit
run_sut --issue 303 --apply
assert_eq "13" "$RC" "(i) read-back still shows loom:issue missing after the completing edit -> exit 13"
assert_eq "ESCALATED" "$(get_field "$OUT" DECISION)" "(i) DECISION=ESCALATED"
assert_eq "tier:maintenance" "$(get_field "$OUT" TIER)" "(i) TIER is still reported even though the write did not verify"

# (j) gh issue view (initial read) failure -> exit 1, usage/environment error.
reset_state
touch "$STUB_DIR/issue-fail-304"
run_sut --issue 304
assert_eq "1" "$RC" "(j) initial gh issue view failure -> exit 1"
assert_contains "$ERR" "gh issue view" "(j) stderr names the failing gh call"

# (k) missing --issue -> exit 1, usage error.
reset_state
run_sut
assert_eq "1" "$RC" "(k) missing --issue -> exit 1"
assert_contains "$ERR" "required" "(k) stderr explains the usage error"

# (l) non-numeric --issue -> exit 1.
reset_state
run_sut --issue abc
assert_eq "1" "$RC" "(l) non-numeric --issue -> exit 1"

# --- #6933: loom:issue currently absent but the issue legitimately progressed
# further (loom:issue -> loom:building/loom:blocked) AFTER promotion actually
# landed. Judging purely from the current label set (no loom:issue present)
# looks identical to #6862's lost-write MISMATCH; the timeline check must
# distinguish the two so this case does NOT get loom:issue re-added on top of
# the issue's current further-along label.

# (m) APPROVED comment present, loom:issue currently absent, but the label
#     timeline shows a `labeled loom:issue` event AFTER the comment -> the
#     promotion landed and the issue has since moved on. DECISION=OK, exit 0,
#     no writes at all (not even a report).
reset_state
issue_json "OPEN" "$(labels_json "loom:building")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance) - cleanup")]" \
  > "$STUB_DIR/issue-400.json"
stage_timeline 400 "[$(labeled_event "2026-08-18T00:05:00Z")]"
run_sut --issue 400
assert_eq "0" "$RC" "(m) loom:issue applied after APPROVED comment, issue since progressed -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(m) DECISION=OK (timeline-confirmed)"
assert_eq "" "$EDITS" "(m) no label edit issued -- must not re-add loom:issue on top of loom:building"
assert_eq "" "$COMMENTS_POSTED" "(m) no comment posted"
run_sut --issue 400 --apply
assert_eq "0" "$RC" "(m) same result even with --apply (nothing to reconcile)"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(m) DECISION=OK under --apply too"
assert_eq "" "$EDITS" "(m) --apply still issues no label edit"

# (n) Regression: APPROVED comment present, loom:issue currently absent, and a
#     `labeled loom:issue` timeline event exists but it is BEFORE the comment
#     (e.g. the label was applied, then somehow removed, predating this
#     verdict) -- this is NOT the #6933 case, direction matters. Must still
#     fall through to the existing #6862 MISMATCH behavior, unchanged.
reset_state
issue_json "OPEN" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance) - cleanup")]" \
  > "$STUB_DIR/issue-401.json"
stage_timeline 401 "[$(labeled_event "2026-08-17T00:00:00Z")]"
run_sut --issue 401
assert_eq "11" "$RC" "(n) labeled-loom:issue event predates the APPROVED comment -> still exit 11"
assert_eq "MISMATCH" "$(get_field "$OUT" DECISION)" "(n) DECISION=MISMATCH (direction-sensitive, not fooled by an earlier event)"

# (o) Regression, explicit: no `labeled loom:issue` timeline event at all
#     (empty timeline, the default when no fixture is staged) -- must still
#     behave exactly like the pre-#6933 script: MISMATCH.
reset_state
issue_json "OPEN" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance) - cleanup")]" \
  > "$STUB_DIR/issue-402.json"
run_sut --issue 402
assert_eq "11" "$RC" "(o) no labeled loom:issue timeline event -> exit 11, MISMATCH unchanged"
assert_eq "MISMATCH" "$(get_field "$OUT" DECISION)" "(o) DECISION=MISMATCH"

# (p) Multiple APPROVED comments (re-evaluation): a `labeled loom:issue` event
#     lands between the two APPROVED comments -- it postdates the OLDEST
#     comment but predates the NEWEST one. The comparison must use the
#     NEWEST comment (matching the existing `sort_by(.createdAt) | last`
#     selection for APPROVED_COMMENT), so this must still be MISMATCH, not OK.
reset_state
issue_json "OPEN" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-08-10T00:00:00Z" "**Goal Alignment**: Tier 2 - first pass"), $(approved_comment "2026-08-20T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance) - re-evaluated")]" \
  > "$STUB_DIR/issue-403.json"
stage_timeline 403 "[$(labeled_event "2026-08-11T00:00:00Z")]"
run_sut --issue 403
assert_eq "11" "$RC" "(p) labeled loom:issue event postdates oldest APPROVED comment but predates newest -> exit 11"
assert_eq "MISMATCH" "$(get_field "$OUT" DECISION)" "(p) DECISION=MISMATCH -- compared against the NEWEST APPROVED comment, not the oldest"

# (q) Same multi-comment shape, but the timeline event postdates BOTH APPROVED
#     comments (i.e. after the re-evaluation too) -> OK, correctly compared
#     against the newest comment.
reset_state
issue_json "OPEN" "$(labels_json "loom:building")" \
  "[$(approved_comment "2026-08-10T00:00:00Z" "**Goal Alignment**: Tier 2 - first pass"), $(approved_comment "2026-08-20T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance) - re-evaluated")]" \
  > "$STUB_DIR/issue-404.json"
stage_timeline 404 "[$(labeled_event "2026-08-20T00:05:00Z")]"
run_sut --issue 404
assert_eq "0" "$RC" "(q) labeled loom:issue event postdates the newest APPROVED comment -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(q) DECISION=OK"

# (r) `gh api .../timeline` fetch itself fails -> exit 1, usage/environment
#     error, same treatment as the initial `gh issue view` failure (j).
reset_state
issue_json "OPEN" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance)")]" \
  > "$STUB_DIR/issue-405.json"
touch "$STUB_DIR/timeline-fail-405"
run_sut --issue 405
assert_eq "1" "$RC" "(r) gh api timeline failure -> exit 1"
assert_contains "$ERR" "timeline" "(r) stderr names the failing gh api call"

# --- #7299: issue #7287 incident regression. A single `gh issue edit
# --add-label loom:issue --add-label "$TIER"` call landed cleanly (each label
# gets its own distinct timeline event, contrary to the issue's first
# suspected-cause hypothesis -- see (t) below), but a SECOND run of this
# script mis-selected its OWN prior completed-reconciliation comment (which
# quotes "Champion Review: APPROVED" in its own narrative text) as the
# "newest APPROVED comment", pushing APPROVED_AT past the very labeled event
# it needed to see, and incorrectly escalated an issue that had already
# progressed to loom:building.

# (s) Isolates the comment-selection bug (Fix A) on its own: the CURRENT
#     label set does NOT include loom:building/loom:blocked (so the Step 1b
#     fallback added by this fix cannot mask a broken comment filter), a
#     genuine Champion APPROVED comment lands first, the `labeled loom:issue`
#     timeline event lands after it, and THEN this script's own self-
#     generated follow-up comment (quoting "Champion Review: APPROVED" in its
#     own prose) lands last. A naive `contains()` match would pick the
#     self-generated comment as "the newest APPROVED comment", whose
#     timestamp is AFTER the labeled event -- producing a false MISMATCH.
#     With the exclusion filter, the genuine comment is used instead and the
#     labeled event correctly postdates it -> OK.
reset_state
issue_json "OPEN" "$(labels_json "loom:auditor")" \
  "[$(approved_comment "2026-09-06T15:08:19Z" "**Goal Alignment**: Tier 2 (goal-supporting)"), $(self_generated_comment "2026-09-06T15:16:27Z" "completed")]" \
  > "$STUB_DIR/issue-7287.json"
stage_timeline 7287 "[$(labeled_event "2026-09-06T15:16:26Z")]"
run_sut --issue 7287
assert_eq "0" "$RC" "(s) #7287 regression: self-generated comment must not shadow the genuine APPROVED comment -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(s) DECISION=OK (genuine APPROVED comment used, not this script's own follow-up)"
assert_eq "" "$EDITS" "(s) no label edit issued"
assert_eq "" "$COMMENTS_POSTED" "(s) no escalation comment posted"

# (t) Isolates the later-lifecycle-label fallback (Fix B / Step 1b) on its
#     own: loom:issue is currently absent, loom:building IS present (the
#     issue already progressed past the promotion, exactly as #7287's
#     Builder claim did at 15:56:06), and the timeline lookup finds NO
#     `labeled loom:issue` event at all (default empty timeline -- e.g. the
#     timeline read itself is incomplete/unreliable, acceptance criterion 3).
#     Must resolve OK from the current-label check alone, independent of the
#     timeline, and without even needing a timeline fixture staged.
reset_state
issue_json "OPEN" "$(labels_json "loom:building")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 2 (goal-supporting)")]" \
  > "$STUB_DIR/issue-406.json"
run_sut --issue 406
assert_eq "0" "$RC" "(t) loom:building present with no timeline evidence at all -> exit 0 via later-lifecycle fallback"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(t) DECISION=OK (later-lifecycle label alone is sufficient proof)"
assert_eq "" "$EDITS" "(t) no label edit issued"
assert_eq "" "$COMMENTS_POSTED" "(t) no comment posted"

# (u) Same fallback, but via loom:blocked instead of loom:building (the other
#     legitimate further-progression the script's own doc comment names).
reset_state
issue_json "OPEN" "$(labels_json "loom:blocked")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 3 (maintenance)")]" \
  > "$STUB_DIR/issue-407.json"
run_sut --issue 407
assert_eq "0" "$RC" "(u) loom:blocked present with no timeline evidence -> exit 0 via later-lifecycle fallback"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(u) DECISION=OK"

# (v) A single multi-label `gh issue edit --add-label loom:issue --add-label
#     "$TIER"` call DOES produce a distinct, matchable `labeled`/`loom:issue`
#     timeline event of its own (confirmed against a real multi-label edit on
#     this repo, #7299) -- so the Step 2 timeline match must not require the
#     event to be the ONLY `labeled` event at that timestamp. Two `labeled`
#     events share the same created_at (loom:issue and the tier label from
#     one multi-label call); the loom:issue-specific one must still be found.
reset_state
issue_json "OPEN" "$(labels_json "loom:building")" \
  "[$(approved_comment "2026-08-18T00:00:00Z" "**Goal Alignment**: Tier 2 (goal-supporting)")]" \
  > "$STUB_DIR/issue-408.json"
stage_timeline 408 "[$(labeled_event "2026-08-18T00:05:00Z"), {\"event\":\"labeled\",\"created_at\":\"2026-08-18T00:05:00Z\",\"label\":{\"name\":\"tier:goal-supporting\"}}]"
run_sut --issue 408
assert_eq "0" "$RC" "(v) multi-label edit's loom:issue timeline event is found alongside a same-timestamp tier event -> exit 0"
assert_eq "OK" "$(get_field "$OUT" DECISION)" "(v) DECISION=OK"

echo
echo "--- Doc pins: champion-issue-promo.md ships the reordered write-then-verify Step 3b and the Pass 0c reconciliation loop (#6862) ---"

assert_doc_contains "$CHAMPION_MD" \
    'LOOM_ISSUE_LANDED=$(gh issue view "$ISSUE_NUMBER" --json labels' \
    "Step 3b reads back loom:issue after the label edit before posting the verdict comment"

assert_doc_contains "$CHAMPION_MD" \
    'do NOT post the APPROVED comment' \
    "Step 3b explicitly refuses to post the APPROVED comment when the label read-back fails"

assert_doc_contains "$CHAMPION_MD" \
    './.loom/scripts/check-promotion-landed.sh --issue "$N" --apply' \
    "Pass 0c invokes check-promotion-landed.sh --apply in its reconciliation loop"

assert_doc_contains "$CHAMPION_MD" \
    'in:comments "Champion Review: APPROVED" -label:loom:issue' \
    "Pass 0c's candidate search shortlists APPROVED-comment-without-loom:issue issues"

assert_doc_contains "$CHAMPION_MD" \
    "#6862" \
    "champion-issue-promo.md documents the #6862 promotion-write-reliability fix"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
