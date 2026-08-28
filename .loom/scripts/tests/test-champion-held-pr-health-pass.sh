#!/usr/bin/env bash
# test-champion-held-pr-health-pass.sh - Regression tests for the merge-risk
# hold "one-way door" defect (#6720), extended for the "rebase treadmill"
# fix (#6852).
#
# Champion's 6 safety criteria (`champion-pr-merge.md`) are prose an LLM
# instance reads and executes, not a standalone script (same situation as
# test-champion-critical-file-check.sh and test-dependency-parse.sh) — so this
# file mirrors the documented control flow in local functions and pins the
# shipped markdown's exact commands with `assert_doc_contains` /
# `assert_doc_lacks`, catching drift between the two.
#
# Incident recap (#6720): criterion #2's sticky-hold precheck bailed out with
# "HOLD, silently. Skip the PR for this pass" BEFORE criteria #4 (merge
# conflict), #5 (recency) and #6 (CI status) were ever evaluated. Because
# criterion #5's stale route is the ONLY automated path from `loom:pr` to
# `loom:changes-requested` — i.e. the only way a `loom:pr` PR reaches Doctor —
# a held PR could never be reported as conflicting nor routed for a rebase.
# Measured on rjwalters/loom 2026-08-22: 21 open PRs carried `loom:pr` +
# `loom:operator`, 20 of the 21 were CONFLICTING (two untouched for 63h), and
# Doctor's `loom:changes-requested` queue was empty.
#
# The #6720 fix decouples MERGE PERMISSION from HEALTH REPORTING:
#   1. A hold (sticky OR fresh) sets MERGE_BLOCKED_BY_HOLD=true and blocks the
#      merge + criterion #3 + Steps 2-3 — but the PR is NOT dropped from the
#      pass. Criteria #4/#6/#5 still run ("Held-PR Health Pass").
#   2. A held PR that turns CONFLICTING gets one idempotent
#      `champion:held-pr-conflict-notice` comment.
#   3. A held PR that goes stale is routed to Doctor exactly like an unheld one
#      (`loom:pr` -> `loom:changes-requested`), with the
#      `champion:merge-risk-hold` marker PRESERVED so the hold re-binds and the
#      mandatory reversal comment stays mandatory (#4742). A rebase must not
#      launder a held PR into an unheld one.
#   4. `loom:operator` is KEPT on the held-and-stale route (narrowing #5802,
#      which clears it on the unheld route) — the human merge decision is still
#      outstanding, and Doctor's `loom:changes-requested` queue filters
#      `loom:blocked` / `loom:operator-only`, not `loom:operator`.
#
# Follow-up incident (#6848/#6852): the #6720 fix routes ANY held-and-stale PR
# to Doctor, including one whose ONLY blocker is the standing hold itself (no
# unresolved feedback, no failing CI). `main` moving repeatedly then produces
# an unproductive "rebase treadmill" — conflict, route to Doctor, rebase,
# still-held, `main` moves again — that cannot converge, since the real
# blocker (a pending human merge decision) is untouched by any of it. #6852
# narrows criterion #5's held route: it distinguishes a **hold-only** stale PR
# (no other red criterion — left in place, one-time suspension notice, no
# label change) from a **hold-plus-feedback** one (a failing required check —
# routes to Doctor exactly as #6720 shipped). CI status is the only currently
# available live signal cheap enough to compute every pass without inventing
# new history-tracking machinery, so it is what `HELD_CI_FAILING` is keyed on;
# criterion #6 is reordered to run BEFORE #5 so that signal is known before
# #5's routing decision. Neither branch ever touches the hold marker or
# `loom:operator` — #6720's core property (a held PR is never silently dropped
# from reporting) holds on both.
#
# This file asserts:
#   1. THE HEADLINE REGRESSION SHAPE (#6720), now the HOLD-PLUS-FEEDBACK
#      variant: hold a PR that ALSO has a failing check, let `main` move until
#      it conflicts, advance past 24h -> it still reaches Doctor
#      (`loom:changes-requested`) instead of sitting silently.
#   1B. THE HOLD-ONLY VARIANT (#6852): the identical shape MINUS the failing
#       check -> the automatic route is SUSPENDED, not fired; the PR stays on
#       `loom:pr`, still reported, still carrying `loom:operator`.
#   2. Criteria #4/#6/#5 are evaluated under a hold; #3 and the merge are not.
#   3. The conflict notice, the stale notice, and the suspension notice are
#      each posted exactly once across repeated ticks (10-minute cron
#      anti-spam).
#   4. The hold marker survives the route to Doctor on every held path, AND is
#      untouched (trivially) on the suspended path.
#   5. `loom:operator` is kept on the held route (routed or suspended) and
#      still cleared on the unheld route (#5802 not regressed).
#   6. A FRESH hold (red axis this tick) gets the same health pass as a sticky
#      one, for both the hold-plus-feedback and hold-only variants — neither
#      defect is sticky-specific.
#   7. The never-held green path is byte-for-byte unchanged (still merges).
#   8. The held-PR census jq pipeline computes count / conflicting / at-Doctor,
#      and a hold-only-suspended PR (still `loom:pr` + `loom:operator`) is
#      still counted exactly like one that routed.
#   9. The stale-notice marker is keyed per-episode (head SHA), not
#      posted-ever: a PR that cycled back to `loom:pr` with new commits and
#      then went stale AGAIN gets a fresh notice, while a re-check within the
#      SAME still-stale episode stays suppressed (#6860).
#   10. The shipped markdown carries the new literals and no longer carries
#       the short-circuiting ones.
#
# #6851 extension: the aggregate census line (above) is transcript-only — it
# is never durably recorded anywhere across passes. This file's Test 10+
# additionally covers the **per-PR digest**: hold-reason extraction from a
# PR's own `champion:merge-risk-hold` comment, per-row table formatting, and
# a drift guard pinning the pinned-tracking-issue mechanism (title/marker,
# `loom:blocked` exclusion, edit-in-place vs. create) in the shipped markdown.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-held-pr-health-pass.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"
CHAMPION_COMMON_MD="$PROMPT_DIR/champion-common.md"

# Same two-layout probe for the docs directory: `.loom/docs` when installed,
# `defaults/docs` in this source repo.
if [[ -d "$SCRIPTS_DIR/../docs" ]]; then
    DOCS_DIR="$(cd "$SCRIPTS_DIR/../docs" && pwd)"
else
    DOCS_DIR="$(cd "$SCRIPTS_DIR/../../docs" && pwd)"
fi
LABEL_SM_MD="$DOCS_DIR/label-state-machine.md"

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
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Missing '$needle' in:"
        sed 's/^/      /' <<<"$haystack"
    fi
}

assert_lacks() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected '$needle' in:"
        sed 's/^/      /' <<<"$haystack"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

# Pin a literal snippet as present verbatim in a doc file — catches drift
# between this test's mirrored control flow and the shipped markdown.
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

# Pin a literal snippet's ABSENCE — catches a regression back to the
# short-circuiting bail-out.
assert_doc_lacks() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (found short-circuiting literal in $file: $needle)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

# =====================================================================
# PR state fixture — a tiny stand-in for the forge state a Champion tick
# reads and writes: which idempotency markers are already posted, and which
# labels the PR carries. Persisted in a file so successive ticks in one test
# observe each other, exactly as successive cron ticks do.
# =====================================================================
state_new() {
    local f
    f=$(mktemp)
    printf 'label:loom:pr\n' >"$f"
    echo "$f"
}

state_has()    { grep -qxF -- "$1" "$2"; }
state_add()    { state_has "$1" "$2" || printf '%s\n' "$1" >>"$2"; }
state_remove() { local tmp; tmp=$(mktemp); grep -vxF -- "$1" "$2" >"$tmp" || true; mv "$tmp" "$2"; }

state_labels() {
    sed -n 's/^label://p' "$1" | sort | paste -sd, - 2>/dev/null || true
}

# =====================================================================
# champion-pr-merge.md's per-PR evaluation, mirrored from the shipped prose:
#   - criterion #2's sticky-hold precheck (STICKY_HOLD / MERGE_BLOCKED_BY_HOLD)
#   - criterion #2's "Hold behavior" fresh-hold branch
#   - the "Held-PR Health Pass" (#6720, reordered by #6852): criteria
#     #4/#6/#5 under a hold
#   - "PR Rejection Workflow -> Stale PR, hold-plus-feedback or unheld"
#     (#6720/#5802) and "-> Hold-only Stale PR" (#6852)
#
# Emits one ACTION line per observable effect so a test can assert on the
# whole tick. Deliberately does NOT model criterion #1 or #3's internals —
# those are covered by test-champion-critical-file-check.sh.
#
# Args: <state-file> <prior_hold> <release_reason> <axes_red> <mergeable>
#       <hours_ago> <ci> [last_activity]
#
# `ci` ("pass" | "fail") plays two roles under a hold (#6852): the #6 report
# below, AND (via HELD_CI_FAILING) the genuine-feedback signal #5 reads to
# decide whether a stale held PR routes to Doctor or is left suspended.
#
# last_activity (#6860): the stale-notice marker's episode key, mirroring
# champion-pr-merge.md's `<!-- champion:stale-pr-notice:$LAST_ACTIVITY -->` —
# the same "most recent commit or non-Champion comment" value criterion #5's
# recency check computes (#6843/#6844), reused here as the episode key.
# Defaults to a fixed value so every pre-existing call site (none of which
# models a Doctor round-trip landing real new activity) is unaffected.
# =====================================================================
champion_pr_pass() {
    local state="$1" prior_hold="$2" release_reason="$3" axes_red="$4"
    local mergeable="$5" hours_ago="$6" ci="$7" last_activity="${8:-2026-08-01T00:00:00Z}"

    local MERGE_BLOCKED_BY_HOLD=false

    # ---- criterion #2: sticky-hold precheck -------------------------------
    # (The doc also sets STICKY_HOLD here for narration; the `HOLD:sticky`
    # action line below is this mirror's equivalent, so the flag itself is not
    # duplicated.)
    if [ "$prior_hold" = true ] && [ -z "$release_reason" ]; then
        MERGE_BLOCKED_BY_HOLD=true
        echo "HOLD:sticky"
        # #6720: NO `continue` here. Pre-fix, the pass ended on this line.
    else
        # Never held, or held-and-released: the four axes are judged normally.
        if [ "$axes_red" = true ]; then
            # "Hold behavior" — idempotent notice + loom:operator, no merge.
            if state_has "marker:champion:merge-risk-hold" "$state"; then
                echo "HOLD_NOTICE:suppressed"
            else
                state_add "marker:champion:merge-risk-hold" "$state"
                echo "COMMENT:champion:merge-risk-hold"
            fi
            state_add "label:loom:operator" "$state"
            MERGE_BLOCKED_BY_HOLD=true
            echo "HOLD:fresh"
        fi
    fi

    # ---- criterion #3: merge gate only; skipped under a hold --------------
    if [ "$MERGE_BLOCKED_BY_HOLD" = true ]; then
        echo "SKIP:criterion-3"
    else
        echo "EVAL:criterion-3"
    fi

    # ---- criterion #4: ALWAYS evaluated (the #6720 fix) ------------------
    echo "EVAL:criterion-4"
    if [ "$mergeable" = "CONFLICTING" ] && [ "$MERGE_BLOCKED_BY_HOLD" = true ]; then
        if state_has "marker:champion:held-pr-conflict-notice" "$state"; then
            echo "CONFLICT_NOTICE:suppressed"
        else
            state_add "marker:champion:held-pr-conflict-notice" "$state"
            echo "COMMENT:champion:held-pr-conflict-notice"
        fi
    fi

    # ---- criterion #6: ALWAYS evaluated; now runs BEFORE #5 (#6852) so its
    # outcome (HELD_CI_FAILING) is known before #5's routing decision -------
    echo "EVAL:criterion-6"
    if [ "$ci" = "fail" ]; then
        echo "REPORT:ci-status"
    fi
    # HELD_CI_FAILING mirrors the doc's own variable name. Only meaningful
    # under a hold; unused (but harmless) on the unheld path.
    local HELD_CI_FAILING=false
    [ "$ci" = "fail" ] && HELD_CI_FAILING=true

    # ---- criterion #5: ALWAYS evaluated; stale routes to Doctor -----------
    # ONLY when hold-plus-feedback or unheld (#6720/#5802). A hold-only stale
    # PR (MERGE_BLOCKED_BY_HOLD=true, HELD_CI_FAILING=false) is suspended
    # instead — no label change, a separate one-time notice (#6852).
    echo "EVAL:criterion-5"
    local stale=false
    [ "$hours_ago" -gt 24 ] && stale=true
    if [ "$stale" = true ]; then
        if [ "$MERGE_BLOCKED_BY_HOLD" = true ] && [ "$HELD_CI_FAILING" != true ]; then
            # ---- Hold-only Stale PR: suspend the route (#6852) ----------
            if state_has "marker:champion:held-stale-suspended" "$state"; then
                echo "SUSPEND_NOTICE:suppressed"
            else
                state_add "marker:champion:held-stale-suspended" "$state"
                echo "COMMENT:champion:held-stale-suspended"
            fi
            echo "SUSPENDED:hold-only"
            # No label touched, no marker touched — the PR stays exactly
            # where it was (loom:pr + loom:operator + the hold marker).
        else
            # ---- Stale PR: hold-plus-feedback, or unheld (#6720/#5802) --
            # #6860: keyed on last_activity, not on marker existence alone — a
            # notice from a PAST episode (an older last_activity value, since
            # superseded by real new activity that cycled the PR back to
            # loom:pr) must NOT suppress a fresh notice for a NEW episode that
            # has since gone stale again.
            if state_has "marker:champion:stale-pr-notice:$last_activity" "$state"; then
                echo "STALE_NOTICE:suppressed"
            else
                state_add "marker:champion:stale-pr-notice:$last_activity" "$state"
                echo "COMMENT:champion:stale-pr-notice"
                state_remove "label:loom:pr" "$state"
                state_add "label:loom:changes-requested" "$state"
                echo "LABEL_REMOVE:loom:pr"
                echo "LABEL_ADD:loom:changes-requested"
                # loom:operator: conditional on whether a hold is still in
                # force (#6720 narrowing #5802).
                if [ "$MERGE_BLOCKED_BY_HOLD" = true ]; then
                    echo "LABEL_KEEP:loom:operator"
                else
                    state_remove "label:loom:operator" "$state"
                    echo "LABEL_REMOVE:loom:operator"
                fi
                # The hold marker is NEVER touched on this path.
            fi
        fi
    fi

    # ---- merge decision --------------------------------------------------
    if [ "$MERGE_BLOCKED_BY_HOLD" = true ]; then
        echo "NO_MERGE:hold"
        return 0
    fi
    if [ "$mergeable" != "MERGEABLE" ]; then echo "NO_MERGE:conflict"; return 0; fi
    if [ "$stale" = true ];             then echo "NO_MERGE:stale";    return 0; fi
    if [ "$ci" = "fail" ];              then echo "NO_MERGE:ci";       return 0; fi
    echo "MERGE"
}

# =====================================================================
# "Held-PR Census" jq pipeline, mirrored from champion-pr-merge.md.
# Reads the `gh pr list --json number,title,createdAt,updatedAt,mergeable,
# labels` payload on stdin, prints "<count> <conflicting> <at-doctor>".
# =====================================================================
held_census() {
    local json
    json=$(cat)
    local count conflicting at_doctor
    count=$(printf '%s\n' "$json" | jq 'length')
    conflicting=$(printf '%s\n' "$json" | jq '[.[] | select(.mergeable == "CONFLICTING")] | length')
    at_doctor=$(printf '%s\n' "$json" | jq '[.[] | select([.labels[].name] | index("loom:changes-requested"))] | length')
    echo "$count $conflicting $at_doctor"
}

# =====================================================================
# "Per-PR Digest" (#6851), mirrored from champion-pr-merge.md's "Held-PR
# Census -> Per-PR Digest" section:
#   - digest_reason_from_body: extract the hold reason from the PR's own
#     `champion:merge-risk-hold` comment body (or fall back when it is
#     missing/unparseable).
#   - digest_row: format one digest table row (PR number, reason, status),
#     appending ", out at Doctor" only when the PR carries
#     loom:changes-requested — same as $HELD_JSON's label check.
# =====================================================================
digest_reason_from_body() {
    local hold_body="$1"
    if [[ -z "$hold_body" ]]; then
        echo "reason unrecorded (no marker comment found)"
        return
    fi
    local reason
    reason=$(printf '%s\n' "$hold_body" | grep -m1 -E '^- \*\*.+\*\*:' | sed 's/^- //')
    if [[ -z "$reason" ]]; then
        echo "hold marker present, axis bullet not parseable"
    else
        echo "$reason"
    fi
}

digest_row() {
    local pr_num="$1" reason="$2" mergeable="$3" at_doctor="$4"
    local status="$mergeable"
    [[ "$at_doctor" == true ]] && status="$status, out at Doctor"
    echo "| #$pr_num | $reason | $status |"
}

# =====================================================================
# Per-PR conflict-duration tracking (#7020), mirrored from
# champion-pr-merge.md's "Held-PR Census -> Per-PR Digest" Step 0 / Step 1:
#   - conflict_since_for: carry the first-seen-CONFLICTING timestamp forward
#     from the PREVIOUS pass's digest body (a `champion:conflict-since:PR=<n>
#     TS=<iso>` marker) if this PR was already CONFLICTING there; otherwise
#     this is a fresh conflict episode and the clock starts at "now". A PR
#     that went MERGEABLE for even one intervening pass has no marker in
#     that pass's body, so the very next CONFLICTING pass naturally resets
#     rather than resuming the original "since" — the load-bearing edge case
#     from the issue's own Test Plan (no rot-time accumulation across a
#     MERGEABLE gap).
#   - conflict_days: whole days elapsed between a since-timestamp and "now"
#     (both ISO-8601 UTC), cross-platform (GNU `date -d`, BSD `date -j -f`).
#   - digest_status: formats the Status cell exactly as champion-pr-merge.md's
#     STATUS variable does — "<mergeable>[ since <ts>, <n>d][, out at Doctor]".
#   - digest_aggregate_line: formats the digest issue's "**Aggregate**: ..."
#     line, split into held-clean vs. held-rotting counts.
# =====================================================================
CONFLICT_SINCE_PREFIX="<!-- champion:conflict-since:PR="
ROT_THRESHOLD_DAYS=3

conflict_since_for() {
    local pr_num="$1" mergeable="$2" old_body="$3" now_iso="$4"
    if [[ "$mergeable" != "CONFLICTING" ]]; then
        echo ""
        return
    fi
    local prior
    prior=$(printf '%s\n' "$old_body" | grep -o "${CONFLICT_SINCE_PREFIX}${pr_num} TS=[0-9TZ:-]*" | head -1 | sed -E 's/.*TS=//')
    if [[ -n "$prior" ]]; then
        echo "$prior"
    else
        echo "$now_iso"
    fi
}

_iso_to_epoch() {
    date -d "$1" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$1" +%s
}

conflict_days() {
    local since_iso="$1" now_iso="$2"
    echo $(( ( $(_iso_to_epoch "$now_iso") - $(_iso_to_epoch "$since_iso") ) / 86400 ))
}

digest_status() {
    local mergeable="$1" since="$2" days="$3" at_doctor="$4"
    local status="$mergeable"
    if [[ "$mergeable" == "CONFLICTING" ]]; then
        status="${status} since ${since}, ${days}d"
    fi
    [[ "$at_doctor" == true ]] && status="$status, out at Doctor"
    echo "$status"
}

digest_aggregate_line() {
    local held="$1" conflicting="$2" rotting="$3" at_doctor="$4" oldest="$5"
    local clean=$((conflicting - rotting))
    echo "Merge-risk holds: $held open PR(s) — $conflicting conflicting ($rotting rotting >=${ROT_THRESHOLD_DAYS}d, $clean clean), $at_doctor out at Doctor, oldest ${oldest}d"
}

echo "=== test-champion-held-pr-health-pass.sh ==="
echo

# ---------------------------------------------------------------------
echo "Test 1: THE REGRESSION SHAPE, hold-plus-feedback — held + CONFLICTING + failing CI + past 24h reaches Doctor"
# This is the exact failure #6720 documents, now with a failing check added so
# it exercises the hold-PLUS-FEEDBACK branch (#6852): a held PR that main has
# moved past, that has aged out, AND that carries a real problem of its own
# (unrelated to the hold). Pre-#6720, this tick produced nothing at all.
# Pre-#6852 the routing below did not depend on `ci` at all — #6852 makes it
# conditional, so this test's `ci=fail` is now load-bearing: it is what keeps
# this a hold-plus-feedback case rather than sliding into the hold-only
# suspension covered by Test 1B below.
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
OUT=$(champion_pr_pass "$S" true "" false CONFLICTING 63 fail)

assert_contains "$OUT" "HOLD:sticky" "the sticky hold is still recognized and still binds"
assert_contains "$OUT" "NO_MERGE:hold" "the held PR is not merged"
assert_contains "$OUT" "COMMENT:champion:held-pr-conflict-notice" \
    "the conflict is surfaced with an idempotent marker comment (AC #2)"
assert_contains "$OUT" "REPORT:ci-status" "the failing check is reported (criterion #6)"
assert_contains "$OUT" "LABEL_REMOVE:loom:pr" "loom:pr is removed by the stale route"
assert_contains "$OUT" "LABEL_ADD:loom:changes-requested" \
    "the held+stale+hold-plus-feedback PR REACHES DOCTOR via loom:changes-requested (AC #3, unchanged by #6852)"
assert_lacks "$OUT" "COMMENT:champion:held-stale-suspended" \
    "a hold-plus-feedback PR is never suspended — only a hold-ONLY PR is (#6852)"
assert_eq "loom:changes-requested,loom:operator" "$(state_labels "$S")" \
    "final labels: loom:changes-requested + loom:operator (loom:pr gone)"
TESTS_RUN=$((TESTS_RUN + 1))
if state_has "marker:champion:merge-risk-hold" "$S"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: the champion:merge-risk-hold marker is PRESERVED across the route to Doctor (AC #3)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: the hold marker was cleared — a rebase would launder this into an unheld PR"
fi
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 1B: THE HOLD-ONLY VARIANT (#6852) — held + CONFLICTING + past 24h, but NOTHING else red, is SUSPENDED not routed"
# Identical shape to Test 1 MINUS the failing check: the PR's only blocker is
# the standing hold itself. This is the treadmill case #6848/#6852 exist to
# fix — main moving repeatedly must not force an endless rebase-to-Doctor
# cycle when the actual blocker (a pending human merge decision) is untouched
# by any of it.
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
OUT=$(champion_pr_pass "$S" true "" false CONFLICTING 63 pass)

assert_contains "$OUT" "HOLD:sticky" "the sticky hold is still recognized and still binds"
assert_contains "$OUT" "NO_MERGE:hold" "the held PR is not merged"
assert_contains "$OUT" "COMMENT:champion:held-pr-conflict-notice" \
    "the conflict is STILL surfaced — #6852 only changes the STALE route, not the conflict notice (AC #1 unaffected)"
assert_lacks "$OUT" "REPORT:ci-status" "no failing check to report — CI is passing"
assert_contains "$OUT" "SUSPENDED:hold-only" "the stale route is recognized as hold-only and suspended (AC #1/#2)"
assert_contains "$OUT" "COMMENT:champion:held-stale-suspended" \
    "a one-time suspension notice is posted explaining why no route happened (AC #2)"
assert_lacks "$OUT" "COMMENT:champion:stale-pr-notice" \
    "the ordinary stale-route notice is NOT posted for a hold-only PR"
assert_lacks "$OUT" "LABEL_REMOVE:loom:pr" "loom:pr is NOT removed — the automatic rebase cycle is suspended (AC #2)"
assert_lacks "$OUT" "LABEL_ADD:loom:changes-requested" \
    "the hold-only PR does NOT reach Doctor — routing it would buy nothing (AC #2)"
assert_eq "loom:operator,loom:pr" "$(state_labels "$S")" \
    "final labels UNCHANGED: still loom:pr + loom:operator, still held-and-stale (AC #2, #3)"
TESTS_RUN=$((TESTS_RUN + 1))
if state_has "marker:champion:merge-risk-hold" "$S"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: the champion:merge-risk-hold marker is untouched on the suspended path (trivially preserved, AC #3)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: the hold marker was cleared on the suspended path — must never happen"
fi
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 1C: hold-only suspension notice idempotency across repeated ticks (#6852)"
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
TICK1=$(champion_pr_pass "$S" true "" false CONFLICTING 63 pass)
TICK2=$(champion_pr_pass "$S" true "" false CONFLICTING 64 pass)
TICK3=$(champion_pr_pass "$S" true "" false CONFLICTING 65 pass)
assert_contains "$TICK1" "COMMENT:champion:held-stale-suspended" "tick 1 posts the suspension notice"
assert_contains "$TICK2" "SUSPEND_NOTICE:suppressed" "tick 2 suppresses the duplicate suspension notice"
assert_contains "$TICK3" "SUSPEND_NOTICE:suppressed" "tick 3 suppresses the duplicate suspension notice"
assert_lacks "$TICK2" "LABEL_REMOVE:loom:pr" "tick 2 still does not route — no label swap ever runs on this path"
assert_lacks "$TICK3" "LABEL_ADD:loom:changes-requested" "tick 3 still does not route — no label swap ever runs on this path"
assert_eq "loom:operator,loom:pr" "$(state_labels "$S")" \
    "labels stay unchanged across every tick of a sustained hold-only suspension"
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 2: the mechanical criteria are evaluated under a hold; #3 and the merge are not"
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
OUT=$(champion_pr_pass "$S" true "" false MERGEABLE 1 pass)
assert_contains "$OUT" "EVAL:criterion-4" "criterion #4 (conflict) runs under a hold (AC #1)"
assert_contains "$OUT" "EVAL:criterion-5" "criterion #5 (recency) runs under a hold (AC #1)"
assert_contains "$OUT" "EVAL:criterion-6" "criterion #6 (CI) runs under a hold (AC #1)"
assert_contains "$OUT" "SKIP:criterion-3" "criterion #3 is skipped under a hold (pure merge gate, no remedy)"
assert_contains "$OUT" "NO_MERGE:hold" "the hold still blocks the merge — health reporting is not permission"
assert_lacks "$OUT" "COMMENT:" "a healthy held PR produces no comment at all (anti-spam)"
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 3: held + CONFLICTING but NOT stale — surfaced, not routed"
# Doctor's Priority 1 queue (loom:pr + CONFLICTING) deliberately excludes
# loom:operator (#5978) so autonomous work never force-pushes a held PR.
# Conflict alone is therefore a report; conflict PLUS staleness is a route.
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
OUT=$(champion_pr_pass "$S" true "" false CONFLICTING 3 pass)
assert_contains "$OUT" "COMMENT:champion:held-pr-conflict-notice" "the conflict is reported"
assert_lacks "$OUT" "LABEL_ADD:loom:changes-requested" \
    "a merely-conflicting held PR is NOT routed to Doctor (respects #5978)"
assert_eq "loom:operator,loom:pr" "$(state_labels "$S")" "labels are unchanged by a conflict report"
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 4: idempotency across repeated cron ticks — one comment per episode (hold-plus-feedback)"
# ci=fail keeps this a hold-plus-feedback episode (#6852) so the route still
# fires, exactly as #6720 shipped; Test 1C above is the hold-only counterpart.
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
TICK1=$(champion_pr_pass "$S" true "" false CONFLICTING 63 fail)
TICK2=$(champion_pr_pass "$S" true "" false CONFLICTING 64 fail)
TICK3=$(champion_pr_pass "$S" true "" false CONFLICTING 65 fail)
assert_contains "$TICK1" "COMMENT:champion:held-pr-conflict-notice" "tick 1 posts the conflict notice"
assert_contains "$TICK2" "CONFLICT_NOTICE:suppressed" "tick 2 suppresses the duplicate conflict notice"
assert_contains "$TICK3" "CONFLICT_NOTICE:suppressed" "tick 3 suppresses the duplicate conflict notice"
assert_contains "$TICK1" "COMMENT:champion:stale-pr-notice" "tick 1 posts the stale notice"
assert_contains "$TICK2" "STALE_NOTICE:suppressed" "tick 2 suppresses the duplicate stale notice"
assert_lacks "$TICK2" "LABEL_REMOVE:loom:pr" "tick 2 does not re-run the label swap"
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 5: loom:operator — kept on the held route, still cleared on the unheld route (#5802)"
# Held, hold-plus-feedback: the human merge decision is still outstanding, so
# the label stays. ci=fail keeps this the routed variant (#6852) — see Test 1B
# for the hold-only variant, where loom:operator is also kept, but because the
# label is simply never touched.
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
OUT=$(champion_pr_pass "$S" true "" false MERGEABLE 30 fail)
assert_contains "$OUT" "LABEL_KEEP:loom:operator" "held+stale+hold-plus-feedback keeps loom:operator (AC #4)"
assert_contains "$(state_labels "$S")" "loom:operator" "loom:operator survives in the final label set"
rm -f "$S"

# Unheld: #5802's original behavior, unchanged. Unheld routing never depended
# on CI status before #6852 and still does not — ci=pass here on purpose.
S=$(state_new)
OUT=$(champion_pr_pass "$S" false "" false MERGEABLE 30 pass)
assert_contains "$OUT" "LABEL_REMOVE:loom:operator" "unheld+stale still clears loom:operator (#5802 not regressed)"
assert_contains "$OUT" "LABEL_ADD:loom:changes-requested" "unheld+stale still routes to Doctor"
assert_eq "loom:changes-requested" "$(state_labels "$S")" "unheld+stale final labels are unchanged from #5802"
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 6: a FRESH hold (red axis this tick) gets the same health pass — hold-plus-feedback"
# The defect is not sticky-specific: a first-tick hold on an already-stale,
# already-conflicting, ALSO-CI-failing PR must not rot either. ci=fail keeps
# this the routed variant (#6852) — Test 6B below is the fresh-hold hold-only
# counterpart.
S=$(state_new)
OUT=$(champion_pr_pass "$S" false "" true CONFLICTING 63 fail)
assert_contains "$OUT" "COMMENT:champion:merge-risk-hold" "the fresh hold notice is posted"
assert_contains "$OUT" "HOLD:fresh" "the fresh hold blocks the merge"
assert_contains "$OUT" "EVAL:criterion-5" "criterion #5 still runs behind a fresh hold"
assert_contains "$OUT" "COMMENT:champion:held-pr-conflict-notice" "a freshly-held conflicting PR is surfaced too"
assert_contains "$OUT" "LABEL_ADD:loom:changes-requested" "a freshly-held, hold-plus-feedback stale PR reaches Doctor too"
assert_contains "$OUT" "LABEL_KEEP:loom:operator" "the fresh hold's loom:operator is kept on the route"
TESTS_RUN=$((TESTS_RUN + 1))
if state_has "marker:champion:merge-risk-hold" "$S"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: the fresh hold's marker is preserved across the route to Doctor"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: the fresh hold's marker was cleared"
fi
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 6B: a FRESH hold that is hold-only (no other blocker) is suspended too (#6852)"
# Same shape as Test 6, minus the failing check: neither the #6720 defect nor
# its #6852 fix is sticky-specific.
S=$(state_new)
OUT=$(champion_pr_pass "$S" false "" true CONFLICTING 63 pass)
assert_contains "$OUT" "COMMENT:champion:merge-risk-hold" "the fresh hold notice is posted"
assert_contains "$OUT" "HOLD:fresh" "the fresh hold blocks the merge"
assert_contains "$OUT" "COMMENT:champion:held-pr-conflict-notice" "a freshly-held conflicting PR is still surfaced"
assert_contains "$OUT" "SUSPENDED:hold-only" "the freshly-held, hold-only stale PR is suspended, not routed"
assert_contains "$OUT" "COMMENT:champion:held-stale-suspended" "the suspension notice is posted"
assert_lacks "$OUT" "LABEL_ADD:loom:changes-requested" "a freshly-held, hold-only stale PR does NOT reach Doctor"
assert_eq "loom:operator,loom:pr" "$(state_labels "$S")" \
    "final labels: still loom:pr + loom:operator — the fresh hold's own label add survives, untouched by the suspension"
TESTS_RUN=$((TESTS_RUN + 1))
if state_has "marker:champion:merge-risk-hold" "$S"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: the fresh hold's marker is untouched on the suspended path"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: the fresh hold's marker was cleared on the suspended path"
fi
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 7: the never-held green path still merges (no collateral change)"
S=$(state_new)
OUT=$(champion_pr_pass "$S" false "" false MERGEABLE 2 pass)
assert_contains "$OUT" "EVAL:criterion-3" "criterion #3 runs on the unheld path"
assert_contains "$OUT" "MERGE" "a green, recent, mergeable, unheld PR still merges"
assert_lacks "$OUT" "COMMENT:" "the never-held merge path posts none of the new notices"
assert_eq "loom:pr" "$(state_labels "$S")" "labels untouched on the merge path"
rm -f "$S"

# A released hold is re-judged, not auto-held: green axes after a real release
# signal merge exactly as before #6720.
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
OUT=$(champion_pr_pass "$S" true "new head commit abc1234" false MERGEABLE 2 pass)
assert_contains "$OUT" "MERGE" "a genuinely-released hold with green axes still merges (#4742 semantics intact)"
rm -f "$S"

# A released hold whose axes are STILL red re-holds — silently, because the
# marker survived. This is the "rebase does not launder a held PR" property.
S=$(state_new)
state_add "marker:champion:merge-risk-hold" "$S"
state_add "label:loom:operator" "$S"
OUT=$(champion_pr_pass "$S" true "new head commit abc1234" true MERGEABLE 2 pass)
assert_contains "$OUT" "HOLD_NOTICE:suppressed" "the re-hold after a rebase is silent (idempotency guard)"
assert_contains "$OUT" "NO_MERGE:hold" "a rebased-but-still-red PR is re-held, not laundered into a merge"
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 8: held-PR census counts the set, the conflicts, and the ones out at Doctor"
# #6445 below (loom:pr + loom:operator, CONFLICTING, no loom:changes-requested)
# is exactly the shape a hold-only-suspended PR (#6852) leaves behind: it never
# left loom:pr, so it is invisible to HELD_AT_DOCTOR but still counted in
# HELD_COUNT/HELD_CONFLICTING via the loom:operator query alone — this doubles
# as the #6852 "never silently dropped from reporting" check (#6720's core
# property), independent of whether the PR routed or was suspended.
CENSUS_FIXTURE='[
  {"number":6445,"createdAt":"2026-08-10T00:00:00Z","updatedAt":"2026-08-19T14:59:00Z","mergeable":"CONFLICTING","labels":[{"name":"loom:pr"},{"name":"loom:operator"}]},
  {"number":6484,"createdAt":"2026-08-12T00:00:00Z","updatedAt":"2026-08-19T14:59:00Z","mergeable":"CONFLICTING","labels":[{"name":"loom:changes-requested"},{"name":"loom:operator"}]},
  {"number":6621,"createdAt":"2026-08-18T00:00:00Z","updatedAt":"2026-08-22T09:00:00Z","mergeable":"MERGEABLE","labels":[{"name":"loom:pr"},{"name":"loom:operator"}]}
]'
assert_eq "3 2 1" "$(printf '%s\n' "$CENSUS_FIXTURE" | held_census)" \
    "census: 3 held, 2 conflicting, 1 out at Doctor (AC #5)"
assert_eq "3" "$(printf '%s\n' "$CENSUS_FIXTURE" | jq '[.[] | select([.labels[].name] | index("loom:pr") or index("loom:changes-requested"))] | length')" \
    "census: every held PR is captured whether it is still on loom:pr (routed or hold-only-suspended, #6852) or already at Doctor"
assert_eq "2026-08-10T00:00:00Z" \
    "$(printf '%s\n' "$CENSUS_FIXTURE" | jq -r 'min_by(.createdAt) | .createdAt')" \
    "census: the oldest held PR is identified for the age report"
assert_eq "0 0 0" "$(printf '%s\n' '[]' | held_census)" \
    "census: an empty held set reports zeros rather than erroring"
echo

# ---------------------------------------------------------------------
echo "Test 9: stale-notice marker is per-episode, not per-PR-forever (#6860)"
# Observed live on PR #6325 / #6207, 2026-08-24: an old stale-notice from a
# PAST episode (six days prior) sat on a PR that had since cycled back to
# loom:pr (proof it went through Doctor -> Judge, i.e. new commits landed)
# and was independently confirmed stale AGAIN, under an active hold. The
# marker-existence-only guard silently skipped it forever.

# Episode 1: PR goes stale at last_activity=T1, gets the notice + is routed
# to Doctor. This models the OLD notice from #6325/#6207's history.
T1="2026-08-18T09:00:00Z"
T2="2026-08-24T02:00:00Z"
S=$(state_new)
E1=$(champion_pr_pass "$S" false "" false MERGEABLE 30 pass "$T1")
assert_contains "$E1" "COMMENT:champion:stale-pr-notice" "episode 1: the first stale notice is posted"
assert_contains "$E1" "LABEL_ADD:loom:changes-requested" "episode 1: routed to Doctor"

# A re-tick within the SAME episode (no real new activity, LAST_ACTIVITY
# still T1) must stay suppressed — the "no duplicate comments" AC.
E1_RETICK=$(champion_pr_pass "$S" false "" false MERGEABLE 31 pass "$T1")
assert_contains "$E1_RETICK" "STALE_NOTICE:suppressed" \
    "a re-check within the same still-stale episode (same LAST_ACTIVITY) does not duplicate the notice (AC #3)"

# Doctor fixes it (a new commit, or a Judge comment -> LAST_ACTIVITY advances
# to T2) and it cycles back through Judge to loom:pr, exactly like
# #6325/#6207's history. Simulated directly on the state, since
# champion_pr_pass only models Champion's own transitions.
state_remove "label:loom:changes-requested" "$S"
state_add "label:loom:pr" "$S"

# Episode 2: time passes with NO further activity; the PR goes stale again at
# its NEW LAST_ACTIVITY (T2). The OLD marker (keyed T1) must not suppress this.
E2=$(champion_pr_pass "$S" false "" false MERGEABLE 46 pass "$T2")
assert_contains "$E2" "COMMENT:champion:stale-pr-notice" \
    "episode 2: a genuinely new staleness episode (LAST_ACTIVITY advanced since the old notice) gets a FRESH notice (AC #1/#2)"
assert_contains "$E2" "LABEL_ADD:loom:changes-requested" "episode 2: routed to Doctor again"
assert_eq "loom:changes-requested" "$(state_labels "$S")" \
    "episode 2: final labels reflect the fresh routing, not a stuck loom:pr"

# Both episode markers coexist — episode 1's marker was never erased, it was
# simply not a match for episode 2's key.
TESTS_RUN=$((TESTS_RUN + 1))
if state_has "marker:champion:stale-pr-notice:$T1" "$S" && state_has "marker:champion:stale-pr-notice:$T2" "$S"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: both episode markers are retained (T1 from episode 1, T2 from episode 2)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: expected both per-episode markers to be present"
fi

# A re-tick within episode 2 (still T2) is suppressed too — the fix is
# per-episode idempotency, not "never suppress again".
E2_RETICK=$(champion_pr_pass "$S" false "" false MERGEABLE 47 pass "$T2")
assert_contains "$E2_RETICK" "STALE_NOTICE:suppressed" \
    "a re-check within episode 2 (same new LAST_ACTIVITY) is suppressed too (AC #3)"
rm -f "$S"
echo

# ---------------------------------------------------------------------
echo "Test 10: per-PR digest — hold-reason extraction and row formatting (#6851)"

HOLD_BODY_GOOD="<!-- champion:merge-risk-hold -->
<!-- champion:hold-state head=abc1234 -->
**Champion: Holding for Human Merge**

This PR is Judge-approved and passes the mechanical safety criteria, but I am not
merging it automatically:

- **Blast radius**: touches merge-pr.sh's ordering guard — a regression there
  can delete a worktree branch before the merge lands"

assert_eq "**Blast radius**: touches merge-pr.sh's ordering guard — a regression there" \
    "$(digest_reason_from_body "$HOLD_BODY_GOOD")" \
    "digest extracts the specific axis/concern bullet from a well-formed hold comment"

assert_eq "reason unrecorded (no marker comment found)" \
    "$(digest_reason_from_body "")" \
    "digest falls back gracefully when no champion:merge-risk-hold comment is found"

HOLD_BODY_NO_BULLET="<!-- champion:merge-risk-hold -->
**Champion: Holding for Human Merge**

Some legacy or malformed hold comment with no axis bullet line."

assert_eq "hold marker present, axis bullet not parseable" \
    "$(digest_reason_from_body "$HOLD_BODY_NO_BULLET")" \
    "digest falls back gracefully when the marker is found but the axis bullet can't be parsed"

assert_eq "| #6445 | CONFLICTING, no reason | CONFLICTING, out at Doctor |" \
    "$(digest_row 6445 "CONFLICTING, no reason" "CONFLICTING" true)" \
    "digest row appends ', out at Doctor' when the PR carries loom:changes-requested"

assert_eq "| #6621 | override: loom:auto-merge-ok applied | MERGEABLE |" \
    "$(digest_row 6621 "override: loom:auto-merge-ok applied" "MERGEABLE" false)" \
    "digest row omits the Doctor suffix when the PR is still in the merge queue"
echo

# ---------------------------------------------------------------------
echo "Test 11: per-PR digest — conflict-duration tracking distinguishes fresh vs. rotting conflicts (#7020)"

NOW_T0="2026-08-20T00:00:00Z"
NOW_T1="2026-08-23T00:00:00Z"  # +3 days, still conflicting
NOW_T2="2026-08-24T00:00:00Z"  # PR cleared to MERGEABLE this tick
NOW_T3="2026-08-25T00:00:00Z"  # PR is CONFLICTING again, after the gap

# Tick 1: brand-new conflict, no prior digest body at all (first-run case,
# also covers "the digest issue itself does not exist yet" from the issue's
# own Test Plan).
SINCE_T1=$(conflict_since_for 9001 CONFLICTING "" "$NOW_T0")
assert_eq "$NOW_T0" "$SINCE_T1" \
    "tick 1: a first-seen conflict with no prior digest body starts the clock at 'now'"
assert_eq "0" "$(conflict_days "$SINCE_T1" "$NOW_T0")" \
    "tick 1: a conflict that started this same instant is 0 days old"

BODY_T1="<!-- champion:conflict-since:PR=9001 TS=${SINCE_T1} -->"

# Tick 2 (+3d): still conflicting — the digest carries the SAME since-
# timestamp forward from the prior pass's body, so duration accumulates
# rather than resetting on every tick.
SINCE_T2=$(conflict_since_for 9001 CONFLICTING "$BODY_T1" "$NOW_T1")
assert_eq "$NOW_T0" "$SINCE_T2" \
    "tick 2: an ongoing conflict reuses the ORIGINAL since-timestamp carried in the prior digest body"
DAYS_T2=$(conflict_days "$SINCE_T2" "$NOW_T1")
assert_eq "3" "$DAYS_T2" "tick 2: 3 continuous days of conflict is computed correctly"
assert_eq "true" "$([[ "$DAYS_T2" -ge "$ROT_THRESHOLD_DAYS" ]] && echo true || echo false)" \
    "tick 2: 3 days at the ROT_THRESHOLD_DAYS=3 boundary already counts as rotting (>=, not >)"
assert_eq "CONFLICTING since ${SINCE_T2}, 3d" \
    "$(digest_status CONFLICTING "$SINCE_T2" "$DAYS_T2" false)" \
    "tick 2: the digest row's Status cell reads 'CONFLICTING since <date>, Nd', matching the issue's own example format"

BODY_T2="<!-- champion:conflict-since:PR=9001 TS=${SINCE_T2} -->"

# Tick 3: the PR cleared (MERGEABLE) this pass — conflict_since_for returns
# empty, so NO conflict-since marker is written for it into this pass's
# digest body (mirroring champion-pr-merge.md's `if [ "$PR_MERGEABLE" =
# "CONFLICTING" ]` guard around the marker-emitting block).
SINCE_T3_CLEAR=$(conflict_since_for 9001 MERGEABLE "$BODY_T2" "$NOW_T2")
assert_eq "" "$SINCE_T3_CLEAR" "tick 3: a cleared (MERGEABLE) PR gets no conflict-since marker"
BODY_T3=""   # PR 9001's marker is genuinely absent from this pass's body

# Tick 4: CONFLICTING again. The prior body (BODY_T3) carries no marker for
# this PR — it was MERGEABLE last pass — so the clock RESETS to "now" rather
# than resuming the T0 origin. This is the exact edge case named in the
# issue body: "a held PR that flips CONFLICTING -> MERGEABLE -> CONFLICTING
# again ... should not accumulate rot time across the MERGEABLE gap."
SINCE_T4=$(conflict_since_for 9001 CONFLICTING "$BODY_T3" "$NOW_T3")
assert_eq "$NOW_T3" "$SINCE_T4" \
    "tick 4: re-conflicting after a MERGEABLE gap RESETS the since-timestamp to 'now'"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$SINCE_T4" != "$NOW_T0" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: tick 4 does NOT resume the original T0 origin — no rot-time accumulates across the MERGEABLE gap"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: tick 4 resumed the pre-gap origin — rot time would wrongly accumulate across a MERGEABLE gap"
fi
echo

# ---------------------------------------------------------------------
echo "Test 12: digest Status formatting and the aggregate held-clean/held-rotting split (#7020)"

assert_eq "MERGEABLE" "$(digest_status MERGEABLE "" "" false)" \
    "a MERGEABLE PR's Status cell carries no since/duration figure at all"
assert_eq "MERGEABLE, out at Doctor" "$(digest_status MERGEABLE "" "" true)" \
    "a MERGEABLE-but-out-at-Doctor PR keeps the existing Doctor suffix, unaffected by #7020"
assert_eq "CONFLICTING since 2026-08-20T00:00:00Z, 8d" \
    "$(digest_status CONFLICTING "2026-08-20T00:00:00Z" 8 false)" \
    "a rotting CONFLICTING PR's Status cell matches the issue's example format verbatim"
assert_eq "CONFLICTING since 2026-08-20T00:00:00Z, 8d, out at Doctor" \
    "$(digest_status CONFLICTING "2026-08-20T00:00:00Z" 8 true)" \
    "the since/duration figure and the Doctor suffix compose without clobbering each other"

# Aggregate line: 5 held, 3 conflicting (2 rotting >=3d, 1 still fresh/clean),
# 1 out at Doctor, oldest 12d.
assert_eq "Merge-risk holds: 5 open PR(s) — 3 conflicting (2 rotting >=3d, 1 clean), 1 out at Doctor, oldest 12d" \
    "$(digest_aggregate_line 5 3 2 1 12)" \
    "the aggregate line distinguishes held-clean from held-rotting counts (AC #2), additive to the existing N/C/D/A fields"

# A fully clean pile (every conflict younger than the threshold) still
# reports both counts explicitly rather than omitting the rotting figure.
assert_eq "Merge-risk holds: 2 open PR(s) — 2 conflicting (0 rotting >=3d, 2 clean), 0 out at Doctor, oldest 1d" \
    "$(digest_aggregate_line 2 2 0 0 1)" \
    "an all-clean pile reports '0 rotting' explicitly, not a silently-omitted figure"
echo

# ---------------------------------------------------------------------
echo "Test 13: the shipped markdown matches this mirror (drift guard)"

assert_doc_contains "$CHAMPION_MD" \
    "## Held-PR Health Pass (#6720)" \
    "champion-pr-merge.md ships the Held-PR Health Pass section"

assert_doc_contains "$CHAMPION_MD" \
    "MERGE_BLOCKED_BY_HOLD=true" \
    "the sticky-hold precheck sets MERGE_BLOCKED_BY_HOLD instead of dropping the PR"

assert_doc_contains "$CHAMPION_MD" \
    'HELD="${MERGE_BLOCKED_BY_HOLD:-false}"' \
    "the stale-PR block reads MERGE_BLOCKED_BY_HOLD to decide the loom:operator reversal"

assert_doc_contains "$CHAMPION_MD" \
    '<!-- champion:held-pr-conflict-notice -->' \
    "the held-PR conflict notice has its own idempotency marker (AC #2)"

assert_doc_contains "$CHAMPION_MD" \
    'any(startswith(\"$CONFLICT_MARKER\"))' \
    "the conflict notice uses the startswith idempotency guard (#5371), not a substring match"

assert_doc_contains "$CHAMPION_MD" \
    'Kept loom:operator on #$PR_NUMBER' \
    "the stale route keeps loom:operator when a hold is in force (AC #4)"

assert_doc_contains "$CHAMPION_MD" \
    "**Never delete or edit the \`champion:merge-risk-hold\` comment here**" \
    "the stale route forbids clearing the hold marker (AC #3)"

assert_doc_contains "$CHAMPION_MD" \
    "## Held-PR Census (report every pass, #6720)" \
    "champion-pr-merge.md ships the held-PR census (AC #5)"

assert_doc_contains "$CHAMPION_MD" \
    'HELD_CONFLICTING=$(printf '"'"'%s\n'"'"' "$HELD_JSON" | jq '"'"'[.[] | select(.mergeable == "CONFLICTING")] | length'"'" \
    "the census jq pipeline this test mirrors is the one shipped in the markdown"

assert_doc_contains "$CHAMPION_COMMON_MD" \
    "Merge-risk holds:" \
    "champion-common.md's Completion Report requires the merge-risk hold census line"

assert_doc_contains "$LABEL_SM_MD" \
    "The stale-PR route out of \`loom:pr\` (#5802, narrowed by #6720)" \
    "label-state-machine.md documents the loom:operator decision for the held route (AC #4)"

assert_doc_contains "$CHAMPION_MD" \
    'STALE_MARKER="<!-- champion:stale-pr-notice:$LAST_ACTIVITY -->"' \
    "the stale-PR notice marker is keyed per-episode on LAST_ACTIVITY, not posted-ever (#6860 AC #1)"

assert_doc_contains "$CHAMPION_MD" \
    "it reuses this same run's \`\$LAST_ACTIVITY\` as" \
    "the Held-PR Health Pass invocation documents reusing criterion #5's LAST_ACTIVITY as the episode key (#6860)"

# --- #6851: per-PR digest, durable across passes ---
assert_doc_contains "$CHAMPION_MD" \
    "### Per-PR Digest (durable across passes, #6851)" \
    "champion-pr-merge.md ships the per-PR digest section (AC #1)"

assert_doc_contains "$CHAMPION_MD" \
    'HOLD_MARKER="<!-- champion:merge-risk-hold -->"' \
    "the digest's per-PR reason extraction reads the SAME marker as the sticky-hold precheck, not a new one"

assert_doc_contains "$CHAMPION_MD" \
    "REASON=\$(printf '%s\\n' \"\$HOLD_BODY\" | grep -m1 -E '^- \\*\\*.+\\*\\*:' | sed 's/^- //')" \
    "the digest extracts the hold template's axis bullet ('- **<AXIS>**: <CONCERN>')"

assert_doc_contains "$CHAMPION_MD" \
    'DIGEST_TITLE="Champion: Merge-Risk Hold Digest"' \
    "the digest is persisted to a fixed-title pinned tracking issue (AC #2)"

assert_doc_contains "$CHAMPION_MD" \
    'DIGEST_MARKER="<!-- champion:merge-risk-hold-digest -->"' \
    "the pinned digest issue carries its own idempotency marker, following the existing marker convention (AC #2)"

assert_doc_contains "$CHAMPION_MD" \
    'gh issue edit "$DIGEST_ISSUE" --body "$DIGEST_BODY"' \
    "an existing digest issue is edited in place (its body overwritten), never re-created or commented on (AC #2)"

assert_doc_contains "$CHAMPION_MD" \
    '--label "loom:blocked"' \
    "the digest issue is filed with loom:blocked so it never enters Curator/Builder/Judge/Champion's own work queues"

assert_doc_contains "$CHAMPION_MD" \
    "not a work item" \
    "the digest issue body itself warns other roles it is not curatable/buildable work"

assert_doc_contains "$CHAMPION_COMMON_MD" \
    "Persist the per-PR merge-risk hold digest" \
    "champion-common.md's Completion Report requires the durable per-PR digest step (AC #2, AC #3)"

assert_doc_contains "$CHAMPION_COMMON_MD" \
    "Per-PR digest (mandatory, #6851)" \
    "champion-common.md documents the per-PR digest as additive to, not a replacement for, the aggregate census line (AC #4)"

assert_doc_contains "$CHAMPION_MD" \
    "Never let this block a merge decision" \
    "the digest is explicitly scoped as reporting-only — no change to Safety Criteria or merge behavior (AC #5)"

# --- absence pins: the short-circuiting behavior must not come back ---
assert_doc_lacks "$CHAMPION_MD" \
    'STALE_MARKER="<!-- champion:stale-pr-notice -->"' \
    "the un-keyed stale-notice marker (posted-ever, any episode) is gone (#6860)"

assert_doc_lacks "$CHAMPION_MD" \
    "Skip the PR for this pass regardless of how the axes read this tick" \
    "the outcomes table no longer says a sticky hold skips the whole PR"

assert_doc_lacks "$CHAMPION_MD" \
    "# Skip this PR for this pass — do not merge." \
    "the Hold behavior block no longer ends by dropping the PR from the pass"

assert_doc_lacks "$CHAMPION_MD" \
    "# merge-risk hold — the PR leaves the auto-merge queue either way" \
    "the stale route no longer claims it unconditionally exits the hold (#5802's stale premise)"

# --- #6843: criterion #5 must not derive staleness from `updatedAt` ---
# `updatedAt` is bumped by ANY PR write, including the Held-PR Health Pass's
# OWN comments (conflict notices, loom:operator reasserts) — so a held PR
# nobody but Champion touches never accumulates real staleness and the stale
# route above (Test 1-6) never fires in practice, even though this file's
# mirror (which takes hours_ago as a plain input) cannot see that failure
# mode itself. These pins guard the actual verification-command literal.
assert_doc_lacks "$CHAMPION_MD" \
    "UPDATED_AT=\$(gh pr view <number> --json updatedAt --jq '.updatedAt')" \
    "criterion #5 no longer derives staleness from the bot-comment-bumped updatedAt field (#6843)"

assert_doc_contains "$CHAMPION_MD" \
    'PR_DATA=$(gh pr view <number> --json createdAt,commits,comments)' \
    "criterion #5 reads commits + comments (+ createdAt floor) instead of updatedAt (#6843)"

assert_doc_contains "$CHAMPION_MD" \
    'select((.body | test("champion:|Automated by Champion role")) | not) | .createdAt)' \
    "criterion #5's activity computation excludes Champion's own comments, mirroring the sticky-hold precheck's exclusion test (#6843)"

# --- #6852: hold-only vs hold-plus-feedback stale routing ---
assert_doc_contains "$CHAMPION_MD" \
    "HELD_CI_FAILING" \
    "the Held-PR Health Pass computes HELD_CI_FAILING to distinguish hold-only from hold-plus-feedback staleness (#6852)"

assert_doc_contains "$CHAMPION_MD" \
    "### #5 under a hold — route to Doctor only when there is ALSO genuine feedback (#6852)" \
    "the Held-PR Health Pass ships the #6852 hold-only-vs-hold-plus-feedback routing decision"

assert_doc_contains "$CHAMPION_MD" \
    "### Hold-only Stale PR — suspend the route, report once (#6852)" \
    "champion-pr-merge.md ships the hold-only suspension block (#6852)"

assert_doc_contains "$CHAMPION_MD" \
    '<!-- champion:held-stale-suspended -->' \
    "the hold-only suspension notice has its own idempotency marker (#6852)"

assert_doc_contains "$CHAMPION_MD" \
    'if [ "$HELD" = true ] && [ "$CI_FAILING" != true ]; then' \
    "the hold-plus-feedback/unheld stale block fails closed rather than routing a hold-only PR if ever reached (#6852)"

assert_doc_contains "$CHAMPION_MD" \
    "this block changes no label at all" \
    "the hold-only suspension block documents that it never swaps loom:pr -> loom:changes-requested (#6852)"

assert_doc_contains "$CHAMPION_MD" \
    "Evaluate #4, then #6, then #5" \
    "the Held-PR Health Pass documents the reordered evaluation sequence (#6852)"

# --- absence pin: a hold-only PR must never be silently dropped from
# reporting — #6720's core property, re-verified after #6852 ---
assert_doc_lacks "$CHAMPION_MD" \
    "### #5 under a hold — route to Doctor, keep the hold" \
    "the pre-#6852 unconditional held-route heading is gone (superseded, not just supplemented)"

# --- #7020: per-PR conflict-duration tracking, and the held-clean/
# held-rotting split in the digest's aggregate line ---
assert_doc_contains "$CHAMPION_MD" \
    "**Step 0 — locate the existing digest issue and read its current body" \
    "the digest gained a Step 0 that reads its OWN previous body, ahead of Step 1 (#7020)"

assert_doc_contains "$CHAMPION_MD" \
    'OLD_DIGEST_BODY=$("$GH_READ" issue view "$DIGEST_ISSUE" --json body --jq '"'"'.body'"'"')' \
    "Step 0 reads the pinned digest issue's own body so Step 1 can carry the conflict-since clock forward across passes (#7020)"

assert_doc_contains "$CHAMPION_MD" \
    'CONFLICT_SINCE_PREFIX="<!-- champion:conflict-since:PR="' \
    "each held PR's first-seen-CONFLICTING timestamp is persisted under its own durable marker (#7020 AC #1)"

assert_doc_contains "$CHAMPION_MD" \
    "ROT_THRESHOLD_DAYS=3" \
    "a rot threshold distinguishes a fresh conflict from a rotting one (#7020 AC #1/#2)"

assert_doc_contains "$CHAMPION_MD" \
    'PRIOR_SINCE=$(printf '"'"'%s\n'"'"' "$OLD_DIGEST_BODY" | grep -o "${CONFLICT_SINCE_PREFIX}${PR_NUM} TS=[0-9TZ:-]*" | head -1 | sed -E '"'"'s/.*TS=//'"'"')' \
    "the since-timestamp is carried forward from the PREVIOUS pass's digest body, not re-derived from a fresh 'now' every tick (#7020)"

assert_doc_contains "$CHAMPION_MD" \
    'STATUS="${STATUS} since ${CONFLICT_SINCE}, ${CONFLICT_DAYS}d"' \
    "a CONFLICTING held PR's digest row shows 'CONFLICTING since <date>, Nd', matching the issue's own example format (#7020 AC #1)"

assert_doc_contains "$CHAMPION_MD" \
    '$HELD_ROTTING rotting >=${ROT_THRESHOLD_DAYS}d, $HELD_CONFLICTING_CLEAN clean' \
    "the digest's aggregate line distinguishes held-clean from held-rotting counts (#7020 AC #2)"

assert_doc_contains "$CHAMPION_MD" \
    '$CONFLICT_SINCE_MARKERS---' \
    "the per-PR conflict-since markers are written back into the digest body so the NEXT pass's Step 0 can read them (#7020)"

assert_doc_contains "$CHAMPION_MD" \
    "a \`MERGEABLE\` pass never writes the marker for that PR" \
    "the doc names the CONFLICTING -> MERGEABLE -> CONFLICTING reset edge case explicitly (#7020 Test Plan)"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
