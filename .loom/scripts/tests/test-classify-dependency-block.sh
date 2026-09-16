#!/usr/bin/env bash
# test-classify-dependency-block.sh - Tests for classify-dependency-block.sh
# (issue #5664), the timing-vs-merits split in Champion's escalation decision.
#
# Champion escalated a proposal to `loom:operator-only` after N=2 unrevised
# evaluations regardless of WHY it kept failing. For a proposal whose only
# finding was "hard dependency on #3, which is still open" that turned a
# self-clearing timing condition into a permanent one: `loom:operator-only`
# makes Champion skip the issue forever, so the only actor that could notice #3
# had closed was the one told to ignore it. In the motivating incident #3 merged
# minutes after three dependents were escalated on it.
#
# This suite is hybrid, mirroring test-detect-dependency-cycle.sh:
#
#   1. extract_findings / is_dependency_finding / findings_are_dependency_only /
#      _extract_refs / _fingerprint are real sourceable functions (the script
#      guards its main on `BASH_SOURCE == $0`), tested directly.
#   2. Both modes, their exit codes, and the --apply side effects are exercised
#      black-box by stubbing `gh` on PATH and running the real script.
#   3. The Champion prose call sites are pinned with literal assert_doc_contains
#      checks, so wiring the gate out of the role files fails here.
#
# Hermetic: no network, no live forge, no tokens. Every read goes to the stub.
#
# Usage:
#   ./.loom/scripts/tests/test-classify-dependency-block.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
CDB="$SCRIPTS_DIR/classify-dependency-block.sh"

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
CHAMPION_PROMO_MD="$PROMPT_DIR/champion-issue-promo.md"
CHAMPION_MD="$PROMPT_DIR/champion.md"
CHAMPION_REF_MD="$PROMPT_DIR/champion-reference.md"
CURATOR_MD="$PROMPT_DIR/curator.md"

# Source for the pure helpers BEFORE defining our own colors (the sourced chain
# defines RED/YELLOW/BLUE/NC itself).
# shellcheck source=/dev/null
source "$CDB"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected NOT to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

assert_true() { if "$@"; then pass "${!#}"; else fail "${!#}"; fi; }

assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    if grep -qF -- "$needle" "$file"; then
        pass "$msg"
    else
        fail "$msg (missing literal in $file: $needle)"
    fi
}

# =====================================================================
# Fixture comment bodies (the real shapes champion-issue-promo.md posts)
# =====================================================================

# The escalation comment from the #5664 incident, verbatim in shape.
ESCALATION_DEP_ONLY='<!-- champion:proposal-escalated -->
**Champion: Escalating to Operator — Repeated Rejection Without Revision**

This proposal has been evaluated 2+ times with converging feedback (1 posted
rejection(s) plus 1 silent skip(s) of an unchanged proposal), but has not been
revised to address it.

**Recurring findings:**
- Technical Feasibility (no obvious blockers): Hard dependency on #3, which is
  still open.

A human needs to decide whether to revise this proposal, close it, or accept it
as-is.

---
*Automated by Champion role*'

ESCALATION_MERITS='<!-- champion:proposal-escalated -->
**Champion: Escalating to Operator — Repeated Rejection Without Revision**

**Recurring findings:**
- Implementation Clarity: acceptance criteria are still not testable.
- Technical Feasibility: hard dependency on #3, which is still open.

A human needs to decide.

---
*Automated by Champion role*'

# shellcheck disable=SC2016  # literal marker text, not an expansion
REJECT_DEP_ONLY='<!-- champion:proposal-verdict:body-abc123 -->
<!-- champion:unrevised-skips:abc123:1 -->
**Champion Review: NEEDS REVISION**

This issue requires additional work before promotion to `loom:issue`:

- Technical Feasibility (no obvious blockers): depends on #3, which is still open.

**Recommended actions:**
- Wait for #3 to land, then resubmit.

---
*Automated by Champion role*'

# shellcheck disable=SC2016  # literal marker text, not an expansion
REJECT_MERITS='<!-- champion:proposal-verdict:body-abc123 -->
**Champion Review: NEEDS REVISION**

This issue requires additional work before promotion to `loom:issue`:

- Scope Appropriateness: this is three issues in one and cannot be built atomically.

**Recommended actions:**
- Split into per-phase issues (see #3 for the pattern).

---
*Automated by Champion role*'

# =====================================================================
# Unit tests: the real sourced helpers
# =====================================================================

echo "--- extract_findings: the first bullet block, continuations folded ---"

out="$(extract_findings "$ESCALATION_DEP_ONLY")"
assert_eq "- Technical Feasibility (no obvious blockers): Hard dependency on #3, which is   still open." \
    "$out" "escalation comment: one folded finding from **Recurring findings:**"

out="$(extract_findings "$ESCALATION_MERITS")"
assert_eq 2 "$(printf '%s\n' "$out" | grep -c '^-')" "escalation comment with two findings yields two lines"

out="$(extract_findings "$REJECT_DEP_ONLY")"
assert_contains "$out" "depends on #3" "NEEDS REVISION comment: the failing-criteria bullet is captured"
assert_not_contains "$out" "Wait for #3 to land" \
    "**Recommended actions:** bullets are NOT findings (a suggestion citing an issue must not read as a blocker)"

assert_eq "" "$(extract_findings 'No bullets at all, just prose.')" \
    "a comment with no bullet block yields no findings"

echo
echo "--- is_dependency_finding: dependency word AND a reference, both required ---"

assert_true is_dependency_finding '- Technical Feasibility: hard dependency on #3, which is still open' \
    "dependency word + #N is a dependency finding"
assert_true is_dependency_finding '- Blocked by example-org/tool-repo#202' \
    "cross-repo reference is a dependency finding"
if is_dependency_finding '- Technical Feasibility: requires a migration plan before this can land'; then
    fail "a dependency WORD with no reference is a merits finding (ordinary English must not defer)"
else
    pass "a dependency WORD with no reference is a merits finding (ordinary English must not defer)"
fi
if is_dependency_finding '- Implementation Clarity: acceptance criteria are untestable, see #12 for the pattern'; then
    fail "a reference with no dependency word is a merits finding"
else
    pass "a reference with no dependency word is a merits finding"
fi

# #4196/#6112: a bare "Dependencies" noun (quoting a section heading) plus an
# incidental, unrelated issue-number mention must NOT read as a dependency
# finding. The real #4196 escalation bullet, reproduced verbatim in shape.
FINDING_4196='- Scope appropriateness: the issue'"'"'s own "Dependencies / references" section recommends filing Phase 2 as its own issue. Phase 1 is shipped (#3997).'
if is_dependency_finding "$FINDING_4196"; then
    fail "a bare 'Dependencies' section-heading reference is a merits finding, not a dependency wait (#6112)"
else
    pass "a bare 'Dependencies' section-heading reference is a merits finding, not a dependency wait (#6112)"
fi
assert_true is_dependency_finding '- Technical Feasibility: hard dependency on private/repo#77, which is still open' \
    "'dependency on' + #N still classifies as a dependency finding after the #6112 fix"
assert_true is_dependency_finding '- Technical Feasibility: this has a dependency of the RTL work on #3' \
    "'dependency of' + #N still classifies as a dependency finding"

# #7652: "cannot start until #N" describes the same sequential-ordering
# relationship as "blocked by #N" but used none of the previously recognized
# keywords, so it misclassified as a merits finding and defeated the #5664
# self-clearing-timing-block protection. Verbatim wording from the real #7431
# escalation comment that exposed the gap.
assert_true is_dependency_finding '- Scope: this issue is explicitly sequential (3 of 3, final) and cannot start until #7430 closes.' \
    "'cannot start until' + #N is a dependency finding (#7652, verbatim #7431 wording)"
assert_true is_dependency_finding '- Scope: this work cannot proceed until private/repo#88 lands' \
    "'cannot proceed until' + #N is a dependency finding (#7652)"
assert_true is_dependency_finding '- Scope: implementation cannot begin work until #12 merges' \
    "'cannot begin work until' + #N is a dependency finding (#7652)"
assert_true is_dependency_finding '- Scope: this is not startable until #9 is resolved' \
    "'not startable until' + #N is a dependency finding (#7652)"
assert_true is_dependency_finding '- Scope: we must wait until #14 before touching this' \
    "'must wait until' + #N is a dependency finding (#7652)"
assert_true is_dependency_finding '- Scope: we must wait for #14 before touching this' \
    "'must wait for' + #N is a dependency finding (#7652)"
if is_dependency_finding '- Scope: this work cannot start soon given the current backlog'; then
    fail "'cannot start' with no 'until' and no reference is a merits finding (#7652)"
else
    pass "'cannot start' with no 'until' and no reference is a merits finding (#7652)"
fi

# #7431/#7756: a phrase-list word ("prerequisite") used in ordinary prose,
# co-occurring in the same bullet with an issue reference that is NOT what the
# phrase is describing, must not read as a dependency finding. Verbatim shape
# of the real #7431 escalation comment that exposed the gap -- #7430 is named
# narratively (it merged minutes earlier and is why a soak window hasn't
# started), not cited as a "Blocked by"/"Depends on"/"Requires" blocker.
FINDING_7431='- Scope/Sequencing: #7430 (per-sweep resource limits + containment observability — a prerequisite for any meaningful soak) merged only minutes before this evaluation, so no soak observation window has started yet.'
if is_dependency_finding "$FINDING_7431"; then
    fail "'prerequisite' narrating an unrelated issue reference is a merits finding, not a dependency wait (#7756)"
else
    pass "'prerequisite' narrating an unrelated issue reference is a merits finding, not a dependency wait (#7756)"
fi
assert_true is_dependency_finding '- Technical Feasibility: this work has a hard prerequisite on #3, which is still open' \
    "'prerequisite on #N' immediately following the reference still classifies as a dependency finding after the #7756 fix"
assert_true is_dependency_finding '- Scope: this is a hard prerequisite blocked by #3' \
    "'prerequisite' bullet with the reference immediately after a dependency word still classifies as a dependency finding"

# #7784: the bare verb/noun family ("blocks"/"blocking"/"blocker") is the one
# part of the phrase list whose natural word order puts the reference BEFORE
# the phrase ("#N blocks this", "#N is the blocker"), so #7756's
# after-phrase-only window silently dropped those shapes. Both word orders must
# classify as dependency findings; the narrow leading window must NOT reopen
# the #7431 false positive (guarded again immediately below and in the
# --check-defer regression block further down).
assert_true is_dependency_finding '- #7430 blocks this proposal' \
    "ref-first 'blocks' is a dependency finding (#7784)"
assert_true is_dependency_finding '- #7430 is the blocker here' \
    "ref-first 'blocker' is a dependency finding (#7784)"
assert_true is_dependency_finding '- #7430 is still blocking this work' \
    "ref-first 'blocking' is a dependency finding (#7784)"
assert_true is_dependency_finding '- Scope: private/repo#88 blocks this proposal' \
    "ref-first 'blocks' with a cross-repo reference is a dependency finding (#7784)"
# The ref-after word orders that already worked under #7756 must keep working.
assert_true is_dependency_finding '- This blocks on #7430' \
    "ref-after 'blocks' still classifies as a dependency finding (#7784)"
assert_true is_dependency_finding '- Blocking dependency: #7430' \
    "ref-after 'blocking' still classifies as a dependency finding (#7784)"
assert_true is_dependency_finding '- The open blocker is tracked at #7430' \
    "ref-after 'blocker' still classifies as a dependency finding (#7784)"
# The leading window is deliberately narrow: a reference far upstream of a
# bare-verb/noun phrase is narrative co-occurrence, not a citation.
if is_dependency_finding '- Scope: #7430 merged minutes before this evaluation and there is nothing blocking here'; then
    fail "a reference far upstream of 'blocking' is a merits finding, not a dependency wait (#7784)"
else
    pass "a reference far upstream of 'blocking' is a merits finding, not a dependency wait (#7784)"
fi
# Narrowest-possible regression restatement of #7756: the #7431 bullet is
# unaffected by the leading window (it uses "prerequisite", a prepositional-
# family phrase, which keeps after-phrase-only directionality).
if is_dependency_finding "$FINDING_7431"; then
    fail "the #7431 bullet stays a merits finding after the #7784 leading-window fix"
else
    pass "the #7431 bullet stays a merits finding after the #7784 leading-window fix"
fi

echo
echo "--- findings_are_dependency_only: one merits finding disqualifies the set ---"

assert_true findings_are_dependency_only "$(extract_findings "$ESCALATION_DEP_ONLY")" \
    "all-dependency findings are dependency-only"
if findings_are_dependency_only "$(extract_findings "$ESCALATION_MERITS")"; then
    fail "a mixed set (merits + dependency) is NOT dependency-only"
else
    pass "a mixed set (merits + dependency) is NOT dependency-only"
fi
if findings_are_dependency_only ""; then
    fail "an empty finding set is not dependency-only"
else
    pass "an empty finding set is not dependency-only"
fi

echo
echo "--- _extract_refs: normalization of every reference form ---"

assert_eq "o/r#3" "$(_extract_refs 'depends on #3' 'o/r')" "bare #N normalizes to the citing repo"
assert_eq "a/b#9" "$(_extract_refs 'blocked by a/b#9' 'o/r')" "explicit owner/repo#N keeps its own repo"
assert_eq "o/x#56" "$(_extract_refs 'blocked by https://github.com/o/x/issues/56' 'o/r')" \
    "issue URL normalizes to owner/repo#N"
assert_eq "o/x#7" "$(_extract_refs 'blocked by https://github.com/o/x/pull/7' 'o/r')" \
    "pull-request URL normalizes too (a blocker may be a PR)"

echo
echo "--- BODY_HASH (#7650 Test Plan): an appended ## Revision section changes champion-issue-promo.md's hash ---"
# Reproduces champion-issue-promo.md's own BODY_HASH formula exactly
# (printf '%s\n%s' "$title" "$body" | _sha256 | ...) rather than assuming it
# holds -- the Test Plan explicitly calls this out as something to verify,
# not take on faith.
ORIG_TITLE='Fix the stale toolchain pin'
ORIG_BODY='A proposal with a stale toolchain pin.'
ORIG_HASH="$(printf '%s\n%s' "$ORIG_TITLE" "$ORIG_BODY" | _sha256 | awk '{print substr($1, 1, 16)}')"
# shellcheck disable=SC2016  # literal backtick/marker text, not an expansion
REVISED_BODY="$(printf '%s\n\n## Revision (2026-09-14)\n\nCurator re-verified every objection Champion cited against `deadbeef` and found all of them resolved.\n\n<!-- curator:fact-revision:fact-abc123 -->' "$ORIG_BODY")"
REVISED_HASH="$(printf '%s\n%s' "$ORIG_TITLE" "$REVISED_BODY" | _sha256 | awk '{print substr($1, 1, 16)}')"
if [[ "$ORIG_HASH" != "$REVISED_HASH" ]]; then
    pass "appending a ## Revision section changes BODY_HASH -- Champion's VERDICT_MARKER for the old hash no longer matches, so the next pass evaluates fresh instead of skipping or re-escalating"
else
    fail "appending a ## Revision section changes BODY_HASH -- Champion's VERDICT_MARKER for the old hash no longer matches, so the next pass evaluates fresh instead of skipping or re-escalating"
fi

echo
echo "--- _fingerprint: identity of the blocker SET, order-independent ---"

assert_eq "$(_fingerprint 'o/r#3 o/r#5')" "$(_fingerprint 'o/r#5 o/r#3')" \
    "same set in either order fingerprints identically"
if [[ "$(_fingerprint 'o/r#3')" == "$(_fingerprint 'o/r#4')" ]]; then
    fail "a different blocker set fingerprints differently"
else
    pass "a different blocker set fingerprints differently"
fi

# =====================================================================
# Black-box: stub `gh` on PATH, run the real script
# =====================================================================

STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cdb-test.XXXXXX")"
cleanup() { rm -rf "$STUB_DIR"; }
trap cleanup EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Minimal `gh` stub: serves fixtures from issue-<owner>_<repo>_<N>.json and
# pr-<owner>_<repo>_<N>.json. Records comments/labels to per-issue logs.
STUB_DIR="$(cd "$(dirname "$0")" && pwd)"
printf '%s\n' "$*" >> "$STUB_DIR/calls.log"

[[ "${1:-}" == "--version" ]] && { echo "gh version 0.0.0 (stub)"; exit 0; }
kind="${1:-}"
case "$kind" in issue|pr) ;; *) echo "stub gh: unhandled args: $*" >&2; exit 3 ;; esac

action="$2"; num="$3"; shift 3
repo=""; jqexpr=""; bodyarg=""; addlabel=""; rmlabel=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)          repo="$2"; shift 2 ;;
    --json)          shift 2 ;;
    --jq|-q)         jqexpr="$2"; shift 2 ;;
    --body)          bodyarg="$2"; shift 2 ;;
    --add-label)     addlabel="$2"; shift 2 ;;
    --remove-label)  rmlabel="$2"; shift 2 ;;
    *)               shift ;;
  esac
done

key="$(printf '%s' "$repo#$num" | tr '/#' '__')"
prefix="issue"; [[ "$kind" == "pr" ]] && prefix="pr"
f="$STUB_DIR/$prefix-$key.json"

# Failure injection (write-ordering tests, #5688 review): fail one specific
# mutating call so a partial write -- one of the two independent `gh` calls in
# _apply_unescalation succeeding and the other not -- can be simulated exactly.
if [[ -n "${GH_STUB_FAIL_REMOVE_LABEL:-}" && "$action" == "edit" && "$rmlabel" == "$GH_STUB_FAIL_REMOVE_LABEL" ]]; then
  echo "stub gh: simulated API failure removing $rmlabel" >&2; exit 1
fi
if [[ -n "${GH_STUB_FAIL_COMMENT:-}" && "$action" == "comment" ]]; then
  echo "stub gh: simulated API failure posting comment" >&2; exit 1
fi

case "$action" in
  view)
    [[ -f "$f" ]] || { echo "gh: not found" >&2; exit 1; }
    if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" < "$f"; else cat "$f"; fi
    ;;
  comment)
    [[ -f "$f" ]] || { echo "gh: not found" >&2; exit 1; }
    printf '%s\n' "$bodyarg" >> "$STUB_DIR/comments-$key.log"
    tmp="$(mktemp)"
    jq --arg b "$bodyarg" '.comments += [{"body":$b}]' "$f" > "$tmp" && mv "$tmp" "$f"
    ;;
  edit)
    [[ -f "$f" ]] || { echo "gh: not found" >&2; exit 1; }
    [[ -n "$addlabel" ]] && printf 'ADD %s\n' "$addlabel" >> "$STUB_DIR/labels-$key.log"
    if [[ -n "$rmlabel" ]]; then
      printf 'REMOVE %s\n' "$rmlabel" >> "$STUB_DIR/labels-$key.log"
      tmp="$(mktemp)"
      jq --arg l "$rmlabel" '.labels = [(.labels // [])[] | select(.name != $l)]' "$f" > "$tmp" && mv "$tmp" "$f"
    fi
    if [[ -n "$bodyarg" ]]; then
      printf '%s\n' "$bodyarg" >> "$STUB_DIR/body-edits-$key.log"
      tmp="$(mktemp)"
      jq --arg b "$bodyarg" '.body = $b' "$f" > "$tmp" && mv "$tmp" "$f"
    fi
    ;;
  *)
    echo "stub gh: unhandled action: $action" >&2; exit 3 ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

export PATH="$STUB_DIR:$PATH"
export GH_CACHE_DISABLE=1

# issue_fixture <owner/repo#N> <STATE> <body> [labels csv] [comment...]
issue_fixture() {
    local node="$1" state="$2" body="$3" labels="${4:-}"
    shift 4 2>/dev/null || shift $#
    local key; key="$(printf '%s' "$node" | tr '/#' '__')"
    local comments_json="[]" c
    for c in "$@"; do
        comments_json="$(jq -n --argjson a "$comments_json" --arg b "$c" '$a + [{"body":$b}]')"
    done
    jq -n --arg s "$state" --arg b "$body" --arg l "$labels" --argjson c "$comments_json" \
        '{state:$s, body:$b, comments:$c,
          labels: ($l | if . == "" then [] else split(",") | map({name:.}) end)}' \
        > "$STUB_DIR/issue-$key.json"
}

pr_fixture() {
    local node="$1" state="$2"
    local key; key="$(printf '%s' "$node" | tr '/#' '__')"
    jq -n --arg s "$state" '{state:$s, body:"", comments:[], labels:[]}' > "$STUB_DIR/pr-$key.json"
}

reset_state() {
    rm -f "$STUB_DIR"/issue-*.json "$STUB_DIR"/pr-*.json \
          "$STUB_DIR"/comments-*.log "$STUB_DIR"/labels-*.log "$STUB_DIR"/body-edits-*.log \
          "$STUB_DIR/calls.log"
}

# run_cdb <args...> -> sets OUT / RC
run_cdb() {
    OUT="$("$CDB" --no-cache "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
}

labels_log() { cat "$STUB_DIR/labels-o_r_$1.log" 2>/dev/null; }
comments_log() { cat "$STUB_DIR/comments-o_r_$1.log" 2>/dev/null; }
body_edits_log() { cat "$STUB_DIR/body-edits-o_r_$1.log" 2>/dev/null; }
issue_body() { jq -r '.body' "$STUB_DIR/issue-o_r_$1.json"; }

# =====================================================================
# --check-defer
# =====================================================================

echo
echo "--- --check-defer: open single-hop non-cycle dependency -> DEFER ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect' "$REJECT_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'The sim harness bootstrap.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "0" "$RC" "exit 0 - defer applies"
assert_contains "$OUT" "DEFER" "DEFER marker present"
assert_contains "$OUT" "OPEN_BLOCKERS: o/r#3" "the open blocker is named"
assert_contains "$OUT" "BLOCKER_FINGERPRINT: " "a blocker-set fingerprint is emitted for the defer marker"
assert_not_contains "$(labels_log 5)" "loom:operator-only" "deferring never touches a label"
assert_eq "" "$(comments_log 5)" "deferring never posts a comment"

echo
echo "--- --check-defer: the blocker has since CLOSED -> REEVALUATE, not escalate ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect' "$REJECT_DEP_ONLY"
issue_fixture 'o/r#3' CLOSED 'The sim harness bootstrap.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "3" "$RC" "exit 3 - the recorded verdict is stale, re-run the criteria"
assert_contains "$OUT" "REEVALUATE" "REEVALUATE marker present"
assert_contains "$OUT" "REASON: blockers-cleared" "reason names the cleared blockers"

echo
echo "--- --check-defer: a MERGED PR blocker counts as resolved ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect' \
    '**Champion Review: NEEDS REVISION**

- Technical Feasibility: blocked by #9 (the harness PR), still open.
'
pr_fixture 'o/r#9' MERGED
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "3" "$RC" "a merged PR blocker resolves (gh issue view falls back to gh pr view)"
assert_contains "$OUT" "REEVALUATE" "merged PR blocker -> REEVALUATE"

echo
echo "--- REGRESSION GUARD: a merits finding still escalates, unchanged ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect' "$REJECT_MERITS"
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "exit 1 - escalate exactly as before"
assert_contains "$OUT" "NO_DEFER" "NO_DEFER marker present"
assert_contains "$OUT" "REASON: merits-finding" "reason is the merits finding, not the open dependency"

echo
echo "--- REGRESSION GUARD (#7756): a 'prerequisite' narrating an unrelated, closed issue still escalates, not REEVALUATE ---"
# The exact #7431 incident shape: the finding narratively mentions #7430 (a
# just-merged PR) as WHY a soak window hasn't started, using the phrase-list
# word "prerequisite" in ordinary prose -- not a "Blocked by"/"Depends
# on"/"Requires" citation of #7430 as this proposal's blocker. #7430 being
# CLOSED must not misclassify the finding as a self-clearing dependency wait.
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect' \
    '**Champion Review: NEEDS REVISION**

- Scope/Sequencing: #7430 (per-sweep resource limits + containment observability — a prerequisite for any meaningful soak) merged only minutes before this evaluation, so no soak observation window has started yet.
'
issue_fixture 'o/r#7430' CLOSED 'Per-sweep resource limits.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "exit 1 - escalate on the merits, exactly like the real #7431 incident"
assert_contains "$OUT" "NO_DEFER" "NO_DEFER marker present"
assert_contains "$OUT" "REASON: merits-finding" \
    "reason is merits-finding, not blockers-cleared -- 'prerequisite' narrating a closed, unrelated issue must not self-clear the escalation (#7756)"

echo
echo "--- REGRESSION GUARD: mixed findings (merits + dependency) still escalate ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect' \
    '**Champion Review: NEEDS REVISION**

- Implementation Clarity: acceptance criteria are not testable.
- Technical Feasibility: depends on #3, still open.
'
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "one merits finding disqualifies the whole set"
assert_contains "$OUT" "REASON: merits-finding" "mixed set reports merits-finding"

# Premise-false interaction with --check-defer (#7904) is covered in the
# sibling module test-classify-dependency-block-premise-false.sh -- extracted
# to keep this file under the file-size-policy.md threshold.

echo
echo "--- REGRESSION GUARD: a real dependency CYCLE still escalates ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect' "$REJECT_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'Depends on #5 first.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "a cycle cannot self-clear, so it must not defer"
assert_contains "$OUT" "REASON: dependency-cycle" "reason routes to the existing cycle gate"

echo
echo "--- --check-defer: no verdict comment / no findings ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect'
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "no recorded findings -> no basis to defer"
assert_contains "$OUT" "REASON: no-findings" "reason is no-findings"

echo
echo "--- --check-defer: a finding citing only the proposal's OWN number ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal with no declared dependencies.' 'loom:architect' \
    '**Champion Review: NEEDS REVISION**

- Technical Feasibility: this depends on #5 landing first (self-reference).
'
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "a self-reference is not a blocker and the body declares none"
assert_contains "$OUT" "REASON: no-recorded-blocker" "reason is no-recorded-blocker"

echo
echo "--- --check-defer: falls back to the body's declared dependencies ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal.

## Dependencies
- [ ] Blocked by #3 (sim harness bootstrap)' 'loom:architect' \
    '**Champion Review: NEEDS REVISION**

- Technical Feasibility: this depends on #5 landing first (self-reference).
'
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "0" "$RC" "the body's ## Dependencies checklist supplies the blocker when the findings cite none usable"
assert_contains "$OUT" "OPEN_BLOCKERS: o/r#3" "blocker resolved from the issue body"

# =====================================================================
# --check-unescalate
# =====================================================================

echo
echo "--- --check-unescalate: recorded blocker still OPEN -> leave it alone ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$REJECT_DEP_ONLY" "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "exit 1 - nothing to heal yet"
assert_contains "$OUT" "REASON: blocker-still-open" "reason names the still-open blocker"
assert_eq "" "$(labels_log 5)" "no label change while the blocker is open"
assert_eq "" "$(comments_log 5)" "no re-comment while the blocker is open"

echo
echo "--- --check-unescalate: blocker CLOSED -> un-escalate and comment once ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$REJECT_DEP_ONLY" "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "0" "$RC" "exit 0 - un-escalation applies"
assert_contains "$OUT" "UNESCALATE" "UNESCALATE marker present"
assert_contains "$OUT" "CLEARED_BLOCKERS: o/r#3" "the cleared blocker is named"
assert_contains "$OUT" "UNESCALATED: o/r#5" "--apply reports what it changed"
assert_contains "$(labels_log 5)" "REMOVE loom:operator-only" "loom:operator-only is removed"
assert_contains "$(comments_log 5)" "champion:proposal-unescalated:" "one fingerprinted un-escalation comment is posted"

echo
echo "--- --check-unescalate: idempotent - a re-applied label is not fought ---"
# The fixture from the previous block already carries the un-escalation comment;
# simulate a human deliberately re-adding the label.
tmp="$(mktemp)"
jq '.labels += [{"name":"loom:operator-only"}]' "$STUB_DIR/issue-o_r_5.json" > "$tmp" && mv "$tmp" "$STUB_DIR/issue-o_r_5.json"
rm -f "$STUB_DIR/labels-o_r_5.log" "$STUB_DIR/comments-o_r_5.log"
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "exit 1 - the same blocker set was already un-escalated once"
assert_contains "$OUT" "REASON: already-unescalated" "idempotency marker short-circuits the second attempt"
assert_eq "" "$(labels_log 5)" "no second label removal"
assert_eq "" "$(comments_log 5)" "no second comment"

echo
echo "--- --check-unescalate: a loom:operator-blocked sub-label (#5671) is removed alongside the base label ---"
reset_state
issue_fixture 'o/r#8' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only,loom:operator-blocked' \
    "$REJECT_DEP_ONLY" "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
run_cdb --issue 8 --repo o/r --check-unescalate --apply
assert_eq "0" "$RC" "exit 0 - un-escalation applies"
assert_contains "$(labels_log 8)" "REMOVE loom:operator-only" "loom:operator-only is removed"
assert_contains "$(labels_log 8)" "REMOVE loom:operator-blocked" \
    "loom:operator-blocked never outlives the base label it accompanies (#5671)"
if jq -e '[.labels[].name] | (contains(["loom:operator-only"]) or contains(["loom:operator-blocked"])) | not' \
    "$STUB_DIR/issue-o_r_8.json" >/dev/null; then
    pass "neither label remains on the issue after un-escalation"
else
    fail "neither label remains on the issue after un-escalation"
fi

echo
echo "--- --check-unescalate: no loom:operator-blocked sub-label -> the best-effort removal is a harmless no-op ---"
reset_state
issue_fixture 'o/r#9' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$REJECT_DEP_ONLY" "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
run_cdb --issue 9 --repo o/r --check-unescalate --apply
assert_eq "0" "$RC" "exit 0 - un-escalation applies even with no sub-label present (pre-#5679 escalation, 'No backfill')"
assert_contains "$(labels_log 9)" "REMOVE loom:operator-only" "loom:operator-only is removed"

echo
echo "--- --apply write ORDER: loom:operator-only is removed BEFORE the marker comment (#5688 review) ---"
reset_state
issue_fixture 'o/r#10' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$REJECT_DEP_ONLY" "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
run_cdb --issue 10 --repo o/r --check-unescalate --apply
assert_eq "0" "$RC" "exit 0 - un-escalation applies"
RM_LINE="$(grep -n -- '--remove-label loom:operator-only' "$STUB_DIR/calls.log" | head -1 | cut -d: -f1)"
COMMENT_LINE="$(grep -n '^issue comment 10 ' "$STUB_DIR/calls.log" | head -1 | cut -d: -f1)"
MSG="the label removal call precedes the marker comment call"
if [[ -n "$RM_LINE" && -n "$COMMENT_LINE" && "$RM_LINE" -lt "$COMMENT_LINE" ]]; then
    pass "$MSG"
else
    fail "$MSG"
    echo "    remove-label at line '$RM_LINE', comment at line '$COMMENT_LINE'"
fi

echo
echo "--- PARTIAL WRITE: label removal fails -> NO marker comment, and the next re-scan RETRIES (#5688 review) ---"
# The permanence bug this ordering exists to prevent: if the marker comment were
# posted first and the label removal then failed, check_unescalate()'s marker-keyed
# idempotency guard would report `already-unescalated` on every later pass and the
# label would never come off -- exactly the #5664 failure mode, reintroduced
# through the self-healing path's own partial-failure case.
reset_state
issue_fixture 'o/r#11' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$REJECT_DEP_ONLY" "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
export GH_STUB_FAIL_REMOVE_LABEL='loom:operator-only'
run_cdb --issue 11 --repo o/r --check-unescalate --apply
unset GH_STUB_FAIL_REMOVE_LABEL
assert_eq "1" "$RC" "a failed label removal is surfaced as apply-failed"
assert_contains "$OUT" "REASON: apply-failed" "the failed apply is named"
assert_eq "" "$(comments_log 11)" "no un-escalation comment is posted when the label removal failed"
if jq -e '[.labels[].name] | contains(["loom:operator-only"])' "$STUB_DIR/issue-o_r_11.json" >/dev/null; then
    pass "the label is (correctly) still present after the failed removal"
else
    fail "the label is (correctly) still present after the failed removal"
fi
# The very next pass, with the transient failure gone, must RETRY rather than
# short-circuit on a marker that was never written.
run_cdb --issue 11 --repo o/r --check-unescalate --apply
assert_eq "0" "$RC" "the next re-scan retries the un-escalation"
assert_not_contains "$OUT" "already-unescalated" \
    "a partial write never makes a later re-scan believe the work is already done"
assert_contains "$OUT" "UNESCALATED: o/r#11" "the retry completes the un-escalation"
assert_contains "$(labels_log 11)" "REMOVE loom:operator-only" "the label comes off on the retry"
assert_contains "$(comments_log 11)" "champion:proposal-unescalated:" "the marker comment lands on the retry"

echo
echo "--- PARTIAL WRITE: comment fails AFTER a successful removal -> the un-escalation still stands (#5688 review) ---"
reset_state
issue_fixture 'o/r#12' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$REJECT_DEP_ONLY" "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
export GH_STUB_FAIL_COMMENT=1
run_cdb --issue 12 --repo o/r --check-unescalate --apply
unset GH_STUB_FAIL_COMMENT
assert_eq "0" "$RC" "the state change that already landed is not misreported as apply-failed"
assert_contains "$OUT" "UNESCALATED: o/r#12" "the completed un-escalation is reported"
assert_contains "$(labels_log 12)" "REMOVE loom:operator-only" "the label was removed before the comment was attempted"
assert_eq "" "$(comments_log 12)" "the audit comment genuinely did not land"
assert_contains "$(cat "$STUB_DIR/stderr.log")" "could not post the un-escalation comment" \
    "the missing audit trail is warned about on stderr"
# The soft direction: a later re-scan is a clean no-op, not a second write.
run_cdb --issue 12 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "the follow-up re-scan has nothing to do"
assert_contains "$OUT" "REASON: not-operator-only" \
    "with the label already gone the re-scan stops at the label check, never re-comments"

echo
echo "--- REGRESSION GUARD: a merits escalation is never un-escalated ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$ESCALATION_MERITS"
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "a merits escalation stays escalated even once its dependency clears"
assert_contains "$OUT" "REASON: merits-finding" "reason is the merits finding"
assert_eq "" "$(labels_log 5)" "the label is untouched"

echo
echo "--- REGRESSION GUARD (#6112): the #4196 incident shape is never un-escalated on a bare 'Dependencies' heading match ---"
# The exact incident: the escalation's own REASON is merits-finding (a Scope
# Appropriateness finding quoting the issue's "Dependencies / references"
# heading, with an incidental #3997 mention), never a real dependency wait --
# so even once #3997 closes, this must stay escalated, not un-escalate.
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect,loom:operator-only' \
    '<!-- champion:proposal-escalated -->
**Champion: Escalating to Operator — Repeated Rejection Without Revision**

**Recurring findings:**
- Scope appropriateness: the issue'"'"'s own "Dependencies / references" section
  recommends filing Phase 2 as its own issue. Phase 1 is shipped (#3997).

A human needs to decide.

---
*Automated by Champion role*'
issue_fixture 'o/r#3997' CLOSED 'Phase 1 work.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "the #4196 shape stays escalated even though #3997 (the incidental mention) is closed"
assert_contains "$OUT" "REASON: merits-finding" "reason is merits-finding, matching #4196's own REASON, not a dependency wait"
assert_eq "" "$(labels_log 5)" "the label is untouched -- no incorrect un-escalation"

echo
echo "--- REGRESSION GUARD: a dependency-CYCLE escalation is never un-escalated ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$ESCALATION_DEP_ONLY" '<!-- champion:dep-cycle:deadbeefdeadbeef -->
**Dependency cycle detected**'
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "a cycle escalation is correctly permanent"
assert_contains "$OUT" "REASON: cycle-escalation" "reason is cycle-escalation"
assert_eq "" "$(labels_log 5)" "the label is untouched"

echo
echo "--- REGRESSION GUARD: a human-applied loom:operator-only is never un-escalated ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    'Needs a credential rotation before this can proceed. Blocked by #3.'
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "no champion:proposal-escalated record means Champion did not escalate it"
assert_contains "$OUT" "REASON: no-escalation-record" "reason is no-escalation-record"
assert_eq "" "$(labels_log 5)" "the label is untouched"

echo
echo "--- --check-unescalate: an unreadable blocker is not evidence of clearing ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by private/repo#77.' 'loom:architect,loom:operator-only' \
    '<!-- champion:proposal-escalated -->
**Recurring findings:**
- Technical Feasibility: hard dependency on private/repo#77, which is still open.
'
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "an unreadable blocker keeps the escalation in place"
assert_contains "$OUT" "UNREADABLE: private/repo#77" "the unreadable node is surfaced"
assert_contains "$OUT" "REASON: unreadable-blocker" "reason is unreadable-blocker"

echo
echo "--- --check-unescalate: no loom:operator-only label at all ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect' "$ESCALATION_DEP_ONLY"
run_cdb --issue 5 --repo o/r --check-unescalate
assert_eq "1" "$RC" "nothing to un-escalate"
assert_contains "$OUT" "REASON: not-operator-only" "reason is not-operator-only"

echo
echo "--- --check-unescalate without --apply is strictly read-only ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' \
    "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' CLOSED 'Merged.' ''
run_cdb --issue 5 --repo o/r --check-unescalate
assert_eq "0" "$RC" "eligibility is reported"
assert_contains "$OUT" "UNESCALATE" "UNESCALATE marker present"
assert_not_contains "$OUT" "UNESCALATED:" "no apply marker without --apply"
assert_eq "" "$(labels_log 5)" "no label change without --apply"
assert_eq "" "$(comments_log 5)" "no comment without --apply"

# =====================================================================
# Sub-issue granularity: startable-subset carve-out (#5664, "recurred after
# closure")
# =====================================================================

# shellcheck disable=SC2016  # literal '#N' text, not an expansion
STARTABLE_SUBSET_TEXT='## Startable Subset

The comparator and mutation tests need only `warmup/01_netlist.v`, which is
already published upstream -- independent of the blocked RTL deliverable.'

echo
echo "--- --check-defer: open blocker + declared startable subset -> PROMOTE_SUBSET ---"
reset_state
issue_fixture 'o/r#5' OPEN "A proposal. Blocked by #3.

$STARTABLE_SUBSET_TEXT" 'loom:architect' "$REJECT_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'The sim harness bootstrap.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "4" "$RC" "exit 4 - promote-subset applies"
assert_contains "$OUT" "PROMOTE_SUBSET" "PROMOTE_SUBSET marker present"
assert_contains "$OUT" "OPEN_BLOCKERS: o/r#3" "the still-open blocker is named"
assert_contains "$OUT" "comparator and mutation tests" "the declared subset text is printed"
assert_eq "" "$(labels_log 5)" "checking the carve-out never touches a label"
assert_eq "" "$(comments_log 5)" "checking the carve-out never posts a comment"

echo
echo "--- --check-defer: open blocker, NO declared subset -> ordinary DEFER (unchanged) ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect' "$REJECT_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'The sim harness bootstrap.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "0" "$RC" "exit 0 - plain DEFER, not promote-subset"
assert_contains "$OUT" "DEFER" "DEFER marker present"
assert_not_contains "$OUT" "PROMOTE_SUBSET" "no promote-subset marker without a declared subset"

echo
echo "--- --check-defer: a genuine dependency CYCLE still escalates even with a declared subset ---"
reset_state
issue_fixture 'o/r#5' OPEN "A proposal. Blocked by #3.

$STARTABLE_SUBSET_TEXT" 'loom:architect' "$REJECT_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'Blocked by #5.' ''
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "1" "$RC" "exit 1 - a cycle is checked BEFORE the subset carve-out"
assert_contains "$OUT" "NO_DEFER" "NO_DEFER marker present"
assert_contains "$OUT" "REASON: dependency-cycle" "reason is dependency-cycle, not the subset carve-out"

echo
echo "--- --check-unescalate: blocker STILL open, but a subset is declared -> UNESCALATE (SUBSET_CARVEOUT) ---"
reset_state
issue_fixture 'o/r#5' OPEN "A proposal. Blocked by #3.

$STARTABLE_SUBSET_TEXT" 'loom:architect,loom:operator-only,loom:operator-blocked' "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "0" "$RC" "exit 0 - the subset carve-out un-escalates even though the blocker is open"
assert_contains "$OUT" "UNESCALATE" "UNESCALATE marker present"
assert_contains "$OUT" "SUBSET_CARVEOUT: yes" "the carve-out reason is distinguishable from blockers-cleared"
assert_contains "$OUT" "STILL_OPEN_BLOCKERS: o/r#3" "the still-open blocker is named, not falsely reported as cleared"
assert_contains "$OUT" "UNESCALATED: o/r#5" "the apply succeeded"
assert_contains "$(labels_log 5)" "REMOVE loom:operator-only" "loom:operator-only was removed"
assert_contains "$(labels_log 5)" "REMOVE loom:operator-blocked" "the sub-kind label was removed alongside it"
assert_contains "$(comments_log 5)" "startable subset" "the un-escalation comment names the subset reason, not blocker closure"

echo
echo "--- --check-unescalate: blocker still open, NO subset declared -> unchanged NO_UNESCALATE ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal. Blocked by #3.' 'loom:architect,loom:operator-only' "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "exit 1 - unchanged: nothing un-escalates a genuinely still-blocked issue"
assert_contains "$OUT" "REASON: blocker-still-open" "reason is blocker-still-open"
assert_eq "" "$(labels_log 5)" "no label change"

echo
echo "--- --check-unescalate: subset-carveout idempotency (re-applying the label is respected) ---"
reset_state
issue_fixture 'o/r#5' OPEN "A proposal. Blocked by #3.

$STARTABLE_SUBSET_TEXT" 'loom:architect,loom:operator-only' "$ESCALATION_DEP_ONLY"
issue_fixture 'o/r#3' OPEN 'Still open.' ''
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "0" "$RC" "first pass un-escalates via the subset carve-out"
# A human (or another mechanism) re-applies loom:operator-only, with the SAME
# un-escalation marker still in the comment history (mirrors the existing
# already-unescalated fixture shape for the blockers-cleared path).
tmp="$(mktemp)"
jq '.labels += [{"name":"loom:operator-only"}]' "$STUB_DIR/issue-o_r_5.json" > "$tmp" && mv "$tmp" "$STUB_DIR/issue-o_r_5.json"
run_cdb --issue 5 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "re-applied label after a completed subset un-escalation is NOT fought"
assert_contains "$OUT" "REASON: already-unescalated" "reason is already-unescalated"

# =====================================================================
# End-to-end: the #5664 incident, replayed
# =====================================================================

echo
echo "--- END-TO-END: the #5664 sequence, healed by Champion itself ---"
reset_state
# Three proposals (#5, #6, #7) hard-depend on #3, which is open.
for N in 5 6 7; do
    issue_fixture "o/r#$N" OPEN "Proposal $N. Blocked by #3." 'loom:architect' "$REJECT_DEP_ONLY"
done
issue_fixture 'o/r#3' OPEN 'Sim harness bootstrap.' ''

# Pass 1 and pass 2 while #3 is open: Champion is at the N=2 escalation threshold
# for each, and the gate must decline to escalate every time.
DEFER_ALL=1
for N in 5 6 7; do
    run_cdb --issue "$N" --repo o/r --check-defer
    [[ "$RC" -eq 0 && "$OUT" == *"DEFER"* ]] || DEFER_ALL=0
done
MSG="all three dependents defer instead of escalating while #3 is open"
if [[ "$DEFER_ALL" -eq 1 ]]; then pass "$MSG"; else fail "$MSG"; fi

no_side_effects=1
ls "$STUB_DIR"/comments-*.log "$STUB_DIR"/labels-*.log >/dev/null 2>&1 && no_side_effects=0
MSG="deferring produced no comments and no labels at all"
if [[ "$no_side_effects" -eq 1 ]]; then pass "$MSG"; else fail "$MSG"; fi

# Now simulate the historical damage: the three were escalated under the old
# behaviour, and #3 then merges.
for N in 5 6 7; do
    tmp="$(mktemp)"
    jq --arg b "$ESCALATION_DEP_ONLY" \
       '.labels += [{"name":"loom:operator-only"}] | .comments += [{"body":$b}]' \
       "$STUB_DIR/issue-o_r_$N.json" > "$tmp" && mv "$tmp" "$STUB_DIR/issue-o_r_$N.json"
done
issue_fixture 'o/r#3' CLOSED 'Sim harness bootstrap. Merged.' ''

# The NEXT Champion pass, with no human intervention.
HEALED=1
for N in 5 6 7; do
    run_cdb --issue "$N" --repo o/r --check-unescalate --apply
    [[ "$RC" -eq 0 && "$OUT" == *"UNESCALATED: o/r#$N"* ]] || HEALED=0
    grep -q "REMOVE loom:operator-only" "$STUB_DIR/labels-o_r_$N.log" 2>/dev/null || HEALED=0
    jq -e '[.labels[].name] | contains(["loom:operator-only"]) | not' \
        "$STUB_DIR/issue-o_r_$N.json" >/dev/null || HEALED=0
done
MSG="all three un-escalate automatically once #3 closes (the manual workaround, performed by Champion)"
if [[ "$HEALED" -eq 1 ]]; then pass "$MSG"; else fail "$MSG"; fi

# And they are now ordinary candidates again: with #3 closed, the defer gate
# reports REEVALUATE rather than DEFER, so the next evaluation re-runs the criteria.
run_cdb --issue 5 --repo o/r --check-defer
assert_eq "3" "$RC" "after healing, the stale dependency verdict triggers a re-evaluation, not an escalation"

# =====================================================================
# End-to-end regression: the 5-issue "recurred after closure" scenario
# (example-org/canary-repo, 2026-08-08) -- #1 is startable outright, #4 is
# startable outright, #2 hard-depends on #1 with NO stated subset (a genuine
# full park), and #3/#5 each hard-depend on #1 but declare a startable subset
# independent of it. #1 is open throughout the first half of this test, then
# merges.
# =====================================================================

# Findings comments citing #1 (the actual blocker in this scenario) rather than
# #3 (the global fixtures' blocker) -- same shapes as REJECT_DEP_ONLY /
# ESCALATION_DEP_ONLY above, re-targeted.
# shellcheck disable=SC2016  # literal marker text, not an expansion
REJECT_DEP_ONLY_1='<!-- champion:proposal-verdict:body-def456 -->
<!-- champion:unrevised-skips:def456:1 -->
**Champion Review: NEEDS REVISION**

This issue requires additional work before promotion to `loom:issue`:

- Technical Feasibility (no obvious blockers): depends on #1, which is still open.

**Recommended actions:**
- Wait for #1 to land, then resubmit.

---
*Automated by Champion role*'

ESCALATION_DEP_ONLY_1='<!-- champion:proposal-escalated -->
**Champion: Escalating to Operator — Repeated Rejection Without Revision**

This proposal has been evaluated 2+ times with converging feedback (1 posted
rejection(s) plus 1 silent skip(s) of an unchanged proposal), but has not been
revised to address it.

**Recurring findings:**
- Technical Feasibility (no obvious blockers): Hard dependency on #1, which is
  still open.

A human needs to decide whether to revise this proposal, close it, or accept it
as-is.

---
*Automated by Champion role*'

echo
echo "--- END-TO-END REGRESSION: 5-issue dependency-ordered set, blocker later merges ---"
reset_state

issue_fixture 'o/r#1' OPEN 'The warmup fixture bootstrap.' ''

# #2: fully blocked, no subset -- correctly parks (mirrors the "defensible"
# case in the reopening comment).
issue_fixture 'o/r#2' OPEN 'Depends on #1 for everything.' 'loom:architect' "$REJECT_DEP_ONLY_1"

# #3 and #5: each hard-depend on #1, but declare a startable subset.
issue_fixture 'o/r#3' OPEN "Depends on #1 for the RTL deliverable.

## Startable Subset

The comparator and mutation tests need only \`warmup/01_netlist.v\`, independent
of the blocked RTL deliverable." 'loom:architect' "$REJECT_DEP_ONLY_1"

issue_fixture 'o/r#5' OPEN "Depends on #1 for the full replay harness.

## Startable Subset

The VCD replay and directed test need only the warm-up fixture from #1." \
    'loom:architect' "$REJECT_DEP_ONLY_1"

# --- Pass 1: while #1 is open ---

# #2 defers -- no subset, nothing to promote yet.
run_cdb --issue 2 --repo o/r --check-defer
assert_eq "0" "$RC" "#2 (no subset, genuinely fully blocked) defers"
assert_contains "$OUT" "DEFER" "#2: plain DEFER"
assert_not_contains "$OUT" "PROMOTE_SUBSET" "#2: never promote-subset (no carve-out declared)"

# #3 and #5 report PROMOTE_SUBSET immediately -- criterion 2's carve-out means
# these should never have been parked at issue granularity in the first place.
for N in 3 5; do
    run_cdb --issue "$N" --repo o/r --check-defer
    assert_eq "4" "$RC" "#$N (declares a startable subset) reports PROMOTE_SUBSET, not DEFER"
    assert_contains "$OUT" "PROMOTE_SUBSET" "#$N: PROMOTE_SUBSET marker present"
    assert_contains "$OUT" "OPEN_BLOCKERS: o/r#1" "#$N: the still-open blocker (#1) is named"
done

# --- Now simulate the historical damage from the reopening comment: #2 has
# already reached loom:operator-only for the (correct) full-park reason. #3 and
# #5 were ALSO parked at loom:operator-only, wrongly discarding their stated
# split -- this is the exact "recurred after closure" bug. #1 is still open. ---
for N in 2 3 5; do
    tmp="$(mktemp)"
    jq --arg b "$ESCALATION_DEP_ONLY_1" \
       '.labels += [{"name":"loom:operator-only"}] | .comments += [{"body":$b}]' \
       "$STUB_DIR/issue-o_r_$N.json" > "$tmp" && mv "$tmp" "$STUB_DIR/issue-o_r_$N.json"
done

# Pass 0's re-scan, with #1 STILL open:
run_cdb --issue 2 --repo o/r --check-unescalate --apply
assert_eq "1" "$RC" "#2 stays parked -- genuinely blocked, no subset, #1 still open"
assert_contains "$OUT" "REASON: blocker-still-open" "#2: reason is blocker-still-open"

for N in 3 5; do
    run_cdb --issue "$N" --repo o/r --check-unescalate --apply
    assert_eq "0" "$RC" "#$N un-parks via the subset carve-out even though #1 is still open"
    assert_contains "$OUT" "SUBSET_CARVEOUT: yes" "#$N: carve-out reason recorded"
    assert_contains "$OUT" "UNESCALATED: o/r#$N" "#$N: apply succeeded"
    if jq -e '[.labels[].name] | contains(["loom:operator-only"]) | not' \
        "$STUB_DIR/issue-o_r_$N.json" >/dev/null; then
        pass "#$N: loom:operator-only actually removed"
    else
        fail "#$N: loom:operator-only actually removed"
    fi
done

# #2 is still parked at this point -- unaffected by #3/#5 healing.
if jq -e '[.labels[].name] | contains(["loom:operator-only"])' \
    "$STUB_DIR/issue-o_r_2.json" >/dev/null; then
    pass "#2 remains parked (correctly) while #1 is still open"
else
    fail "#2 remains parked (correctly) while #1 is still open"
fi

# --- #1 merges. ---
issue_fixture 'o/r#1' CLOSED 'The warmup fixture bootstrap. Merged.' ''

# The NEXT Champion pass: #2's ordinary (blockers-cleared) un-escalation path
# now applies too, with no human intervention.
run_cdb --issue 2 --repo o/r --check-unescalate --apply
assert_eq "0" "$RC" "#2 un-parks once #1 actually closes (ordinary blockers-cleared healing)"
assert_contains "$OUT" "CLEARED_BLOCKERS: o/r#1" "#2: the cleared blocker is #1"
assert_not_contains "$OUT" "SUBSET_CARVEOUT" "#2's healing is the ordinary path, not the subset carve-out"

echo
MSG="5-issue regression: #1/#4 startable outright, #2 correctly parks and later un-parks on #1 closing, #3/#5 never should have parked at all and heal via the subset carve-out without waiting"
pass "$MSG"

# =====================================================================
# --check-fact-unescalate (#7650) -- the fact-checkable generalization of
# --check-unescalate for a merits-shaped escalation whose recurring findings
# are arbitrary re-verifiable claims about repo state, not dependency
# citations. Mirrors sg13cmos5l-protocol-emulator#6/#8 (a "file still carries
# the old pin" / "file does not exist" finding that a sibling PR resolved
# hours later).
# =====================================================================

# The escalation comment shape from the #7650 motivating incident: two
# fact-checkable findings, neither a dependency citation (findings_are_
# dependency_only would report false -- --check-unescalate never fires here).
# shellcheck disable=SC2016  # literal marker/backtick text, not an expansion
ESCALATION_FACTS='<!-- champion:proposal-escalated -->
**Champion: Escalating to Operator — Repeated Rejection Without Revision**

**Recurring findings:**
- Technical Feasibility: `layout/toolchain.json` still carries the old `klt`
  pin.
- Technical Feasibility: `verification/_repo_utils.py` does not exist
  anywhere in this repo.

A human needs to decide whether to revise this proposal, close it, or accept
it as-is.

---
*Automated by Champion role*'

RESOLUTIONS_ALL='RESOLVED: layout/toolchain.json now carries the pin the proposal names (verified against the head commit)
RESOLVED: verification/_repo_utils.py landed verbatim (verified against the head commit)'

RESOLUTIONS_PARTIAL='RESOLVED: layout/toolchain.json now carries the pin the proposal names (verified against the head commit)
UNRESOLVED: verification/_repo_utils.py still does not exist'

RESOLUTIONS_SHORT='RESOLVED: layout/toolchain.json now carries the pin the proposal names (verified against the head commit)'

echo
echo "--- --check-fact-unescalate: all cited objections resolved -> revise body, drop labels, comment once ---"
reset_state
res_file="$(mktemp)"; printf '%s\n' "$RESOLUTIONS_ALL" > "$res_file"
issue_fixture 'o/r#5' OPEN 'A proposal with a stale toolchain pin.' \
    'loom:architect,loom:operator-only,loom:operator-decision' "$ESCALATION_FACTS"
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit deadbeef --apply
assert_eq "0" "$RC" "exit 0 - fact-unescalation applies"
assert_contains "$OUT" "FACT_UNESCALATE" "FACT_UNESCALATE marker present"
assert_contains "$OUT" "VERIFIED_COMMIT: deadbeef" "the verifying commit is echoed"
assert_contains "$OUT" "RESOLVED_COUNT: 2" "both findings counted as resolved"
assert_contains "$OUT" "UNESCALATED: o/r#5" "--apply reports what it changed"
assert_contains "$(labels_log 5)" "REMOVE loom:operator-only" "loom:operator-only is removed"
assert_contains "$(labels_log 5)" "REMOVE loom:operator-decision" "loom:operator-decision sub-kind is removed alongside the base label"
if jq -e '[.labels[].name] | (contains(["loom:operator-only"]) or contains(["loom:operator-decision"])) | not' \
    "$STUB_DIR/issue-o_r_5.json" >/dev/null; then
    pass "neither label remains on the issue after de-escalation"
else
    fail "neither label remains on the issue after de-escalation"
fi
assert_contains "$(body_edits_log 5)" "## Revision" "a Revision section was appended to the body"
assert_contains "$(body_edits_log 5)" "deadbeef" "the Revision section names the verifying commit"
assert_contains "$(issue_body 5)" "A proposal with a stale toolchain pin." "the ORIGINAL body text is preserved, not replaced"
assert_contains "$(comments_log 5)" "champion:proposal-unescalated-facts:" "one fingerprinted de-escalation comment is posted"
assert_contains "$(comments_log 5)" "deadbeef" "the confirming comment also names the verifying commit"

echo
echo "--- --check-fact-unescalate: only SOME objections resolved -> leave escalation in place, no label/body change ---"
reset_state
res_file="$(mktemp)"; printf '%s\n' "$RESOLUTIONS_PARTIAL" > "$res_file"
issue_fixture 'o/r#5' OPEN 'A proposal with a stale toolchain pin.' \
    'loom:architect,loom:operator-only,loom:operator-decision' "$ESCALATION_FACTS"
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit deadbeef --apply
assert_eq "1" "$RC" "exit 1 - a partial resolution never applies"
assert_contains "$OUT" "REASON: partial-resolution" "reason names the partial resolution"
assert_eq "" "$(labels_log 5)" "no label change on a partial resolution"
assert_eq "" "$(comments_log 5)" "no comment on a partial resolution"
assert_eq "" "$(body_edits_log 5)" "no body edit on a partial resolution"

echo
echo "--- --check-fact-unescalate: a resolutions file that doesn't address every finding never approves (short file) ---"
reset_state
res_file="$(mktemp)"; printf '%s\n' "$RESOLUTIONS_SHORT" > "$res_file"
issue_fixture 'o/r#5' OPEN 'A proposal with a stale toolchain pin.' \
    'loom:architect,loom:operator-only,loom:operator-decision' "$ESCALATION_FACTS"
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit deadbeef --apply
assert_eq "1" "$RC" "exit 1 - a resolutions count mismatch never applies"
assert_contains "$OUT" "REASON: resolutions-mismatch" "reason names the count mismatch"
assert_eq "" "$(labels_log 5)" "no label change on a resolutions mismatch"

echo
echo "--- --check-fact-unescalate: loom:operator-only present but NO champion:proposal-escalated marker -> never touched ---"
reset_state
res_file="$(mktemp)"; printf '%s\n' "$RESOLUTIONS_ALL" > "$res_file"
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect,loom:operator-only,loom:operator-decision'
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit deadbeef --apply
assert_eq "1" "$RC" "exit 1 - a human-applied (or unmarked) operator-only is never touched"
assert_contains "$OUT" "REASON: no-escalation-record" "reason is no-escalation-record"
assert_eq "" "$(labels_log 5)" "no label change without the marker"

echo
echo "--- --check-fact-unescalate: no loom:operator-only label at all -> never touched ---"
reset_state
res_file="$(mktemp)"; printf '%s\n' "$RESOLUTIONS_ALL" > "$res_file"
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect'
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit deadbeef --apply
assert_eq "1" "$RC" "exit 1 - nothing to de-escalate"
assert_contains "$OUT" "REASON: not-operator-only" "reason is not-operator-only"

echo
echo "--- --check-fact-unescalate: issue also carries champion:dep-cycle -> permanent escalation, never touched ---"
reset_state
res_file="$(mktemp)"; printf '%s\n' "$RESOLUTIONS_ALL" > "$res_file"
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect,loom:operator-only,loom:operator-decision' \
    "$ESCALATION_FACTS" '<!-- champion:dep-cycle:abcdef1234567890 -->
A genuine dependency cycle.'
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit deadbeef --apply
assert_eq "1" "$RC" "exit 1 - a dependency cycle cannot self-clear and is never touched by this mechanism either"
assert_contains "$OUT" "REASON: cycle-escalation" "reason is cycle-escalation"
assert_eq "" "$(labels_log 5)" "no label change on a cycle escalation"

echo
echo "--- --check-fact-unescalate: idempotent - a re-applied label for the SAME finding set + commit is not fought ---"
reset_state
res_file="$(mktemp)"; printf '%s\n' "$RESOLUTIONS_ALL" > "$res_file"
issue_fixture 'o/r#5' OPEN 'A proposal with a stale toolchain pin.' \
    'loom:architect,loom:operator-only,loom:operator-decision' "$ESCALATION_FACTS"
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit deadbeef --apply
assert_eq "0" "$RC" "first de-escalation applies"
# Simulate a human deliberately re-adding the label.
tmp="$(mktemp)"
jq '.labels += [{"name":"loom:operator-only"}]' "$STUB_DIR/issue-o_r_5.json" > "$tmp" && mv "$tmp" "$STUB_DIR/issue-o_r_5.json"
rm -f "$STUB_DIR/labels-o_r_5.log" "$STUB_DIR/comments-o_r_5.log" "$STUB_DIR/body-edits-o_r_5.log"
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit deadbeef --apply
assert_eq "1" "$RC" "exit 1 - the same finding set + commit was already de-escalated once"
assert_contains "$OUT" "REASON: already-unescalated" "idempotency marker short-circuits the second attempt"
assert_eq "" "$(labels_log 5)" "no second label removal"
assert_eq "" "$(comments_log 5)" "no second comment"

echo
echo "--- --check-fact-unescalate: re-verifying against a LATER commit is a genuine new attempt, not a no-op ---"
# Reuses the state left behind by the idempotency test above: label re-applied,
# already carrying the deadbeef marker.
res_file="$(mktemp)"; printf '%s\n' "$RESOLUTIONS_ALL" > "$res_file"
run_cdb --issue 5 --repo o/r --check-fact-unescalate --resolutions-file "$res_file" --commit cafef00d --apply
assert_eq "0" "$RC" "a later commit produces a different fingerprint, so it is not short-circuited"
assert_contains "$OUT" "UNESCALATED: o/r#5" "the re-verification against the new commit succeeds"
assert_contains "$(labels_log 5)" "REMOVE loom:operator-only" "the label comes off again"

echo
echo "--- --check-fact-unescalate: --apply without --resolutions-file never applies (fails safe, not a silent no-op) ---"
reset_state
issue_fixture 'o/r#5' OPEN 'A proposal.' 'loom:architect,loom:operator-only,loom:operator-decision' "$ESCALATION_FACTS"
run_cdb --issue 5 --repo o/r --check-fact-unescalate --apply
assert_eq "1" "$RC" "missing --resolutions-file leaves the escalation in place rather than applying blind"
assert_contains "$OUT" "REASON: missing-resolutions-file" "reason is missing-resolutions-file"
assert_eq "" "$(labels_log 5)" "no label change without a resolutions file"

# =====================================================================
# Argument validation
# =====================================================================

echo
echo "--- argument validation ---"
reset_state
run_cdb --repo o/r
assert_eq "2" "$RC" "missing --issue exits 2"
run_cdb --issue notanumber --repo o/r
assert_eq "2" "$RC" "non-numeric --issue exits 2"
run_cdb --issue 5 --repo o/r --apply
assert_eq "2" "$RC" "--apply without --check-unescalate exits 2 (never a silent no-op)"
run_cdb --issue 404 --repo o/r --check-defer
assert_eq "2" "$RC" "unreadable root issue exits 2"

# =====================================================================
# Doc pins: the Champion prose actually calls the gate
# =====================================================================
# #7508 regression guard: the bash-3.2 heredoc-in-command-substitution trap
# =====================================================================
#
# Every `--apply` assertion in this suite failed on macOS while passing on CI's
# bash 5 (#7930), because `_apply_unescalation` built its comment body with
# `"$(cat <<EOF ...)"`. bash 3.2 does not skip heredoc bodies when scanning a
# command substitution for its closing paren, so the `#5664` reference inside
# parentheses was misread as opening a region it never closes: `bad
# substitution: no closing )`, an EMPTY body, and a silently failed
# un-escalation. `_apply_fact_unescalation` had already been converted; these
# two were missed, and nothing scanned this file.
#
# The scan already existed (lib/heredoc-body-safety.sh, #7834) but was wired
# only into the watchdog suites. Wiring it here is the part that makes the fix
# durable rather than a one-time repair.
echo
echo "--- #7508 heredoc-body safety (static scan) ---"
# shellcheck source=lib/heredoc-body-safety.sh
source "$TEST_DIR/lib/heredoc-body-safety.sh"
check_heredoc_scan_selftest
check_heredoc_body_safety "$CDB" _apply_unescalation "_apply_unescalation() (#7930)"
check_heredoc_body_safety "$CDB" _apply_fact_unescalation "_apply_fact_unescalation() (#7508)"
check_heredoc_body_safety "$SCRIPTS_DIR/detect-dependency-cycle.sh" _report_cycle \
    "_report_cycle() (#7930)"

# =====================================================================

echo
echo "--- Doc pins: the Champion prose actually calls the gate ---"

# LITERAL prose fragments, quoted exactly as they appear in the .md files.
# shellcheck disable=SC2016
{
    assert_doc_contains "$CHAMPION_PROMO_MD" "classify-dependency-block.sh" \
        "champion-issue-promo.md invokes the classifier"
    assert_doc_contains "$CHAMPION_PROMO_MD" "--check-defer" \
        "Step 4's escalation path runs the dependency-timing gate"
    assert_doc_contains "$CHAMPION_PROMO_MD" "--check-unescalate --apply" \
        "the self-healing re-scan actually applies the un-escalation"
    assert_doc_contains "$CHAMPION_PROMO_MD" "Pass 0: Self-Healing Un-Escalation Re-Scan" \
        "the re-scan has its own named section that runs before evaluation"
    assert_doc_contains "$CHAMPION_PROMO_MD" 'FORCE_REEVALUATE=no' \
        "an un-escalated proposal bypasses the body-hash marker instead of re-escalating in the same pass"
    assert_doc_contains "$CHAMPION_PROMO_MD" 'LOOM_MAX_UNESCALATION_RESCANS' \
        "the re-scan is bounded per pass"
    assert_doc_contains "$CHAMPION_MD" "Pass 0" \
        "champion.md explains why loom:operator-only is excluded from discovery yet still examined"
    assert_doc_contains "$CHAMPION_REF_MD" "classify-dependency-block.sh --check-defer" \
        "champion-reference.md's decision table documents the defer condition"
    assert_doc_contains "$CURATOR_MD" "classify-dependency-block.sh --issue" \
        "curator.md invokes the classifier for its fact-based de-escalation procedure (#7650)"
    assert_doc_contains "$CURATOR_MD" "--check-fact-unescalate" \
        "curator.md's de-escalation procedure calls the new fact-checkable mode"
    assert_doc_contains "$CURATOR_MD" "--resolutions-file" \
        "curator.md's procedure supplies the per-finding resolutions Curator itself verified"
    assert_doc_contains "$CURATOR_MD" "De-escalating Fact-Based Champion Escalations" \
        "the de-escalation procedure has its own named section"
    assert_doc_contains "$CHAMPION_PROMO_MD" "## Revision" \
        "champion-issue-promo.md documents how a Curator-appended Revision section interacts with BODY_HASH (#7650)"
}

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
