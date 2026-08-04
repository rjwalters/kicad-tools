#!/usr/bin/env bash
# test-merge-pr-partial-increment.sh - Unit tests for the partial-increment
# label-reset logic in merge-pr.sh (#3667).
#
# A merged PR that references a family/epic issue with a NON-closing keyword
# (`Part of #N` / `Contributes to #N`) does not auto-close the issue, so its
# `loom:building` label is left orphaned. merge-pr.sh's
# _reset_partial_increment_labels swaps that still-open, still-loom:building
# issue back to `loom:issue` at the merge choke point (mirroring
# orphan_recovery.py's recover_issue() semantics). Closing keywords
# (`Closes`/`Fixes`/`Resolves`) are untouched (GitHub auto-closes those).
#
# Also covers the #4569 closing-keyword conflict guard: GitHub honors a closing
# keyword ANYWHERE in a PR body (not just a line-leading trailer), so prose like
# "...then close #N" in a follow-up checklist silently overrides the deliberate
# `Contributes to #N` trailer and closes the issue on merge. merge-pr.sh detects
# that pre-merge (_check_partial_increment_close_conflict, warning + two
# published globals) and reverts it post-merge (reopen inside
# _reset_one_partial_issue) — while explicitly NOT reverting a close it cannot
# attribute to this PR (a deliberate close in the same window).
#
# #4595 extends that guard to a third source: the PR's COMMIT MESSAGES. merge-pr.sh
# squash-merges without overriding the commit message, so GitHub composes the
# squash message from the PR's commits and honors a `close #N` there even when the
# PR body only says `Part of #N`. The guard unions a paginated REST read of
# `pulls/<N>/commits` into its close-reference set, names the commit message as
# the source in its warning (the operator remedy is amend/reword, not a body
# edit), and degrades to today's warn-only behavior when that fetch fails.
#
# Strategy: the functions under test (_partial_increment_refs,
# _closing_refs_stdin, _body_closing_refs, _closing_ref_snippets,
# _pr_commit_messages, _check_partial_increment_close_conflict,
# _reset_one_partial_issue, _reset_partial_increment_labels) depend only on
# globals (PR_JSON, REPO_NWO, PR_NUMBER, FORGE_TYPE, GH,
# PARTIAL_CONFLICT_ISSUES, PARTIAL_OPEN_BEFORE_MERGE) and the `gh` CLI. We
# extract just those function definitions from merge-pr.sh and source them, stub
# `gh` on PATH to serve canned issue JSON and record mutating calls, then assert
# on the recorded calls. Extracting from source (rather than replicating) keeps
# the test in lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-partial-increment.sh

# SC2034: several globals (PR_JSON, REPO_NWO, PR_NUMBER, FORGE_TYPE) are read
# only by the functions extracted+sourced from merge-pr.sh, which shellcheck
# cannot see — every such assignment looks "unused" to the linter.
# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

# Colors
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
    # Here-string, not a pipe, so grep -q exiting early on a match cannot
    # SIGPIPE printf and (under set -o pipefail) flip the pipeline non-zero
    # despite a match — a size-sensitive flake on large haystacks (#3820).
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

# --- Minimal logging shims the extracted functions call ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }

# --- Real forge-helpers.sh (for the #4856 rate-limit-safe mutation wrappers) ---
# merge-pr.sh's mutating call sites no longer invoke `gh issue edit` / `gh issue
# reopen` / `gh issue comment` directly — they route through
# forge_gh_swap_label_rl_safe / forge_gh_reopen_issue_rl_safe /
# forge_gh_comment_rl_safe, which live in lib/forge-helpers.sh and fall back to
# REST when the GraphQL mutation is rate-limited (#4856). Source the REAL
# helpers (rather than shimming them) so this suite keeps exercising the actual
# `gh` invocation shape the wrappers produce on their happy path, which the stub
# below records verbatim. Sourced BEFORE the shared globals are assigned:
# forge-helpers.sh initializes FORGE_TYPE="" at load time, which would otherwise
# clobber the FORGE_TYPE="github" the tests rely on.
# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

# --- Extract the functions under test from merge-pr.sh and source them ---
# Two spans, each bounded by an anchor line that is NOT part of the span:
#   1. `_strip_fenced_code_blocks() {` .. `_check_partial_increment_close_conflict
#      || true` — the #4569 pre-merge guard, its pure-text ref extractors, and
#      the #5234 code-span/fence-stripping helper the extractors depend on.
#      Stopping at the INVOCATION line keeps the top-level call out of the
#      sourced file (we drive the guard explicitly from the tests).
#   2. `_reset_one_partial_issue() {` .. `# Handle auto-merge mode` — the
#      post-merge reset/reopen pass.
# Both spans contain only function definitions plus comments (harmless when
# sourced).
FUNCS_FILE="$(mktemp)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT
awk '
  /^_strip_fenced_code_blocks\(\) \{/                    { capture=1 }
  /^_check_partial_increment_close_conflict \|\| true/   { capture=0 }
  /^_reset_one_partial_issue\(\) \{/                    { capture=1 }
  /^# Handle auto-merge mode/                           { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

for _fn in _strip_fenced_code_blocks _partial_increment_refs _closing_refs_stdin \
           _body_closing_refs _closing_ref_snippets _partial_increment_ref_snippets \
           _pr_commit_messages _check_partial_increment_close_conflict \
           _reset_one_partial_issue _reset_partial_increment_labels; do
    if ! grep -q "^${_fn}() {" "$FUNCS_FILE"; then
        echo -e "${RED}FATAL${NC}: could not extract $_fn from $MERGE_PR_SRC" >&2
        exit 2
    fi
done
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub gh on PATH ---
STUB_DIR="$(mktemp -d)"
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh for test-merge-pr-partial-increment.sh.
#   gh api repos/OWNER/REPO/issues/N        -> cat $STUB_DIR/issue-N.json (or {})
#   gh api repos/OWNER/REPO/pulls/N/commits -> cat $STUB_DIR/pr-commits.json (or [])
#        (exits 1 when LOOM_TEST_COMMITS_FAIL=1, simulating a fetch failure)
#   gh issue edit N ...                     -> record to $STUB_DIR/gh-calls.log
#   gh issue comment N ...                  -> record to $STUB_DIR/gh-calls.log
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "api" ]]; then
  # Scan argv for the api path rather than assuming it is the last argument:
  # flags such as --paginate / --jq '<program>' may follow it (#4595).
  shift
  path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      # Flags that take a value: drop the flag AND its argument.
      --jq|-q|--field|-f|--raw-field|-F|--header|-H|--method|-X|--input)
        shift
        [[ $# -gt 0 ]] && shift
        continue
        ;;
      -*) shift; continue ;;
      *) path="$1"; break ;;
    esac
  done

  case "$path" in
    */pulls/*/commits)
      if [[ "${LOOM_TEST_COMMITS_FAIL:-}" == "1" ]]; then
        echo "stub gh: simulated commits fetch failure" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/pr-commits.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo '[]'; fi
      exit 0
      ;;
  esac

  num="${path##*/}"
  canned="$STUB_DIR_FROM_ENV/issue-$num.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '{}'; fi
  exit 0
fi

if [[ "$1" == "issue" ]]; then
  # Record the full argv (issue edit/comment) for assertions.
  echo "$*" >> "$LOG"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# --- Shared globals the functions read (consumed indirectly by the sourced
# functions; see the file-level SC2034 disable at the top). ---
REPO_NWO="owner/repo"
PR_NUMBER="999"
FORGE_TYPE="github"
GH="gh"
PARTIAL_OPEN_BEFORE_MERGE=""
PARTIAL_CONFLICT_ISSUES=""

# Shim for the GraphQL-backed close-target helper merge-pr.sh gets from
# forge-helpers.sh. Driven by $FORGE_CLOSE_TARGETS so tests can simulate both
# "GraphQL answered" and "GraphQL quota exhausted -> empty" (the condition the
# #4569 incident actually occurred under).
FORGE_CLOSE_TARGETS=""
forge_pr_close_targets() { printf '%s' "$FORGE_CLOSE_TARGETS"; }

# Canned issue fixtures.
cat > "$STUB_DIR/issue-123.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"},{"name":"loom:epic"}]}
EOF
cat > "$STUB_DIR/issue-456.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-777.json" <<'EOF'
{"state":"closed","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-888.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:issue"}]}
EOF
cat > "$STUB_DIR/issue-321.json" <<'EOF'
{"state":"open","pull_request":{"url":"x"},"labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-4574.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF
# PA6 fixtures: a numbered-list declaration `3. Part of #789` whose marker
# ordinal (3) collides with a genuine `Closes #3` elsewhere in the same body.
cat > "$STUB_DIR/issue-3.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-789.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF

# Canned `pulls/<N>/commits` payload: one JSON commit object per message given.
# Written in the GitHub REST shape the script reads (.[].commit.message).
set_commits() {
  local first=1 m
  {
    printf '['
    for m in "$@"; do
      [[ $first -eq 1 ]] || printf ','
      first=0
      printf '{"sha":"deadbeef","commit":{"message":%s}}' "$(printf '%s' "$m" | jq -Rs .)"
    done
    printf ']'
  } > "$STUB_DIR/pr-commits.json"
}

# Clearing the two #4569 globals here keeps every pre-existing case below running
# against the same "no conflict recorded" state it was written for; resetting the
# commits fixture to empty does the same for the #4595 commit-message signal.
reset_log() {
  : > "$STUB_DIR/gh-calls.log"
  PARTIAL_OPEN_BEFORE_MERGE=""
  PARTIAL_CONFLICT_ISSUES=""
  FORGE_CLOSE_TARGETS=""
  set_commits
  unset LOOM_TEST_COMMITS_FAIL
}
read_log()  { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }

# Run one of the functions under test IN THE CURRENT SHELL (never a command
# substitution — the #4569 guard publishes its results via globals, which a
# subshell would discard) while capturing its stderr for assertions.
run_capturing_stderr() {
    "$@" 2>"$STUB_DIR/stderr.log" || true
}
read_stderr() { cat "$STUB_DIR/stderr.log" 2>/dev/null || true; }

echo "Testing _reset_partial_increment_labels behavior..."

# T1: Part of #123, open + loom:building -> swap to loom:issue + comment.
reset_log
PR_JSON='{"body":"Implements a slice.\n\nPart of #123"}'
_reset_partial_increment_labels
log="$(read_log)"
assert_contains "$log" "issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Part of #123 (open, building) -> swaps loom:building to loom:issue (repo-scoped)"
assert_contains "$log" "issue comment 123 --repo owner/repo" \
  "Part of #123 -> posts an auditable comment (repo-scoped)"

# T2: Closes #123 -> NOT a partial ref -> no mutation (existing behavior preserved).
reset_log
PR_JSON='{"body":"All done.\n\nCloses #123"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Closes #123 -> no label mutation (auto-close path untouched)"

# T2b: Fixes / Resolves also untouched.
reset_log
PR_JSON='{"body":"Fixes #123 and Resolves #456"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Fixes/Resolves -> no label mutation"

# T3: Part of #777 but issue is CLOSED -> no-op.
reset_log
PR_JSON='{"body":"Part of #777"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Part of #777 (closed) -> no-op, no mutation"

# T4: Part of #888 but issue lacks loom:building -> no-op (idempotent).
reset_log
PR_JSON='{"body":"Part of #888"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Part of #888 (not loom:building) -> no-op, idempotent"

# T5: Contributes to #456 (open, building) -> swap.
reset_log
PR_JSON='{"body":"Contributes to #456"}'
_reset_partial_increment_labels
assert_contains "$(read_log)" "issue edit 456 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Contributes to #456 (open, building) -> swaps to loom:issue (repo-scoped)"

# T6: case-insensitive keyword + multiple refs -> both swapped.
reset_log
PR_JSON='{"body":"part of #123\ncontributes TO #456"}'
_reset_partial_increment_labels
log="$(read_log)"
assert_contains "$log" "issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Case-insensitive 'part of #123' matched"
assert_contains "$log" "issue edit 456 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Multiple refs: #456 also matched"

# T7: reference target is actually a PR (has .pull_request) -> skip.
reset_log
PR_JSON='{"body":"Part of #321"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Part of #321 (is a PR, has .pull_request) -> skipped"

# T8: FORGE_TYPE != github -> no-op (v1 is GitHub-only).
reset_log
FORGE_TYPE="gitea"
PR_JSON='{"body":"Part of #123"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "FORGE_TYPE=gitea -> no-op (GitHub-only v1)"
FORGE_TYPE="github"

# T9: empty / null body -> no-op.
reset_log
PR_JSON='{"body":null}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "null PR body -> no-op"

# T10: closing keyword AND a partial ref in the same body -> only the partial
# ref's issue is reset; the Closes issue is left to GitHub auto-close.
reset_log
PR_JSON='{"body":"Closes #888\n\nPart of #123"}'
_reset_partial_increment_labels
log="$(read_log)"
assert_contains "$log" "issue edit 123 --repo owner/repo --remove-label loom:building" \
  "Mixed body: Part of #123 is reset"
assert_not_contains "$log" "issue edit 888" \
  "Mixed body: Closes #888 is NOT touched"

# ---------------------------------------------------------------------------
# #4569: closing-keyword conflict detection + premature-close revert.
# ---------------------------------------------------------------------------

echo ""
echo "Testing _body_closing_refs (GitHub closing-keyword extraction)..."

# The incident phrasing: a closing keyword buried in prose, NOT line-leading.
assert_eq "123" "$(_body_closing_refs '3. Verify the publish, then close #123.')" \
  "Prose 'then close #123' is a closing reference (keyword is not line-leading)"
assert_eq "123" "$(_body_closing_refs 'Closes #123')" \
  "Canonical 'Closes #123' trailer extracted"
assert_eq "$(printf '7\n9')" "$(_body_closing_refs 'Fixes #7 and resolved #9')" \
  "Tense/case variants (Fixes/resolved) both extracted, numerically sorted"
assert_eq "" "$(_body_closing_refs 'Contributes to #123')" \
  "Non-closing 'Contributes to #123' is NOT a closing reference"
assert_eq "" "$(_body_closing_refs 'Discloses #123 and Updates #456')" \
  "Word-boundary guard: 'Discloses'/'Updates' are not closing keywords"
assert_eq "" "$(_body_closing_refs 'close issue #123')" \
  "'close issue #123' is NOT a closing reference (keyword not adjacent to #N)"
assert_eq "1234" "$(_body_closing_refs 'Closes #1234')" \
  "Full number is extracted (no #123 prefix confusion)"

echo ""
echo "Testing _partial_increment_refs prose/code-span guarding (#5234)..."

# PA1: the exact #5234 incident shape — a mid-sentence, backticked, conditional
# mention of `Part of #4574` inside a paragraph addressed to the Judge, plus
# the real, declared `Closes #4574` elsewhere in the body. The backticked
# mention is prose describing a hypothetical ("if you would rather... I will
# switch the reference to"), not a declaration, and must NOT be read as one.
incident_body='Summary of the fix.

Judge: if you would rather this stay open until #4580 lands, say so and I will switch the reference to `Part of #4574`.

More detail about the change.

Closes #4574'
assert_eq "" "$(_partial_increment_refs "$incident_body")" \
  "#5234 incident: backticked, mid-sentence 'Part of #4574' is NOT read as a declaration"

reset_log
PR_JSON="$(jq -n --arg body "$incident_body" '{body: $body}')"
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "#5234 incident: no conflict recorded — the real 'Closes #4574' stands unopposed"
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "#5234 incident: #4574 is not tracked as a partial-increment ref at all (no declaration found)"

# PA2: companion — a genuine, line-leading `Part of #4574` trailer (its own
# line, no backticks) still triggers the existing partial-increment path, and
# a conflicting closing reference elsewhere in the body is still detected. Also
# verifies AC #4: the warning quotes the matched declaration text.
genuine_body='Summary of the fix.

Part of #4574

Closes #4574'
assert_eq "4574" "$(_partial_increment_refs "$genuine_body")" \
  "Companion: genuine line-leading 'Part of #4574' IS read as a declaration"

reset_log
PR_JSON="$(jq -n --arg body "$genuine_body" '{body: $body}')"
run_capturing_stderr _check_partial_increment_close_conflict
genuine_err="$(read_stderr)"
assert_eq "4574" "$PARTIAL_CONFLICT_ISSUES" \
  "Companion: genuine 'Part of #4574' + 'Closes #4574' -> conflict recorded (existing path still fires)"
assert_contains "$genuine_err" '("Part of #4574")' \
  "Companion: warning quotes the matched declaration text with context (AC #4)"
assert_contains "$genuine_err" 'Closes #4574' \
  "Companion: warning also quotes the offending closing-keyword text"

# PA3: `Part of #123` shown inside a FENCED code block (not just inline
# backticks) is also excluded — a documentation example, not a live
# declaration.
fenced_body='Example usage of the convention:

```
Part of #123
```

Closes #123'
assert_eq "" "$(_partial_increment_refs "$fenced_body")" \
  "Fenced code block: 'Part of #123' inside a triple-backtick fence is NOT a declaration"

# PA4: mid-sentence prose (no backticks at all) referencing the pattern is
# still excluded by the line-leading anchor alone.
prose_body='This work is part of #123, a larger initiative that will continue after this PR.'
assert_eq "" "$(_partial_increment_refs "$prose_body")" \
  "Mid-sentence prose 'part of #123' (no backticks) is NOT a declaration (line-leading anchor)"

# PA5: a list-marker-prefixed declaration still counts (AC: "optionally after
# list-marker/whitespace").
list_body='Changes in this increment:

- Part of #123'
assert_eq "123" "$(_partial_increment_refs "$list_body")" \
  "List-marker prefix: '- Part of #123' still counts as a declaration"

# PA6: a NUMBERED list marker carries its own digits, which the second-stage
# extraction must not mistake for an issue number. PA5 only covers a dash
# marker (no digits to leak), so this case is what caught the regression: with
# a naive `grep -oE '[0-9]+'` over the whole matched span, `3. Part of #789`
# yielded BOTH `3` and `789`. Paired with a genuine `Closes #3` elsewhere in
# the body, that spuriously registers #3 as a declared partial increment AND a
# closing reference — the exact false-positive-reopen shape #5234 exists to
# eliminate, reintroduced through the list-marker support itself.
numbered_body='Changes in this increment:

3. Part of #789

Closes #3'
assert_eq "789" "$(_partial_increment_refs "$numbered_body")" \
  "Numbered list marker: '3. Part of #789' yields ONLY 789 — the marker ordinal 3 does not leak"

reset_log
PR_JSON="$(jq -n --arg body "$numbered_body" '{body: $body}')"
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Numbered list marker: no conflict — 'Closes #3' stands, #3 is not a partial-increment ref"
assert_eq "789" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Numbered list marker: only the real declaration target (#789) is tracked as open before the merge"

# PA7: blockquote marker (`>`), the other marker branch the regex claims to
# support and that no test previously exercised.
quoted_body='Context from the epic:

> Part of #456'
assert_eq "456" "$(_partial_increment_refs "$quoted_body")" \
  "Blockquote marker: '> Part of #456' still counts as a declaration"

echo ""
echo "Testing _check_partial_increment_close_conflict (pre-merge guard)..."

# T11: reproduces the censusapi#5 -> censusapi#2 incident shape — a deliberate
# `Contributes to #123` trailer plus an "Operator follow-up" step saying
# "...then close #123", with GraphQL unavailable (quota exhausted).
reset_log
PR_JSON='{"body":"## Summary\n\nBuilder scope only; publish steps stay with the operator.\n\n## Operator follow-up (after merge)\n\n1. npm publish\n2. Verify, then close #123.\n\nContributes to #123\n"}'
run_capturing_stderr _check_partial_increment_close_conflict
guard_err="$(read_stderr)"
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "Incident shape: #123 recorded as a closing-keyword conflict (body regex, no GraphQL)"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Incident shape: #123 recorded as open before the merge"
assert_contains "$guard_err" "Partial-increment conflict (#4569)" \
  "Incident shape: emits the actionable pre-merge warning"
assert_contains "$guard_err" "close #123" \
  "Incident shape: warning quotes the offending phrase"

# T12: a clean partial-increment body -> tracked as open-before-merge, no conflict.
reset_log
PR_JSON='{"body":"Implements a slice.\n\nContributes to #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Clean 'Contributes to #123' body -> no conflict recorded"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Clean body -> #123 still recorded as open before the merge"

# T13: closing keyword aimed at a DIFFERENT issue -> not a conflict.
reset_log
PR_JSON='{"body":"Closes #888\n\nPart of #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Closes #888 + Part of #123 -> no conflict (different issues)"

# T14: partial ref already closed BEFORE the merge -> tracked in neither set,
# so nothing this merge does can be blamed on it.
reset_log
PR_JSON='{"body":"Part of #777"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" "Pre-closed #777 -> no conflict recorded"
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" "Pre-closed #777 -> not recorded as open before merge"

# T15: GraphQL-only closing link (e.g. a Development-sidebar link no body text
# reveals) is also caught, via the forge_pr_close_targets union.
reset_log
FORGE_CLOSE_TARGETS="123"
PR_JSON='{"body":"Contributes to #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "closingIssuesReferences-only link (no body keyword) -> conflict recorded"

# T16: the reference target is a PR -> never tracked, never mutated.
reset_log
PR_JSON='{"body":"Part of #321"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" "Partial ref that is a PR (#321) -> not tracked"

# T17: gitea -> guard is a no-op (matches the GitHub-only reset path).
reset_log
FORGE_TYPE="gitea"
PR_JSON='{"body":"Contributes to #123\n\nthen close #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" "FORGE_TYPE=gitea -> conflict guard no-op"
FORGE_TYPE="github"

# T17b: --dry-run still DETECTS and reports the conflict, but must not phrase it
# as an accomplished merge (same dry-run contract as
# _check_no_open_stacked_children: report the would-be outcome, prefixed).
reset_log
DRY_RUN=true
PR_JSON='{"body":"Verify, then close #123.\n\nContributes to #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
dryrun_err="$(read_stderr)"
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "--dry-run still detects the closing-keyword conflict"
assert_contains "$dryrun_err" "[dry-run] Partial-increment conflict (#4569)" \
  "--dry-run prefixes the conflict warning"
assert_contains "$dryrun_err" "would reopen #123" \
  "--dry-run phrases the reopen as conditional, not as an accomplished action"
assert_not_contains "$dryrun_err" "will reopen #123" \
  "--dry-run does not claim a reopen will happen"
DRY_RUN=false

echo ""
echo "Testing the commit-message closing-keyword signal (#4595)..."

# T21: the #4595 shape — PR body is CLEAN (`Contributes to #123` only), but a
# commit message carries `close #123`. This repo squash-merges without overriding
# the commit message, so GitHub composes the squash message from the commits and
# honors that keyword: it must be recorded as a conflict.
reset_log
set_commits "feat: implement the slice

close #123"
PR_JSON='{"body":"Implements a slice.\n\nContributes to #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
commit_err="$(read_stderr)"
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "Clean body + commit message 'close #123' -> conflict recorded (commit-message signal)"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Commit-message conflict -> #123 also recorded as open before the merge"
assert_contains "$commit_err" "closing keyword in a commit message of this PR" \
  "Commit-message conflict -> warning names the commit message as the source"
assert_contains "$commit_err" "close #123" \
  "Commit-message conflict -> warning quotes the offending commit-message phrase"
assert_contains "$commit_err" "reword the offending commit message" \
  "Commit-message conflict -> remedy is amend/reword, not editing the PR body"
assert_not_contains "$commit_err" "its body ALSO carries" \
  "Commit-message conflict -> does NOT blame the (clean) PR body"

# T22: commits fetch FAILS (e.g. REST error) -> empty signal, conservative
# behavior: still tracked as open-before-merge, but no conflict, so the
# post-merge pass warns instead of reopening. Never blocks the merge.
reset_log
export LOOM_TEST_COMMITS_FAIL=1
PR_JSON='{"body":"Implements a slice.\n\nContributes to #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Commits fetch failure -> degrades to an empty signal (no conflict recorded)"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Commits fetch failure -> #123 still tracked as open before the merge"
unset LOOM_TEST_COMMITS_FAIL

# T22b: fetch failure is survivable — the guard still returns 0 (best-effort,
# never fails or blocks the merge).
reset_log
export LOOM_TEST_COMMITS_FAIL=1
PR_JSON='{"body":"Contributes to #123"}'
guard_rc=0
_check_partial_increment_close_conflict 2>/dev/null || guard_rc=$?
assert_eq "0" "$guard_rc" "Commits fetch failure -> guard still exits 0 (never blocks the merge)"
unset LOOM_TEST_COMMITS_FAIL

# T23: clean body AND clean commits -> no conflict at all.
reset_log
set_commits "feat: implement the slice" "docs: note the follow-up work"
PR_JSON='{"body":"Implements a slice.\n\nContributes to #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Clean body + clean commits -> no conflict recorded"
assert_eq "123" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "Clean body + clean commits -> #123 recorded as open before the merge"

# T24: multi-commit PR where only a MIDDLE commit carries the keyword — all
# commit messages must be aggregated, not just the first or last.
reset_log
set_commits "feat: part one" "fixup: resolves #123 while here" "chore: part three"
PR_JSON='{"body":"Part of #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
mid_err="$(read_stderr)"
assert_eq "123" "$PARTIAL_CONFLICT_ISSUES" \
  "Middle commit's 'resolves #123' -> conflict recorded (all commit messages aggregated)"
assert_contains "$mid_err" "resolves #123" \
  "Middle-commit conflict -> warning quotes the offending phrase"

# T25: 'close issue #123' in a commit message is NOT a closing reference (same
# word-adjacency rule the body regex enforces).
reset_log
set_commits "chore: follow-up will close issue #123 later"
PR_JSON='{"body":"Part of #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Commit 'close issue #123' -> not a closing reference, no conflict"

# T26: a commit closing a DIFFERENT issue than the partial ref -> ignored.
reset_log
set_commits "fix: closes #888"
PR_JSON='{"body":"Part of #123"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "" "$PARTIAL_CONFLICT_ISSUES" \
  "Commit 'closes #888' + Part of #123 -> no conflict (different issues)"

# T27: end-to-end — the commit-message conflict recorded pre-merge drives the
# post-merge reopen. #456 is open when the guard runs and closed when the reset
# pass runs (the fixture is swapped mid-test to simulate the merge's auto-close).
reset_log
set_commits "feat: slice

close #456"
PR_JSON='{"body":"Implements a slice.\n\nContributes to #456"}'
_check_partial_increment_close_conflict 2>/dev/null
assert_eq "456" "$PARTIAL_CONFLICT_ISSUES" \
  "End-to-end: commit-message conflict on #456 recorded pre-merge"
: > "$STUB_DIR/gh-calls.log"
cat > "$STUB_DIR/issue-456.json" <<'EOF'
{"state":"closed","labels":[{"name":"loom:building"}]}
EOF
run_capturing_stderr _reset_partial_increment_labels
e2e_log="$(read_log)"
assert_contains "$e2e_log" "issue reopen 456 --repo owner/repo" \
  "End-to-end: #456 auto-closed by the commit-message keyword is reopened post-merge"
assert_contains "$e2e_log" "issue edit 456 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "End-to-end: reopened #456 falls through to the loom:building -> loom:issue swap"
# Restore the shared fixture for any later case.
cat > "$STUB_DIR/issue-456.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF

# T28: the commit fetch must not happen at all when the PR has no partial-
# increment reference (zero extra API calls on the common path). A failing
# commits endpoint would be reached only if the guard fetched it.
reset_log
export LOOM_TEST_COMMITS_FAIL=1
PR_JSON='{"body":"All done.\n\nCloses #123"}'
run_capturing_stderr _check_partial_increment_close_conflict
nofetch_err="$(read_stderr)"
assert_eq "" "$PARTIAL_OPEN_BEFORE_MERGE" \
  "No partial-increment ref -> guard returns before any commit fetch"
assert_not_contains "$nofetch_err" "simulated commits fetch failure" \
  "No partial-increment ref -> commits endpoint is never called"
unset LOOM_TEST_COMMITS_FAIL

echo ""
echo "Testing the post-merge premature-close revert..."

# T18: conflicted + closed after the merge -> reopen, comment, THEN the normal
# label swap (the issue re-enters the ready queue in the same pass).
reset_log
PARTIAL_OPEN_BEFORE_MERGE="777"
PARTIAL_CONFLICT_ISSUES="777"
PR_JSON='{"body":"Verify, then close #777.\n\nContributes to #777"}'
run_capturing_stderr _reset_partial_increment_labels
revert_err="$(read_stderr)"
log="$(read_log)"
assert_contains "$log" "issue reopen 777 --repo owner/repo" \
  "Conflicted + auto-closed #777 -> reopened (repo-scoped)"
assert_contains "$log" "issue edit 777 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Reopened #777 falls through to the normal loom:building -> loom:issue swap"
assert_contains "$log" "issue comment 777 --repo owner/repo" \
  "Reopened #777 -> posts an auditable comment"
assert_contains "$revert_err" "was auto-closed by PR #999's merge" \
  "Reopen path warns with the attribution"

# T19: EDGE CASE — open before the merge, closed after it, but NO closing
# reference on this PR (e.g. a human closed it deliberately in the same window):
# warn loudly, but do NOT reopen.
reset_log
PARTIAL_OPEN_BEFORE_MERGE="777"
PARTIAL_CONFLICT_ISSUES=""
PR_JSON='{"body":"Contributes to #777"}'
run_capturing_stderr _reset_partial_increment_labels
edge_err="$(read_stderr)"
assert_eq "" "$(read_log)" \
  "Deliberate close in the merge window (no closing reference) -> no reopen, no label mutation"
assert_contains "$edge_err" "NOT reopening automatically" \
  "Deliberate-close case still warns so the coincidence is investigable"

# T20: closed BEFORE the merge and untracked -> byte-for-byte the pre-#4569
# skip (already asserted by T3; re-asserted here with the globals in play).
reset_log
PR_JSON='{"body":"Part of #777"}'
_reset_partial_increment_labels 2>/dev/null
assert_eq "" "$(read_log)" \
  "Untracked closed #777 -> unchanged log-and-skip behavior"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_reset_partial_increment_labels" \
  "merge-pr.sh defines and calls _reset_partial_increment_labels"
assert_contains "$src" "(Part of|Contributes to)" \
  "merge-pr.sh matches the non-closing partial-increment keywords"
# The label swap / reopen / comment mutations route through the #4856
# rate-limit-safe wrappers in lib/forge-helpers.sh, so the source guards assert
# on the WRAPPER call sites (with their label/issue arguments) rather than the
# pre-#4856 raw `gh issue edit --remove-label ... --add-label ...` / `gh issue
# reopen ...` literals, which no longer appear in merge-pr.sh. The intent is
# unchanged: fail loudly if a refactor drops the loom:building -> loom:issue
# swap, the premature-auto-close revert, or the REPO_NWO scoping.
assert_contains "$src" 'forge_gh_swap_label_rl_safe "$REPO_NWO" "$issue_num" "loom:building" "loom:issue"' \
  "merge-pr.sh swaps loom:building -> loom:issue via the rate-limit-safe wrapper (#4856)"
assert_contains "$src" '"loom:building" "loom:issue"' \
  "merge-pr.sh restores loom:issue as the swap's add-label argument"
assert_contains "$src" "--repo \"\$REPO_NWO\"" \
  "merge-pr.sh scopes the partial-increment mutations to REPO_NWO"
assert_contains "$src" 'forge_gh_swap_label_rl_safe "$REPO_NWO"' \
  "merge-pr.sh passes REPO_NWO to the label-swap wrapper (repo-scoped mutation)"
assert_contains "$src" "_check_partial_increment_close_conflict || true" \
  "merge-pr.sh invokes the #4569 conflict guard BEFORE either merge path"
assert_contains "$src" 'forge_gh_reopen_issue_rl_safe "$REPO_NWO" "$issue_num"' \
  "merge-pr.sh reverts a premature auto-close via the rate-limit-safe reopen wrapper (#4856)"
assert_contains "$src" 'forge_gh_comment_rl_safe "$REPO_NWO" "$issue_num" "$comment"' \
  "merge-pr.sh posts the partial-increment / premature-close comments via the rate-limit-safe wrapper (#4856)"
assert_contains "$src" 'close[sd]?|fix(e[sd])?|resolve[sd]?' \
  "merge-pr.sh matches the canonical GitHub closing-keyword set"
assert_contains "$src" 'repos/$REPO_NWO/pulls/$PR_NUMBER/commits' \
  "merge-pr.sh reads the PR's commit messages for closing keywords (#4595)"
assert_contains "$src" '--paginate' \
  "merge-pr.sh paginates the commits fetch (>30-commit PRs are not truncated)"
assert_contains "$src" '_strip_fenced_code_blocks' \
  "merge-pr.sh strips fenced code blocks before matching a partial-increment declaration (#5234)"
assert_contains "$src" "sed -E 's/\`[^\`]*\`//g'" \
  "merge-pr.sh blanks inline code spans before matching a partial-increment declaration (#5234)"
assert_contains "$src" '_partial_increment_ref_snippets' \
  "merge-pr.sh quotes the matched partial-increment declaration text in the pre-merge warning (#5234 AC #4)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
