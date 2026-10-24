#!/usr/bin/env bash
# test-champion-ci-workflow-blast.sh - Regression tests for the merge-risk criterion's
# vetted CI-workflow check (#12677): `ci_workflow_blast_eval` in
# champion-pr-merge.md decides whether a workflow change stays red on the
# blast-radius axis for being a workflow.
#
# The function is extracted from the shipped markdown and run against a
# stubbed `gh api` / `read_ci_checks`, so the decision is tested rather than
# mirrored (same approach as test-champion-standing-authorization.sh).
#
# Usage:
#   ./.loom/scripts/tests/test-champion-ci-workflow-blast.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Installed copy (.loom/scripts) is two levels below .claude; the source repo
# (defaults/scripts) is one (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"

# shellcheck source=lib/assert.sh
source "$TEST_DIR/lib/assert.sh"

SRC="$(mktemp)"
trap 'rm -f "$SRC"' EXIT
{
    awk '/^KEPT_SET_PATTERNS=\(/{p=1} p{print} p&&/^\)/{exit}' "$CHAMPION_MD"
    awk '/^PRIVILEGED_CONTENT_PATTERNS=\(/{p=1} p{print} p&&/^\)/{exit}' "$CHAMPION_MD"
    awk '/^standing_auth_fetch_diff\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD"
    awk '/^standing_auth_content_clear\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD"
    awk '/^ci_workflow_blast_eval\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD"
} >"$SRC"

echo "Extraction"
assert_contains "$(cat "$SRC")" "KEPT_SET_PATTERNS=(" "kept set extracted"
assert_contains "$(cat "$SRC")" "PRIVILEGED_CONTENT_PATTERNS=(" "shared capability list extracted"
assert_contains "$(cat "$SRC")" "standing_auth_content_clear() {" "head-contents check extracted"
assert_contains "$(cat "$SRC")" "ci_workflow_blast_eval() {" "ci_workflow_blast_eval extracted"

# The fixtures and cases shared with test-champion-standing-authorization.sh (#12988).
FIXTURE_DIR="$TEST_DIR/fixtures/champion-workflows"

# entry <filename> <patch> [previous_filename] -> one files-API object
entry() {
    if [ -n "${3:-}" ]; then
        jq -n -c --arg f "$1" --arg p "$2" --arg o "$3" '{filename:$f, patch:$p, previous_filename:$o}'
    else
        jq -n -c --arg f "$1" --arg p "$2" '{filename:$f, patch:$p}'
    fi
}

# run <api-json-or-FAIL> <ci: green|red|pending|none|skipped> <labels>
# The stubbed `gh api` answers the files read with <api> and the changed_files
# read with the length of <api> (COUNT overrides it; FAIL fails that read).
run() {
    local api="$1" ci="$2" labels="$3"
    # shellcheck disable=SC2034,SC2329  # read by the sourced function
    (
        PR_NUMBER=1
        LABELS="$labels"
        DIFF_JSON=""
        gh() {
            [ "$api" = "FAIL" ] && return 1
            case " $* " in
                *" .head.sha "*) echo abc123 ;;
                *"/contents/"*)
                    # Head contents: CONTENT names a fixture (default: unprivileged); CONTENT=FAIL = unreadable.
                    [ "${CONTENT:-}" = FAIL ] && return 1
                    cat "$FIXTURE_DIR/${CONTENT:-ci-github-token-only.yml}"
                    ;;
                *" --jq "*)
                    [ "${COUNT:-}" = FAIL ] && return 1
                    printf '%s\n' "${COUNT:-$(printf '%s' "$api" | jq 'length')}"
                    ;;
                *) printf '%s' "$api" ;;
            esac
        }
        read_ci_checks() {
            NO_CHECKS=false
            case "$ci" in
                green) CHECKS='[{"name":"t","bucket":"pass"}]' ;;
                red) CHECKS='[{"name":"t","bucket":"fail"}]' ;;
                pending) CHECKS='[{"name":"t","bucket":"pending"}]' ;;
                none) CHECKS=""; NO_CHECKS=true ;;
                skipped) CHECKS='[{"name":"t","bucket":"skipping"}]' ;;
                mixed) CHECKS='[{"name":"t","bucket":"pass"},{"name":"u","bucket":"skipping"}]' ;;
            esac
        }
        # shellcheck disable=SC1090
        source "$SRC"
        ci_workflow_blast_eval
        echo "$WF_BLAST"
    )
}

P=$(printf '@@ -1,2 +1,2 @@\n-  timeout-minutes: 30\n+  timeout-minutes: 45')
CI_WF=$(entry .github/workflows/ci.yml "$P")
DOC=$(entry docs/x.md "$(printf '@@ -1 +1 @@\n+text')")

echo "Vetted workflow change"
assert_eq green "$(run "[$CI_WF,$DOC]" green "loom:pr")" "plain ci.yml edit, approved, CI green -> green"
assert_eq none "$(run "[$DOC]" green "loom:pr")" "no workflow file -> none"

echo "Conditions"
assert_eq red "$(run "[$CI_WF]" red "loom:pr")" "CI failing -> red"
assert_eq red "$(run "[$CI_WF]" pending "loom:pr")" "CI pending -> red"
assert_eq red "$(run "[$CI_WF]" none "loom:pr")" "no checks -> red"
assert_eq red "$(run "[$CI_WF]" green "loom:review-requested")" "no loom:pr -> red"
assert_eq red "$(run FAIL green "loom:pr")" "files API unreadable -> red"
assert_eq red "$(run "[$CI_WF]" skipped "loom:pr")" "every check skipped -> red"
assert_eq green "$(run "[$CI_WF]" mixed "loom:pr")" "one pass plus skipped checks -> green"
assert_eq red "$(COUNT=2 run "[$CI_WF]" green "loom:pr")" "files list shorter than changed_files -> red"
assert_eq red "$(COUNT=FAIL run "[$CI_WF]" green "loom:pr")" "changed_files unreadable -> red"
assert_eq red "$(run '{"message":"x"}' green "loom:pr")" "non-array files body -> red"

echo "Capability, not name (#12988)"
# A release/deploy/publish/merge name alone no longer holds a workflow.
for name in release.yml publish-pypi.yml deploy.yaml merge-group-check.yml Release.yml sub/release.yml; do
    assert_eq green "$(run "[$(entry ".github/workflows/$name" "$P")]" green "loom:pr")" "$name, unprivileged contents -> green"
done
for name in release.yml ci.yml deploy.yaml; do
    assert_eq red "$(CONTENT=ci-with-cloudflare-secret.yml run "[$(entry ".github/workflows/$name" "$P")]" green "loom:pr")" "$name with a secrets.CLOUDFLARE_API_TOKEN deploy step -> red"
done
assert_eq red "$(CONTENT=FAIL run "[$CI_WF]" green "loom:pr")" "head contents unreadable -> red"
for name in champion.yml Loom-Champion.yml JUDGE.yml guard-check.yml; do
    assert_eq red "$(run "[$(entry ".github/workflows/$name" "$P")]" green "loom:pr")" "kept-set workflow $name -> red"
done
assert_eq green "$(run "[$(entry .github/workflows/ci.yml "$P" .github/workflows/release.yml)]" green "loom:pr")" "renamed out of release.yml, unprivileged new contents -> green"
REMOVED=$(jq -n -c --arg p "$(printf '@@ -1,2 +0,0 @@\n-name: old\n-on: push')" '{filename:".github/workflows/old-release.yml", status:"removed", patch:$p}')
assert_eq green "$(run "[$REMOVED]" green "loom:pr")" "removing an unprivileged workflow needs no head contents -> green"

echo "Shared fixtures with #12958 (cases.tsv)"
# Every row of the standing-authorization fixture set must agree: covered -> green, held -> red.
while IFS=$'\t' read -r id pr_path fixture line expected note; do
    case "$id" in "" | \#*) continue ;; esac
    patch=$(printf '@@ -1 +1,2 @@\n name: x\n%s' "$line")
    case "$expected" in covered) want=green ;; *) want=red ;; esac
    assert_eq "$want" "$(CONTENT="$fixture" run "[$(entry "$pr_path" "$patch")]" green "loom:pr")" "fixture $id ($pr_path, $fixture): $expected -- $note"
done <"$FIXTURE_DIR/cases.tsv"

echo "Forbidden lines"
# shellcheck disable=SC2016  # literal workflow text
for line in '  permissions:' '      id-token: write' '  token: ${{ secrets.PAT }}' '  pull_request_target:' '  workflow_run:' \
    '      secrets: inherit' '  token: ${{ secrets["PAT"] }}' '  env: ${{ toJSON(secrets) }}' '  token: ${{ SECRETS.PAT }}' \
    '  "permissions": {contents: write}' '  permissions : write-all' '      - run: gh pr merge --admin --merge "$PR"' \
    '      - run: npm publish' '      - run: gh release create v1' '      - run: wrangler deploy' \
    '        env: {GH_TOKEN: ${{ github.token }}}' '        env: {GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}}'; do
    patch=$(printf '@@ -1 +1,2 @@\n x\n+%s' "$line")
    assert_eq red "$(run "[$(entry .github/workflows/ci.yml "$patch")]" green "loom:pr")" "added '$line' -> red"
done
patch=$(printf '@@ -1,2 +1 @@\n x\n-  permissions:')
assert_eq red "$(run "[$(entry .github/workflows/ci.yml "$patch")]" green "loom:pr")" "removed 'permissions:' -> red"
patch=$(printf '@@ -1,2 +1,2 @@\n permissions:\n-  x: 1\n+  x: 2')
assert_eq green "$(run "[$(entry .github/workflows/ci.yml "$patch")]" green "loom:pr")" "'permissions:' only in context -> green"
echo "YAML comments (#12934)"
# shellcheck disable=SC2016  # literal workflow text
for line in '# release the lock' '  # deploy notes' '# publish later' '    # merge queue' '  timeout-minutes: 45 # was merge-gated'; do
    patch=$(printf '@@ -1 +1,2 @@\n x\n+%s' "$line")
    assert_eq green "$(run "[$(entry .github/workflows/ci.yml "$patch")]" green "loom:pr")" "added comment '$line' -> green"
done
patch=$(printf '@@ -1,2 +1 @@\n x\n-  # release note')
assert_eq green "$(run "[$(entry .github/workflows/ci.yml "$patch")]" green "loom:pr")" "removed comment line -> green"
# shellcheck disable=SC2016  # literal workflow text
for line in '      - run: npm publish # ok' '      - run: echo "#release"' "      - run: echo '#deploy'" '      - run: foo#release' \
    '# permissions: write-all' '  # secrets.PAT' '  x: 1 # id-token'; do
    patch=$(printf '@@ -1 +1,2 @@\n x\n+%s' "$line")
    assert_eq red "$(run "[$(entry .github/workflows/ci.yml "$patch")]" green "loom:pr")" "added '$line' -> red"
done
NOPATCH='{"filename":".github/workflows/ci.yml"}'
assert_eq red "$(run "[$NOPATCH]" green "loom:pr")" "workflow file without a patch -> red"
assert_eq red "$(run '[{"filename":".github/workflows/ci.yml","patch":""}]' green "loom:pr")" "empty patch string -> red"
assert_eq green "$(run "[$CI_WF,{\"filename\":\"img.png\"}]" green "loom:pr")" "non-workflow file without a patch does not count"

echo "Doc wiring"
DOCTXT=$(cat "$CHAMPION_MD")
assert_contains "$DOCTXT" "classification is by capability, never by file name" "vetted-change prose states the capability rule"
# shellcheck disable=SC2016  # literal backticks
assert_contains "$DOCTXT" 'CI workflows (unless `WF_BLAST=green`, #12677)' "blast-radius row names the exception"
# shellcheck disable=SC2016  # literal backticks
assert_contains "$DOCTXT" 'A CI-workflow change with `WF_BLAST=green` (#12677) is not red for being a workflow' "kept set: the exception for WF_BLAST=green is named"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
