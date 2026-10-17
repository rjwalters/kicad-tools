#!/usr/bin/env bash
# test-champion-standing-authorization.sh - Regression tests for the Champion
# standing-authorization mechanism (#6850): an optional
# `.loom/config.json` -> `champion.standingAuthorizations` block letting an
# operator pre-authorize an entire merge-risk *class* (e.g. guard-hook edits)
# so criterion #2 (Merge-Risk Judgment) can skip the four-axis judgment for a
# matching PR, given every stated condition mechanically holds.
#
# champion-pr-merge.md's "Standing operator authorization (#6850)" subsection
# is prose an LLM instance reads and executes, not a standalone script (same
# situation as test-docs-only-fast-path.sh and
# test-champion-critical-file-check.sh) -- so this file mirrors the documented
# matcher/validator logic in a local copy and pins the shipped markdown's
# exact commands with `assert_doc_contains`, catching drift between the two.
#
# What this asserts:
#   1. The mirrored glob matcher correctly requires an EXACT SUBSET match
#      (every changed file matches at least one pattern in the class) --
#      one file outside the class's patterns disqualifies the whole PR from
#      that class, mirroring the docs-only fast path's exact-subset rule.
#   2. A malformed class entry (missing id/filePatterns/conditions, or an
#      unrecognized condition string) is treated as invalid -- it never
#      authorizes anything -- without disabling sibling entries in the array.
#   3. No `champion.standingAuthorizations` key, or an empty array, is a
#      structural no-op (mirrors "absent = byte-for-byte unchanged").
#   4. champion-pr-merge.md ships the config schema, the closed condition
#      vocabulary, the fail-safe-on-malformed-entry behavior, and documents
#      this mechanism as orthogonal to `loom:auto-merge-ok`.
#   5. champion-reference.md documents the new mechanism too.
#   6. (#12648) a `criticalFilesOnly` class, run through the shipped
#      standing_auth_eval and criterion #3 block: matched against the
#      critical-file hits only, never eligible for criterion #2.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-standing-authorization.sh

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
CHAMPION_REF_MD="$PROMPT_DIR/champion-reference.md"

# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# Pin a literal snippet as present verbatim in a doc file -- catches drift
# between this test's mirrored logic and the shipped markdown.
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

# =====================================================================
# Mirrored, in isolation, from champion-pr-merge.md's "Standing operator
# authorization (#6850)" subsection: the exact-subset glob matcher and the
# closed-vocabulary condition validator. The live doc also re-verifies each
# condition against `gh`/criterion #1/#6 state -- that part is not testable
# hermetically here and is instead pinned via assert_doc_contains below.
# =====================================================================

# class_matches_files <newline-separated-files> <newline-separated-patterns>
# Echoes "MATCH" if every file matches at least one pattern, else
# "NO MATCH: <first non-matching file>".
class_matches_files() {
    local files="$1" patterns="$2"
    local file pattern matched saw_any=0
    while IFS= read -r file; do
        [ -z "$file" ] && continue
        saw_any=1
        matched=0
        while IFS= read -r pattern; do
            [ -z "$pattern" ] && continue
            # shellcheck disable=SC2053
            if [[ "$file" == $pattern ]]; then
                matched=1
                break
            fi
        done <<<"$patterns"
        if [ "$matched" -ne 1 ]; then
            echo "NO MATCH: $file"
            return 0
        fi
    done <<<"$files"
    if [ "$saw_any" -eq 0 ]; then
        echo "NO MATCH: empty file list"
        return 0
    fi
    echo "MATCH"
}

# class_entry_valid <id> <patterns> <conditions>
# Echoes "VALID" or "INVALID: <reason>" -- mirrors the fail-safe checks that
# run before matching: missing id/patterns/conditions, or any condition
# outside the closed vocabulary, invalidates the entry.
class_entry_valid() {
    local id="$1" patterns="$2" conditions="$3" cond
    if [ -z "$id" ] || [ -z "$patterns" ] || [ -z "$conditions" ]; then
        echo "INVALID: missing id/filePatterns/conditions"
        return 0
    fi
    while IFS= read -r cond; do
        [ -z "$cond" ] && continue
        case "$cond" in
            judgeApproval | greenCi) ;;
            *)
                echo "INVALID: unrecognized condition '$cond'"
                return 0
                ;;
        esac
    done <<<"$conditions"
    echo "VALID"
}

echo "--- class_matches_files: exact-subset glob matching ---"

out="$(class_matches_files ".loom/hooks/guard-destructive.sh" ".loom/hooks/guard-*.sh")"
assert_eq "MATCH" "$out" "a single guard-hook file matches its own class pattern"

out="$(class_matches_files "$(printf '.loom/hooks/guard-a.sh\ndefaults/hooks/guard-b.sh')" "$(printf '.loom/hooks/guard-*.sh\ndefaults/hooks/guard-*.sh')")"
assert_eq "MATCH" "$out" "files matching either of two patterns in the same class both match"

out="$(class_matches_files "$(printf '.loom/hooks/guard-a.sh\nsrc/lib.rs')" ".loom/hooks/guard-*.sh")"
assert_eq "NO MATCH: src/lib.rs" "$out" \
    "a guard-hook file plus an unrelated source file is rejected (mixed diff, exact-subset rule)"

out="$(class_matches_files "merge-pr.sh" ".loom/hooks/guard-*.sh")"
assert_eq "NO MATCH: merge-pr.sh" "$out" \
    "a file outside the class's patterns entirely is rejected"

out="$(class_matches_files "" ".loom/hooks/guard-*.sh")"
assert_eq "NO MATCH: empty file list" "$out" \
    "an empty diff (no files at all) is rejected, not silently treated as a match"

out="$(class_matches_files ".loom/hooks/guard-x.sh.bak" ".loom/hooks/guard-*.sh")"
assert_eq "NO MATCH: .loom/hooks/guard-x.sh.bak" "$out" \
    "bash glob semantics: guard-*.sh does NOT match guard-x.sh.bak (the pattern must match the full string, so a trailing suffix disqualifies it)"

echo
echo "--- class_entry_valid: fail-safe on malformed/unrecognized entries ---"

out="$(class_entry_valid "guard-hooks" ".loom/hooks/guard-*.sh" "judgeApproval")"
assert_eq "VALID" "$out" "a well-formed single-condition entry is valid"

out="$(class_entry_valid "guard-hooks" ".loom/hooks/guard-*.sh" "$(printf 'judgeApproval\ngreenCi')")"
assert_eq "VALID" "$out" "a well-formed multi-condition entry is valid"

out="$(class_entry_valid "" ".loom/hooks/guard-*.sh" "judgeApproval")"
assert_eq "INVALID: missing id/filePatterns/conditions" "$out" \
    "an entry with no id is invalid"

out="$(class_entry_valid "guard-hooks" "" "judgeApproval")"
assert_eq "INVALID: missing id/filePatterns/conditions" "$out" \
    "an entry with no filePatterns is invalid"

out="$(class_entry_valid "guard-hooks" ".loom/hooks/guard-*.sh" "")"
assert_eq "INVALID: missing id/filePatterns/conditions" "$out" \
    "an entry with no conditions is invalid"

out="$(class_entry_valid "guard-hooks" ".loom/hooks/guard-*.sh" "reviewedByTwoHumans")"
assert_eq "INVALID: unrecognized condition 'reviewedByTwoHumans'" "$out" \
    "an entry naming a condition outside the closed vocabulary is invalid (never silently treated as satisfied)"

out="$(class_entry_valid "guard-hooks" ".loom/hooks/guard-*.sh" "$(printf 'judgeApproval\nreviewedByTwoHumans')")"
assert_eq "INVALID: unrecognized condition 'reviewedByTwoHumans'" "$out" \
    "one unrecognized condition invalidates the whole entry, even alongside a recognized one"

echo
echo "--- No config / empty array: structural no-op ---"

# jq's own `// []` default is what the shipped doc relies on for the
# no-config-present case -- pin that jq behaves as expected rather than
# re-implementing config parsing here.
result=$(printf '{}' | jq '(.champion.standingAuthorizations // []) | length > 0')
assert_eq "false" "$result" "a config with no champion key at all is treated as zero classes"

result=$(printf '{"champion":{"standingAuthorizations":[]}}' | jq '(.champion.standingAuthorizations // []) | length > 0')
assert_eq "false" "$result" "an explicit empty standingAuthorizations array is treated as zero classes"

result=$(printf '{"champion":{"standingAuthorizations":[{"id":"x"}]}}' | jq '(.champion.standingAuthorizations // []) | length > 0')
assert_eq "true" "$result" "a non-empty standingAuthorizations array is detected"

echo
echo "--- Doc pins: champion-pr-merge.md ships the standing-authorization mechanism ---"

assert_doc_contains "$CHAMPION_MD" \
    "Standing operator authorization (#6850)" \
    "champion-pr-merge.md documents the #6850 standing-authorization mechanism"

assert_doc_contains "$CHAMPION_MD" \
    "champion.standingAuthorizations" \
    "champion-pr-merge.md names the champion.standingAuthorizations config key"

assert_doc_contains "$CHAMPION_MD" \
    'judgeApproval | greenCi' \
    "champion-pr-merge.md defines the closed condition vocabulary"

assert_doc_contains "$CHAMPION_MD" \
    "STANDING_AUTH_RESULT" \
    "champion-pr-merge.md defines the STANDING_AUTH_RESULT variable used in the Decision rule"

assert_doc_contains "$CHAMPION_MD" \
    "fail safe" \
    "champion-pr-merge.md documents fail-safe behavior for a malformed class entry"

assert_doc_contains "$CHAMPION_MD" \
    "orthogonal" \
    "champion-pr-merge.md documents the standing authorization as orthogonal to loom:auto-merge-ok"

assert_doc_contains "$CHAMPION_MD" \
    "Reuses criterion #6's OWN read" \
    "champion-pr-merge.md's greenCi condition reuses criterion #6's read_ci_checks rather than a second, divergent implementation"

echo
echo "--- #12528: the shipped standing_auth_eval, executed with stubbed forge reads ---"

# Extract the REAL function and KEPT_SET_PATTERNS from the doc and run them,
# so the decision is tested rather than mirrored.
EVAL_SRC="$(mktemp)"
CFG_DIR="$(mktemp -d)"
LOADER_SRC="$(mktemp)"
CRIT_SRC="$(mktemp)"
trap 'rm -f "$EVAL_SRC" "$LOADER_SRC" "$CRIT_SRC" "${BLOCK_SRC:-}"; rm -rf "$CFG_DIR"' EXIT
awk '/^standing_auth_load_config\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD" >"$LOADER_SRC"
awk '/^KEPT_SET_PATTERNS=\(/{p=1} p{print} /^standing_auth_eval any$/{exit}' "$CHAMPION_MD" | sed '$d' >"$EVAL_SRC"
# Criterion #3's own CRITICAL_PATTERNS (#12648): a criticalFilesOnly class reads
# the critical-file hits through it, so run_eval defines it as the block does.
awk '/^CRITICAL_PATTERNS=\(/{p=1} p{print} p&&/^\)/{exit}' "$CHAMPION_MD" >"$CRIT_SRC"

# run_eval <mode> <config-json> <files> <api> <ci: green|red> <labels>
# <files> is criterion #3's own $FILES (critical) / FAST_PATH_FILES (any).
# <api> is the files-API body the stubbed `gh api` returns: "" = the read fails,
# "AUTO" = a well-formed single page built from <files>. API_COUNT overrides the
# PR's changed_files (default: number of files in <api>; FAIL = that read fails).
run_eval() {
    local mode="$1" cfg="$2" files="$3" api="$4" ci="$5" labels="$6"
    if [ "$api" = "AUTO" ]; then
        api="$(printf '%s\n' "$files" | jq -R 'select(length>0) | {filename:., patch:"@@ -1 +1 @@\n+x"}' | jq -s -c .)"
    fi
    printf '%s' "$cfg" >"$CFG_DIR/config.json"
    # shellcheck disable=SC2034  # consumed by the sourced $EVAL_SRC / standing_auth_eval
    (
        CONFIG_FILE="$CFG_DIR/config.json"
        PR_NUMBER=1
        FAST_PATH_FILES="$files"
        LABELS="$labels"
        STUB_API="$api"
        gh() { stub_gh "$@"; }
        read_ci_checks() {
            NO_CHECKS=false
            if [ "$ci" = "green" ]; then CHECKS='[{"name":"t","bucket":"pass"}]'; else CHECKS='[{"name":"t","bucket":"fail"}]'; fi
        }
        # shellcheck disable=SC1090
        source "$EVAL_SRC"
        # shellcheck disable=SC1090
        [ -n "${NO_CRIT_PATTERNS:-}" ] || source "$CRIT_SRC"
        if [ -n "${FETCH_FAILS_AFTER_FILL:-}" ]; then
            # The read fills DIFF_JSON and still reports failure: only the
            # caller's own status check can reject it.
            eval "orig_$(declare -f standing_auth_fetch_diff)"
            standing_auth_fetch_diff() { orig_standing_auth_fetch_diff; return 1; }
        fi
        standing_auth_eval "$mode" "$files"
        if [ -n "${SHOW_SCOPE:-}" ]; then
            echo "$STANDING_AUTH_RESULT:$STANDING_AUTH_ID:$STANDING_AUTH_SCOPE:$(printf '%s' "$STANDING_AUTH_COVERED" | tr '\n' ',')"
        else
            echo "$STANDING_AUTH_RESULT:$STANDING_AUTH_ID"
        fi
    )
}

# stub_gh: the three forge reads the shipped code makes. Reads STUB_API / API_COUNT.
stub_gh() {
    local arg is_files=0 is_count=0 is_jq=0
    for arg in "$@"; do
        case "$arg" in
            */pulls/1/files) is_files=1 ;;
            */pulls/1) is_count=1 ;;
            --jq) is_jq=1 ;;
        esac
    done
    if [ "$is_count" -eq 1 ]; then
        [ "${API_COUNT:-}" = "FAIL" ] && return 1
        if [ -n "${API_COUNT:-}" ]; then echo "$API_COUNT"; else printf '%s' "$STUB_API" | jq -s 'add | length'; fi
        return 0
    fi
    [ "$is_files" -eq 1 ] || return 1
    [ -n "$STUB_API" ] || return 1
    if [ "$is_jq" -eq 1 ]; then printf '%s' "$STUB_API" | jq -r '.[].filename'; else printf '%s\n' "$STUB_API"; fi
    # STUB_FILES_RC=1: the files read prints what it got, then fails (a
    # --paginate run that dies after some pages). The output looks complete.
    [ "${STUB_FILES_RC:-0}" -eq 0 ] || return "$STUB_FILES_RC"
}

CI_CLASS='{"champion":{"standingAuthorizations":[{"id":"ci-steps","filePatterns":[".github/workflows/*.yml"],"conditions":["judgeApproval","greenCi"],"coversCriticalFiles":true,"excludeFilePatterns":[".github/workflows/*publish*"],"forbiddenDiffPatterns":["^\\s*permissions:","secrets\\."]}]}}'
CI_CLASS_NOFLAG='{"champion":{"standingAuthorizations":[{"id":"ci-steps","filePatterns":[".github/workflows/*.yml"],"conditions":["judgeApproval","greenCi"]}]}}'
OKPATCH='[{"filename":".github/workflows/ci.yml","patch":"@@ -1 +1,2 @@\n name: ci\n+# a comment"}]'
PERMPATCH='[{"filename":".github/workflows/ci.yml","patch":"@@ -1 +1,2 @@\n name: ci\n+permissions: write-all"}]'
SECRETADDPATCH='[{"filename":".github/workflows/ci.yml","patch":"@@ -1 +1,2 @@\n name: ci\n+  run: echo ${{ secrets.TOKEN }}"}]'
SECRETDELPATCH='[{"filename":".github/workflows/ci.yml","patch":"@@ -1,2 +1 @@\n name: ci\n-  run: echo ${{ secrets.TOKEN }}"}]'
NOPATCH='[{"filename":".github/workflows/ci.yml"}]'
WF=".github/workflows/ci.yml"

out="$(run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "ELIGIBLE:ci-steps" "$out" "covering class, whole diff in class, approved + green CI: criterion #3 passes, class named"

out="$(run_eval critical "$CI_CLASS" "$(printf '%s\nCargo.toml' "$WF")" AUTO green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "one file outside the class restores FAIL"

out="$(run_eval critical "$CI_CLASS" ".github/workflows/publish.yml" AUTO green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "an excludeFilePatterns file restores FAIL"

out="$(run_eval critical "$CI_CLASS" "$WF" "$PERMPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a forbiddenDiffPatterns line restores FAIL"

# Only the 2nd forbiddenDiffPatterns entry (secrets\.) matches; the 1st
# (^\s*permissions:) matches nothing. A check of only the first pattern passes
# these, so they pin that every pattern is evaluated (#12590).
out="$(run_eval critical "$CI_CLASS" "$WF" "$SECRETADDPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a line matching only the 2nd forbiddenDiffPatterns entry (added) restores FAIL"

out="$(run_eval critical "$CI_CLASS" "$WF" "$SECRETDELPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a line matching only the 2nd forbiddenDiffPatterns entry (removed) restores FAIL"

out="$(run_eval critical "$CI_CLASS" "$WF" "$NOPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a changed file with no patch (unreadable diff) fails closed"

out="$(run_eval critical "$CI_CLASS" "$WF" "" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a failed files-API read fails closed"

# The read-failure path itself, not only the count check behind it: the files
# read prints a body that is complete and consistent (the count and set checks
# would both pass on it) and then exits nonzero. Only honouring the read's own
# status rejects it.
out="$(STUB_FILES_RC=1 run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a files-API read that exits nonzero after printing a complete-looking body fails closed"

out="$(STUB_FILES_RC=0 run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "ELIGIBLE:ci-steps" "$out" "negative control: the same body with a zero exit passes"

# The caller honours the read's status: a fetch that fills DIFF_JSON with a
# complete, consistent body but returns nonzero still disqualifies. (Without
# this, the call-site check is masked by the count check, because the shipped
# fetch leaves DIFF_JSON empty on failure.)
out="$(FETCH_FAILS_AFTER_FILL=1 run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "criterion #3 rejects a failed files read even when DIFF_JSON was filled"

# standing_auth_fetch_diff directly: a failed read returns nonzero, whether or
# not the read printed anything.
fetch_rc() {
    # shellcheck disable=SC2034  # consumed by the sourced standing_auth_fetch_diff
    (
        PR_NUMBER=1
        STUB_API="$1"
        STUB_FILES_RC="$2"
        gh() { stub_gh "$@"; }
        # shellcheck disable=SC1090
        source "$EVAL_SRC"
        standing_auth_fetch_diff
        echo "rc=$?"
    )
}
assert_eq "rc=1" "$(fetch_rc "" 0)" "standing_auth_fetch_diff: a read that fails with no output returns nonzero"
assert_eq "rc=1" "$(fetch_rc "$OKPATCH" 1)" "standing_auth_fetch_diff: a read that fails after printing a page returns nonzero"
assert_eq "rc=0" "$(fetch_rc "$OKPATCH" 0)" "standing_auth_fetch_diff: negative control, a clean read returns zero"

out="$(API_COUNT=FAIL run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a failed changed_files read fails closed"

out="$(run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" red "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "red CI (greenCi does not hold) restores FAIL"

out="$(run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" green "loom:review-requested")"
assert_eq "NOT ELIGIBLE:" "$out" "no Judge approval (judgeApproval does not hold) restores FAIL"

out="$(run_eval critical "$CI_CLASS_NOFLAG" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a class without coversCriticalFiles never passes criterion #3"

out="$(run_eval any "$CI_CLASS_NOFLAG" "$WF" "" green "loom:pr")"
assert_eq "ELIGIBLE:ci-steps" "$out" "criterion #2 behavior of a class without the new keys is unchanged (no forge read)"

out="$(run_eval any "$CI_CLASS_NOFLAG" "" "" green "loom:pr")"
assert_eq "ELIGIBLE:ci-steps" "$out" "criterion #2 'any' mode keeps its pre-#12528 empty-list behavior (no change without the flag)"

BAD_FLAG="${CI_CLASS/\"coversCriticalFiles\":true/\"coversCriticalFiles\":\"true\"}"
out="$(run_eval critical "$BAD_FLAG" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a non-boolean coversCriticalFiles does not opt in"

echo
echo "--- #12528: file-list integrity (one slurped read, count + set equality, renames) ---"

ALL_CFG='{"champion":{"standingAuthorizations":[{"id":"all","filePatterns":["*"],"conditions":["judgeApproval"],"coversCriticalFiles":true}]}}'

out="$(run_eval critical "$ALL_CFG" "docs/note.md" AUTO green "loom:pr")"
assert_eq "ELIGIBLE:all" "$out" "negative control: a class matching '*' passes a harmless non-kept path"

MISMATCH='[{"filename":".github/workflows/ci.yml","patch":"@@\n+a"},{"filename":"defaults/hooks/guard-destructive.sh","patch":"@@\n+a"}]'
out="$(run_eval critical "$CI_CLASS" "$WF" "$MISMATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "criterion #3's file list is a strict subset of the API list (partial/stale read): fails closed"

INCLASS_EXTRA='[{"filename":".github/workflows/ci.yml","patch":"@@\n+a"},{"filename":".github/workflows/other.yml","patch":"@@\n+a"}]'
out="$(run_eval critical "$CI_CLASS" "$WF" "$INCLASS_EXTRA" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "API list names an extra IN-CLASS file criterion #3 did not list (head moved between reads): set equality fails closed"

out="$(API_COUNT=2 run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "API list shorter than the PR's changed_files (truncated read): fails closed"

out="$(run_eval critical "$ALL_CFG" "" "[]" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "an empty changed-file list never passes criterion #3"

RENAME_FROM_KEPT='[{"filename":".github/workflows/x.yml","previous_filename":".github/labels.yml","status":"renamed"}]'
out="$(run_eval critical "$CI_CLASS" ".github/workflows/x.yml" "$RENAME_FROM_KEPT" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a pure rename out of a kept-set path is caught via previous_filename"

RENAME_FROM_HOOK='[{"filename":"docs/notes.md","previous_filename":"defaults/hooks/guard-destructive.sh","status":"renamed"}]'
out="$(run_eval critical "$ALL_CFG" "docs/notes.md" "$RENAME_FROM_HOOK" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a rename of a guard hook to a harmless path is caught even with no forbiddenDiffPatterns"

RENAME_FROM_OUTSIDE='[{"filename":".github/workflows/ci.yml","previous_filename":"src/ci.yml","status":"renamed"}]'
out="$(run_eval critical "$CI_CLASS" "$WF" "$RENAME_FROM_OUTSIDE" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a rename from a path outside the class is not covered by the class"

# Per-page output (older gh): one JSON array per page, no patch on page 1.
P1_NOPATCH='[{"filename":".github/workflows/a.yml"}]'
P2_OK='[{"filename":".github/workflows/b.yml","patch":"@@\n+# c"}]'
TWO_FILES="$(printf '.github/workflows/a.yml\n.github/workflows/b.yml')"
out="$(run_eval critical "$CI_CLASS" "$TWO_FILES" "$(printf '%s\n%s' "$P1_NOPATCH" "$P2_OK")" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "no-patch file on page 1 of a multi-page read is not masked by a clean last page"

P1_OK='[{"filename":".github/workflows/a.yml","patch":"@@\n+# c"}]'
out="$(run_eval critical "$CI_CLASS" "$TWO_FILES" "$(printf '%s\n%s' "$P1_OK" "$P2_OK")" green "loom:pr")"
assert_eq "ELIGIBLE:ci-steps" "$out" "negative control: two clean pages are slurped into one list and pass"

P1_PERM='[{"filename":".github/workflows/a.yml","patch":"@@\n+permissions: write-all"}]'
out="$(run_eval critical "$CI_CLASS" "$TWO_FILES" "$(printf '%s\n%s' "$P1_PERM" "$P2_OK")" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a forbidden line on page 1 of a multi-page read is found"

echo
echo "--- #12528: forbiddenDiffPatterns regex handling (jq test(), invalid = disqualify) ---"

BADRX="${CI_CLASS/\"^\\\\s*permissions:\",\"secrets\\\\.\"/\"(\"}"
out="$(run_eval critical "$BADRX" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "an invalid regex disqualifies the PR"

CONTEXT_ONLY='[{"filename":".github/workflows/ci.yml","patch":"@@ -1 +1 @@\n name: ci"}]'
out="$(run_eval critical "$BADRX" "$WF" "$CONTEXT_ONLY" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "an invalid regex disqualifies even when there are no added/removed lines to test it on"

# Kept set: never satisfiable, even when the class names the path and sets the flag.
# One real, current path per area the Judge listed (merge gate, config, installers,
# token selection, guard wiring, verdict docs).
for kept in defaults/hooks/guard-x.sh defaults/scripts/merge-pr.sh defaults/scripts/install-loom.sh \
    defaults/scripts/resync-installed.sh defaults/scripts/spawn-claude.sh \
    defaults/.claude/commands/loom/champion-pr-merge.md .github/labels.yml db/migrations/001.sql \
    loom-daemon/src/cli/merge_pr_check_runs_rollup.rs loom-daemon/src/comment_trust.rs \
    loom-daemon/src/comment_trust/records.rs loom-daemon/src/claim_reconciliation.rs \
    loom-daemon/src/claim_reconciliation/verdict_invalidation.rs loom-daemon/src/cli/forge_verdict_cmd.rs \
    loom-daemon/src/cli/forge_identity_cmd.rs .loom/config.json defaults/config.json install.sh \
    loom-daemon/src/auto_update/runner.rs loom-daemon/src/auto_update.rs \
    defaults/scripts/lib/github-app-token.sh defaults/scripts/verify-token-precedence.sh \
    .claude/settings.json defaults/.claude/settings.json defaults/hooks/hook-wiring.sh \
    defaults/scripts/lib/installed-file-guard.sh defaults/.claude/commands/loom/judge.md \
    defaults/.claude/commands/loom/champion.md defaults/roles/judge.md \
    defaults/scripts/post-verdict.sh defaults/scripts/verdict-staleness-guard.sh; do
    out="$(run_eval critical "$ALL_CFG" "$kept" AUTO green "loom:pr")"
    assert_eq "NOT ELIGIBLE:" "$out" "kept-set path $kept is never waived, even by a class matching everything"
done

echo
echo "--- #12528: every real path the review named is in the kept set ---"

# Real, current paths in each area the kept set must hold (sticky hold, every
# Champion/Judge prompt copy and workflow, updater, token and credential
# selection, guard wiring, label schema, worktree and branch deletion). Each is
# checked twice: against KEPT_SET_PATTERNS directly, and through the shipped
# evaluator with a class that matches everything.
KEPT_REAL_PATHS=(
    loom-daemon/src/champion_hold.rs loom-daemon/src/champion_hold/classify.rs
    loom-daemon/src/cli/champion_hold_cmd.rs
    defaults/roles/champion.md defaults/roles/champion.json defaults/roles/judge.md
    defaults/roles/judge.json defaults/roles/judge-reference.md defaults/roles/judge-ci-red.md
    defaults/.claude/agents/loom-champion.md defaults/.claude/agents/loom-judge.md
    defaults/.agents/skills/loom-champion/SKILL.md defaults/.agents/skills/loom-judge/SKILL.md
    defaults/.agents/skills/loom-judge-ci-red/SKILL.md defaults/.agents/skills/loom-judge-reference/SKILL.md
    loom-daemon/src/judge_backstop.rs defaults/scripts/judge-fallback-guard.sh
    .github/workflows/loom-champion.yml .github/workflows/loom-judge.yml
    loom-daemon/src/daemon_update/mod.rs loom-daemon/src/daemon_update/provision/txn.rs
    loom-daemon/src/cli/daemon_update.rs defaults/scripts/cli/loom-daemon-update.sh
    loom-daemon/src/cli/install_binary.rs defaults/scripts/land-resync-commit.sh
    loom-daemon/src/forge_identity.rs loom-daemon/src/forge_identity/route.rs
    loom-daemon/src/forge_identity/served.rs loom-daemon/src/forge_identity/sidecar.rs
    loom-daemon/src/cli/tokens.rs loom-daemon/src/cli/tokens/select_cmd.rs
    loom-daemon/src/credential_preflight.rs loom-daemon/src/credential_preflight/pool.rs
    defaults/scripts/probe-tokens.sh
    loom-daemon/src/guard_wiring.rs loom-daemon/src/cli/check_guard_wiring.rs
    loom-daemon/src/mcp_tool_guard.rs defaults/scripts/check-guards-installed.sh
    .githooks/pre-commit .githooks/pre-push
    .github/labels.yml defaults/.github/labels.yml defaults/labels.json
    loom-daemon/src/cli/labels_migrate.rs loom-daemon/src/cli/labels_migrate_holds.rs
    loom-daemon/src/cli/worktree_remove.rs defaults/scripts/cleanup-branches.sh
    loom-daemon/src/claim_reconciliation.rs
)

kept_set_hit() {
    (
        # shellcheck disable=SC1090
        source "$EVAL_SRC"
        local pattern
        for pattern in "${KEPT_SET_PATTERNS[@]}"; do
            # shellcheck disable=SC2053
            if [[ "$1" == $pattern ]]; then echo KEPT; exit 0; fi
        done
        echo "NOT KEPT"
    )
}

# In this source repo, the list must name files that exist, so it cannot rot
# into a list of paths nobody ships. (An installed copy has no loom-daemon/.)
SRC_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
for kept in "${KEPT_REAL_PATHS[@]}"; do
    if [[ -d "$SRC_ROOT/loom-daemon/src" ]]; then
        if [[ -e "$SRC_ROOT/$kept" ]]; then r=EXISTS; else r="missing: $kept"; fi
        assert_eq "EXISTS" "$r" "real path $kept exists in this checkout"
    fi
    assert_eq "KEPT" "$(kept_set_hit "$kept")" "KEPT_SET_PATTERNS matches $kept"
    out="$(run_eval critical "$ALL_CFG" "$kept" AUTO green "loom:pr")"
    assert_eq "NOT ELIGIBLE:" "$out" "kept-set path $kept is never waived by a class matching everything"
done

# The floor errs broad, but harmless paths still pass (negative controls).
for harmless in docs/note.md README.md defaults/scripts/lib/assert.sh loom-daemon/src/main.rs; do
    assert_eq "NOT KEPT" "$(kept_set_hit "$harmless")" "harmless path $harmless is not in the kept set"
done

out="$(run_eval any "$ALL_CFG" "defaults/hooks/guard-x.sh" "" green "loom:pr")"
assert_eq "ELIGIBLE:all" "$out" "kept-set floor is critical-mode only: criterion #2 class behavior is unchanged"

echo
echo "--- #12648: criticalFilesOnly class (critical-file hits only, never criterion #2) ---"

CFO_CLASS='{"champion":{"standingAuthorizations":[{"id":"ci-only","filePatterns":[".github/workflows/*.yml"],"conditions":["judgeApproval","greenCi"],"criticalFilesOnly":true,"excludeFilePatterns":[".github/workflows/*publish*"],"forbiddenDiffPatterns":["^\\s*permissions:","secrets\\."]}]}}'
CFO_ALL='{"champion":{"standingAuthorizations":[{"id":"crit-all","filePatterns":["*"],"conditions":["judgeApproval"],"criticalFilesOnly":true}]}}'
DOC="docs/guide.md"
WF_DOC="$(printf '%s\n%s' "$WF" "$DOC")"
mixed() { # mixed <workflow-patch-line> <doc-patch-line>
    jq -n -c --arg w "$1" --arg d "$2" --arg wf "$WF" --arg doc "$DOC" \
        '[{filename:$wf, patch:("@@ -1 +1,2 @@\n name: ci\n" + $w)}, {filename:$doc, patch:("@@ -1 +1,2 @@\n # Guide\n" + $d)}]'
}
MIX_OK="$(mixed '+# a comment' '+more prose')"

out="$(SHOW_SCOPE=1 run_eval critical "$CFO_CLASS" "$WF_DOC" "$MIX_OK" green "loom:pr")"
assert_eq "ELIGIBLE:ci-only:critical:$WF" "$out" \
    "workflow + docs PR: criterion #3 passes under the criticalFilesOnly class, covering only the workflow"

out="$(run_eval any "$CFO_CLASS" "$WF_DOC" "" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "the same PR is not eligible in criterion #2: it still gets the four-axis judgment"

out="$(run_eval any "$CFO_CLASS" "$WF" "" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a workflow-only PR inside the class's patterns is still not eligible in criterion #2"

out="$(run_eval any "$CFO_ALL" "$DOC" "" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a criticalFilesOnly class matching '*' never satisfies criterion #2"

TWO_CLASSES='{"champion":{"standingAuthorizations":[{"id":"crit-all","filePatterns":["*"],"conditions":["judgeApproval"],"criticalFilesOnly":true},{"id":"docs","filePatterns":["docs/*"],"conditions":["judgeApproval"]}]}}'
out="$(run_eval any "$TWO_CLASSES" "$DOC" "" green "loom:pr")"
assert_eq "ELIGIBLE:docs" "$out" "criterion #2 skips the criticalFilesOnly class and still reaches the ordinary class after it"

out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$(mixed '+permissions: write-all' '+more prose')" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a forbidden line in the workflow patch fails criterion #3"

out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$(mixed '-  run: echo ${{ secrets.TOKEN }}' '+more prose')" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a removed line matching the 2nd forbidden pattern in the workflow patch fails criterion #3"

out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$(mixed '+# a comment' '+permissions: are explained here')" green "loom:pr")"
assert_eq "ELIGIBLE:ci-only" "$out" "a forbidden-looking line in a NON-critical file is not the class's to judge (criterion #2 reads it)"

NOPATCH_WF="$(jq -n -c --arg wf "$WF" --arg doc "$DOC" '[{filename:$wf}, {filename:$doc, patch:"@@\n+x"}]')"
out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$NOPATCH_WF" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a critical-file hit with no patch fails closed"

NOPATCH_DOC="$(jq -n -c --arg wf "$WF" --arg doc "$DOC" '[{filename:$wf, patch:"@@\n+# c"}, {filename:$doc}]')"
out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$NOPATCH_DOC" green "loom:pr")"
assert_eq "ELIGIBLE:ci-only" "$out" "a non-critical file with no patch does not block the class"

out="$(run_eval critical "$CFO_CLASS" "$(printf '.github/workflows/publish.yml\n%s' "$DOC")" AUTO green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "an excluded workflow fails criterion #3"

out="$(run_eval critical "$CFO_CLASS" "$(printf '%s\nCargo.toml\n%s' "$WF" "$DOC")" AUTO green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a second critical-file hit outside the class's patterns fails criterion #3"

out="$(run_eval critical "$CFO_CLASS" "$(printf '.github/workflows/loom-champion.yml\n%s' "$DOC")" AUTO green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a kept-set critical-file hit fails criterion #3 even though it matches the class"

out="$(run_eval critical "$CFO_CLASS" "$(printf '%s\ndefaults/scripts/install-loom.sh' "$WF")" AUTO green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a kept-set path among the NON-critical files still fails criterion #3 (the floor covers every changed file)"

RENAME_KEPT_IN="$(jq -n -c --arg doc "$DOC" '[{filename:".github/workflows/x.yml", previous_filename:".github/labels.yml", status:"renamed", patch:"@@\n+# c"}, {filename:$doc, patch:"@@\n+x"}]')"
out="$(run_eval critical "$CFO_CLASS" "$(printf '.github/workflows/x.yml\n%s' "$DOC")" "$RENAME_KEPT_IN" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a rename of a kept-set file into the class's pattern fails criterion #3"

RENAME_OUT_IN="$(jq -n -c --arg doc "$DOC" --arg wf "$WF" '[{filename:$wf, previous_filename:"src/ci.yml", status:"renamed", patch:"@@\n+# c"}, {filename:$doc, patch:"@@\n+x"}]')"
out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$RENAME_OUT_IN" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a rename from a path outside the class into the pattern fails criterion #3"

out="$(run_eval critical "$CFO_CLASS" "$DOC" AUTO green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "no critical-file hit at all: the class does not apply (criterion #3 passes on its own)"

out="$(NO_CRIT_PATTERNS=1 run_eval critical "$CFO_CLASS" "$WF_DOC" "$MIX_OK" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "CRITICAL_PATTERNS not in scope: fails closed"

out="$(NO_CRIT_PATTERNS=1 run_eval critical "$CFO_ALL" "$WF_DOC" "$MIX_OK" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "no CRITICAL_PATTERNS, class without forbiddenDiffPatterns: fails closed (empty-hits guard)"

out="$(run_eval critical "$CFO_ALL" "$DOC" AUTO green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "no hits, class without forbiddenDiffPatterns: not applied (empty-hits guard)"

# A diff over 128 KiB (Linux MAX_ARG_STRLEN) must still reach the class: the diff goes to jq on stdin, never as an argument.
BIG_MIX="$(head -c 200000 /dev/zero | tr '\0' 'x' | jq -R -s -c --arg wf "$WF" --arg doc "$DOC" \
    '[{filename:$wf, patch:"@@ -1 +1,2 @@\n name: ci\n+# a comment"}, {filename:$doc, patch:("@@ -1 +1,2 @@\n+" + .)}]')"
out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$BIG_MIX" green "loom:pr")"
assert_eq "ELIGIBLE:ci-only" "$out" "a >128 KiB files-API diff does not disable the class (no argv-limit failure)"

out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$MIX_OK" red "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "red CI: greenCi does not hold"

out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$MIX_OK" green "loom:review-requested")"
assert_eq "NOT ELIGIBLE:" "$out" "no Judge approval: judgeApproval does not hold"

out="$(run_eval critical "$CFO_CLASS" "$WF_DOC" "$(jq -c '.[0:1]' <<<"$MIX_OK")" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "the files read names fewer files than criterion #3 listed: fails closed"

CFO_STR="${CFO_CLASS/\"criticalFilesOnly\":true/\"criticalFilesOnly\":\"true\"}"
out="$(run_eval critical "$CFO_STR" "$WF_DOC" "$MIX_OK" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a non-boolean criticalFilesOnly is malformed in criterion #3"
CFO_ALL_STR="${CFO_ALL/\"criticalFilesOnly\":true/\"criticalFilesOnly\":\"true\"}"
out="$(run_eval any "$CFO_ALL_STR" "$DOC" "" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a non-boolean criticalFilesOnly is malformed in criterion #2 too (never widened to an ordinary class)"

# Non-flag classes are unchanged.
out="$(run_eval critical "$CI_CLASS" "$WF_DOC" "$MIX_OK" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "a coversCriticalFiles class without the flag still needs every changed file in the class"
out="$(SHOW_SCOPE=1 run_eval critical "$CI_CLASS" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "ELIGIBLE:ci-steps:all:$WF" "$out" "a coversCriticalFiles class without the flag passes a whole-PR match with scope 'all'"
CFO_FALSE="${CI_CLASS/\"coversCriticalFiles\":true/\"coversCriticalFiles\":true,\"criticalFilesOnly\":false}"
out="$(run_eval critical "$CFO_FALSE" "$WF_DOC" "$MIX_OK" green "loom:pr")"
assert_eq "NOT ELIGIBLE:" "$out" "criticalFilesOnly: false is today's class (whole-PR match required)"
out="$(run_eval critical "$CFO_FALSE" "$WF" "$OKPATCH" green "loom:pr")"
assert_eq "ELIGIBLE:ci-steps" "$out" "criticalFilesOnly: false still passes a whole-PR match"
out="$(run_eval any "$ALL_CFG" "$DOC" "" green "loom:pr")"
assert_eq "ELIGIBLE:all" "$out" "criterion #2 still accepts an ordinary class"

echo
echo "--- #12528: criterion #3's shipped block, executed end to end ---"

# Extract the REAL criterion #3 verification block (FILES fetch ... final PASS
# echo), substitute the PR number, and run it with the evaluator sourced, so
# the call site (not just the function) is tested.
BLOCK_SRC="$(mktemp)"
awk '/^FILES=\$\(gh api "repos\/\{owner\}\/\{repo\}\/pulls\/<number>\/files"/{p=1} p{print} /^echo "PASS: No critical files modified/{exit}' "$CHAMPION_MD" | sed 's/<number>/1/g' >"$BLOCK_SRC"
mkdir -p "$CFG_DIR/repo/.loom"

# run_block <config-json> <files> <api> <labels>
run_block() {
    local cfg="$1" files="$2" api="$3" labels="$4"
    if [ "$api" = "AUTO" ]; then
        api="$(printf '%s\n' "$files" | jq -R 'select(length>0) | {filename:., patch:"@@ -1 +1 @@\n+x"}' | jq -s -c .)"
    fi
    printf '%s' "$cfg" >"$CFG_DIR/repo/.loom/config.json"
    # shellcheck disable=SC2034  # consumed by the sourced $EVAL_SRC / $BLOCK_SRC
    (
        cd "$CFG_DIR/repo" || exit 99
        FAST_PATH_FILES="$files"
        LABELS="$labels"
        STUB_API="$api"
        # A stale ELIGIBLE from criterion #2 must not leak into criterion #3.
        STANDING_AUTH_RESULT="ELIGIBLE"
        gh() { stub_gh "$@"; }
        loom-daemon() { return 1; }
        read_ci_checks() { NO_CHECKS=false; CHECKS='[{"name":"t","bucket":"pass"}]'; }
        # shellcheck disable=SC1090
        source "$LOADER_SRC"
        # shellcheck disable=SC1090
        source "$EVAL_SRC"
        # shellcheck disable=SC1090
        source "$BLOCK_SRC"
    ) 2>&1
}

out="$(run_block "$CI_CLASS" "$WF" "$OKPATCH" "loom:pr")"
case "$out" in *"PASS (standing authorization 'ci-steps', #12528)"*) r=PASS ;; *) r="got: $out" ;; esac
assert_eq "PASS" "$r" "criterion #3 block: a covering class passes and names the class"

out="$(run_block "$CI_CLASS_NOFLAG" "$WF" "$OKPATCH" "loom:pr")"
case "$out" in *"FAIL: Critical file modified: $WF"*) r=FAIL ;; *) r="got: $out" ;; esac
assert_eq "FAIL" "$r" "criterion #3 block: a class without coversCriticalFiles still FAILs a workflow edit"

out="$(run_block "$CI_CLASS" "$WF" "$OKPATCH" "loom:review-requested")"
case "$out" in *"FAIL: Critical file modified: $WF"*) r=FAIL ;; *) r="got: $out" ;; esac
assert_eq "FAIL" "$r" "criterion #3 block: a stale ELIGIBLE from criterion #2 is not inherited"

out="$(run_block "$CFO_CLASS" "$WF_DOC" "$MIX_OK" "loom:pr")"
case "$out" in *"PASS (standing authorization 'ci-only', #12648): criterion #3 satisfied by a criticalFilesOnly class — critical files: $WF"*) r=PASS ;; *) r="got: $out" ;; esac
assert_eq "PASS" "$r" "criterion #3 block: a criticalFilesOnly class passes a workflow + docs PR and names the class and the critical files"

out="$(run_block "$CFO_CLASS" "$WF_DOC" "$(mixed '+permissions: write-all' '+x')" "loom:pr")"
case "$out" in *"FAIL: Critical file modified: $WF"*) r=FAIL ;; *) r="got: $out" ;; esac
assert_eq "FAIL" "$r" "criterion #3 block: a forbidden line in the workflow restores the FAIL"

out="$(run_block "$CFO_CLASS" "$(printf '.github/workflows/loom-judge.yml\n%s' "$DOC")" AUTO "loom:pr")"
case "$out" in *"FAIL: Critical file modified: .github/workflows/loom-judge.yml"*) r=FAIL ;; *) r="got: $out" ;; esac
assert_eq "FAIL" "$r" "criterion #3 block: a kept-set workflow restores the FAIL"

echo
echo "--- Doc pins: #12528 ---"
assert_doc_contains "$CHAMPION_MD" '`coversCriticalFiles` (#12528' "champion-pr-merge.md documents coversCriticalFiles"
assert_doc_contains "$CHAMPION_MD" '`excludeFilePatterns` (#12528' "champion-pr-merge.md documents excludeFilePatterns"
assert_doc_contains "$CHAMPION_MD" '`forbiddenDiffPatterns` (#12528' "champion-pr-merge.md documents forbiddenDiffPatterns"
assert_doc_contains "$CHAMPION_MD" "standing_auth_eval critical" "criterion #3 runs the class check before it emits FAIL"
assert_doc_contains "$CHAMPION_MD" "Revertability counts only what happens on merge or roll (#12528)" "revertability-axis sentence is present"
assert_doc_contains "$CHAMPION_MD" "No standing authorization satisfies the kept set (#12528)" "kept set restated as never satisfiable"
assert_doc_contains "$PROMPT_DIR/champion-critical-file-hold.md" "A covering standing authorization means no hold (#12528)" "champion-critical-file-hold.md documents the covering class"
assert_doc_contains "$CHAMPION_MD" '`criticalFilesOnly` (#12648' "champion-pr-merge.md documents criticalFilesOnly"
assert_doc_contains "$PROMPT_DIR/champion-critical-file-hold.md" '"criticalFilesOnly": true' "champion-critical-file-hold.md documents the criticalFilesOnly class"
assert_doc_contains "$CHAMPION_REF_MD" "Critical files only (#12648)" "champion-reference.md documents the criticalFilesOnly class"

echo
echo "--- Doc pins: champion-reference.md documents the new mechanism ---"

assert_doc_contains "$CHAMPION_REF_MD" \
    "Standing Operator Authorization for a Merge-Risk Class (#6850)" \
    "champion-reference.md has an Edge Case for the #6850 mechanism"

assert_doc_contains "$CHAMPION_REF_MD" \
    "champion.standingAuthorizations" \
    "champion-reference.md names the champion.standingAuthorizations config key"

assert_doc_contains "$CHAMPION_REF_MD" \
    "#6850" \
    "champion-reference.md's decision matrix references #6850"

echo
echo "--- Resolved effective config (#12591) ---"
assert_doc_contains "$CHAMPION_MD" "loom-daemon config get champion.standingAuthorizations --json" "champion-pr-merge.md reads classes through the config verb"

LOADER="$(cat "$LOADER_SRC")"
STUB_DIR="$(mktemp -d)"
COMMITTED_DIR="$(mktemp -d)"
mkdir -p "$COMMITTED_DIR/.loom"
echo '{"champion":{"standingAuthorizations":[{"id":"committed"}]}}' >"$COMMITTED_DIR/.loom/config.json"

# $1 = stub stdout, $2 = stub exit code; prints the first class id the loader exposes
load_ids() {
    printf '#!/usr/bin/env bash\nprintf "%%s" %q\nexit %s\n' "$1" "$2" >"$STUB_DIR/loom-daemon"
    chmod +x "$STUB_DIR/loom-daemon"
    (cd "$COMMITTED_DIR" && PATH="$STUB_DIR:$PATH" bash -c "$LOADER"'
standing_auth_load_config
jq -c "[.champion.standingAuthorizations[]?.id]" "$CONFIG_FILE"')
}

assert_eq '["fleet"]' "$(load_ids '{"found":true,"value":[{"id":"fleet"}],"tier":"fleet-repo"}' 0)" "fleet-tier-only class is honoured"
assert_eq '["committed"]' "$(load_ids '' 2)" "resolver failure falls back to the committed file"
assert_eq '["committed"]' "$(load_ids 'not json' 0)" "unparseable verb output falls back to the committed file"
assert_eq '[]' "$(load_ids '{"found":true,"value":"oops","tier":"fleet-repo"}' 0)" "malformed (non-array) value fails closed"
rm -rf "$STUB_DIR" "$COMMITTED_DIR"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
