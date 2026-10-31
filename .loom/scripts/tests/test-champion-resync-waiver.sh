#!/usr/bin/env bash
# test-champion-resync-waiver.sh - Regression tests for the Champion resync
# waiver (#13096): a `chore(loom): resync installed Loom to vX` PR passes the
# merge-risk kept-set hold only when every changed kept-set file has the same
# blob SHA as its defaults/ counterpart at tag vX, and holds on anything else.
#
# champion-pr-merge.md is prose an LLM executes, so this extracts the real
# KEPT_SET_PATTERNS, standing_auth_fetch_diff and resync_* functions from it and
# runs them against a stubbed `gh`.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-resync-waiver.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"
CHAMPION_REF_MD="$PROMPT_DIR/champion-reference.md"

# shellcheck source=lib/assert.sh
source "$TEST_DIR/lib/assert.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- extract the shipped code -------------------------------------------------
SRC="$WORK/src.sh"
{
    awk '/^KEPT_SET_PATTERNS=\(/{p=1} p{print} p&&/^\)/{exit}' "$CHAMPION_MD"
    awk '/^RESYNC_GATE_PATTERNS=\(/{p=1} p{print} p&&/^\)/{exit}' "$CHAMPION_MD"
    awk '/^standing_auth_fetch_diff\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD"
    awk '/^resync_upstream_path\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD"
    awk '/^resync_waiver_eval\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD"
} >"$SRC"
for fn in KEPT_SET_PATTERNS= RESYNC_GATE_PATTERNS= standing_auth_fetch_diff resync_upstream_path resync_waiver_eval; do
    if grep -q "$fn" "$SRC"; then pass "extracted $fn from champion-pr-merge.md"; else fail "could not extract $fn"; fi
done

# --- gh stub ------------------------------------------------------------------
# Fixture dir layout (set by each case via $FX):
#   title, head_sha, changed_files, metadata.json, files.json,
#   tags/<tag> (presence = tag exists), upstream/<tag>/<path> (blob sha)
STUB_DIR="$WORK/bin"
mkdir -p "$STUB_DIR"
cat >"$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Emulates only the `gh api` reads the waiver makes.
ep="" jq_filter="" raw=0
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[$i]}" in
        api) ;;
        --jq) jq_filter="${args[$((i + 1))]}"; i=$((i + 1)) ;;
        -H) raw=1; i=$((i + 1)) ;;
        --paginate) ;;
        repos/*) ep="${args[$i]}" ;;
    esac
done
emit() { if [ -n "$jq_filter" ]; then printf '%s' "$1" | jq -r "$jq_filter"; else printf '%s' "$1"; fi; }
case "$ep" in
    repos/\{owner\}/\{repo\}/pulls/*/files) cat "$FX/files.json" ;;
    repos/\{owner\}/\{repo\}/pulls/*)
        [ -f "$FX/pr_fail" ] && exit 1
        emit "$(jq -n --arg t "$(cat "$FX/title")" --arg s "$(cat "$FX/head_sha")" --argjson n "$(cat "$FX/changed_files")" \
            '{title:$t, head:{sha:$s}, changed_files:$n}')" ;;
    repos/\{owner\}/\{repo\}/contents/.loom/install-metadata.json*) cat "$FX/metadata.json" ;;
    repos/rjwalters/loom/git/ref/tags/*)
        tag="${ep##*/}"
        [ -f "$FX/tags/$tag" ] || exit 1
        emit "{\"ref\":\"refs/tags/$tag\"}" ;;
    repos/rjwalters/loom/contents/*)
        path="${ep#repos/rjwalters/loom/contents/}"; ref="${path##*?ref=}"; path="${path%%\?ref=*}"
        [ -f "$FX/upstream/$ref/$path" ] || exit 1
        emit "{\"type\":\"file\",\"sha\":\"$(cat "$FX/upstream/$ref/$path")\"}" ;;
    *) exit 1 ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

# new_fx <version>: a fully valid all-match resync PR; cases then break one thing.
new_fx() {
    FX="$(mktemp -d "$WORK/fx.XXXXXX")"
    mkdir -p "$FX/tags" "$FX/upstream/v$1/defaults/scripts" "$FX/upstream/v$1/defaults/.claude/commands/loom"
    echo "chore(loom): resync installed Loom to v$1" >"$FX/title"
    echo "headsha1" >"$FX/head_sha"
    echo "3" >"$FX/changed_files"
    echo "{\"loom_version\": \"$1\"}" >"$FX/metadata.json"
    touch "$FX/tags/v$1"
    echo "aaa111" >"$FX/upstream/v$1/defaults/scripts/spawn-claude.sh"
    echo "bbb222" >"$FX/upstream/v$1/defaults/scripts/guard-foo.sh"
    cat >"$FX/files.json" <<JSON
[{"filename":".loom/scripts/spawn-claude.sh","sha":"aaa111","status":"modified"},
 {"filename":".loom/scripts/guard-foo.sh","sha":"bbb222","status":"modified"},
 {"filename":".loom/scripts/other-tool.sh","sha":"ccc333","status":"modified"}]
JSON
}

# run_eval: prints "<RESULT>|<TAG>|<REASON>"
run_eval() {
    FX="$FX" PATH="$STUB_DIR:$PATH" bash -c '
        set -u
        PR_NUMBER=7
        source "'"$SRC"'"
        resync_waiver_eval
        printf "%s|%s|%s" "$RESYNC_WAIVER_RESULT" "$RESYNC_WAIVER_TAG" "$RESYNC_WAIVER_REASON"
    '
}
result_of() { printf '%s' "${1%%|*}"; }

V=0.19.1209
echo "--- all kept-set files match the tag ---"
new_fx "$V"
out="$(run_eval)"
assert_eq "ELIGIBLE" "$(result_of "$out")" "all-match resync PR passes the kept-set hold"
assert_contains "$out" "|v$V|" "waiver names the upstream tag"
rec="$(FX="$FX" PATH="$STUB_DIR:$PATH" bash -c 'PR_NUMBER=7; source "'"$SRC"'"; resync_waiver_eval; printf "%s" "$RESYNC_WAIVER_RECORD"')"
assert_contains "$rec" ".loom/scripts/spawn-claude.sh: aaa111" "record lists spawn-claude.sh with its blob SHA"
assert_contains "$rec" ".loom/scripts/guard-foo.sh: bbb222" "record lists the guard hook with its blob SHA"
assert_not_contains "$rec" "other-tool.sh" "non-kept-set files are not in the record"

echo "--- one changed byte holds ---"
new_fx "$V"
sed -i 's/aaa111/aaa999/' "$FX/files.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a kept-set blob that differs from the tag holds"

echo "--- missing / unparsable tag holds ---"
new_fx "$V"; rm "$FX/tags/v$V"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a tag that does not exist upstream holds"
new_fx "$V"; echo '{"loom_version": "main"}' >"$FX/metadata.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "metadata naming a non-version holds"
new_fx "$V"; echo 'not json' >"$FX/metadata.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "unparsable metadata holds"
new_fx "$V"; echo '{"loom_version": "0.19.1100"}' >"$FX/metadata.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "metadata and title naming different versions holds"

echo "--- local-only kept-set file holds ---"
new_fx "$V"; rm "$FX/upstream/v$V/defaults/scripts/guard-foo.sh"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "kept-set file absent at the tag holds"
new_fx "$V"
echo '[{"filename":".claude/settings.json","sha":"ddd444","status":"modified"}]' >"$FX/files.json"; echo 1 >"$FX/changed_files"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "kept-set file with no defaults/ mapping holds"

echo "--- removed / renamed kept-set file holds ---"
new_fx "$V"; sed -i '0,/"modified"/s//"removed"/' "$FX/files.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a removed kept-set file holds"
new_fx "$V"; sed -i '0,/"modified"/s//"renamed"/' "$FX/files.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a renamed kept-set file holds"
new_fx "$V"
jq '.[0] |= (.previous_filename = .filename | .filename = (.filename + ".bak") | .status = "renamed")' "$FX/files.json" >"$FX/files.tmp" && mv "$FX/files.tmp" "$FX/files.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a rename out of a kept-set path (new name non-kept) holds"

echo "--- non-resync PR holds ---"
new_fx "$V"; echo "feat: change the merge gate" >"$FX/title"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a non-resync title holds even with matching blobs"
new_fx "$V"; echo "chore(loom): resync installed Loom to v$V and more" >"$FX/title"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a title with trailing text holds"

echo "--- resync PR with no kept-set file holds (nothing to waive) ---"
new_fx "$V"
echo '[{"filename":".loom/scripts/other-tool.sh","sha":"ccc333","status":"modified"}]' >"$FX/files.json"; echo 1 >"$FX/changed_files"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "no kept-set file means no waiver"

echo "--- fetch failures hold ---"
new_fx "$V"; touch "$FX/pr_fail"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "unreadable PR holds"
new_fx "$V"; echo 9 >"$FX/changed_files"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "incomplete files read (count mismatch) holds"
new_fx "$V"; rm "$FX/files.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "unreadable files list holds"

echo "--- mixed PR: matches plus one mismatch holds ---"
new_fx "$V"; sed -i 's/bbb222/bbb000/' "$FX/files.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "one mismatching kept-set file among matches holds"

echo "--- merge-gate path or non-install file holds (#12945) ---"
new_fx "$V"
jq '.[2].filename = ".loom/scripts/merge-pr.sh"' "$FX/files.json" >"$FX/f.tmp" && mv "$FX/f.tmp" "$FX/files.json"
echo ccc333 >"$FX/upstream/v$V/defaults/scripts/merge-pr.sh"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a byte-identical merge-gate file still holds"
new_fx "$V"
jq '.[2].filename = ".claude/commands/loom/champion-pr-merge.md"' "$FX/files.json" >"$FX/f.tmp" && mv "$FX/f.tmp" "$FX/files.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a champion prompt change holds"
new_fx "$V"
jq '.[2].filename = "src/app.rs"' "$FX/files.json" >"$FX/f.tmp" && mv "$FX/f.tmp" "$FX/files.json"
assert_eq "NOT ELIGIBLE" "$(result_of "$(run_eval)")" "a file outside the install set holds"

echo "--- docs ---"
assert_doc() { assert_contains "$(cat "$1")" "$2" "$3"; }
assert_doc "$CHAMPION_MD" "Resync waiver for the kept-set hold (#13096)" "champion-pr-merge.md documents the waiver"
assert_doc "$CHAMPION_MD" "champion:resync-waiver tag=" "pre-merge comment carries a greppable waiver marker"
assert_doc "$CHAMPION_MD" "KEPT_SET_PATTERNS\` stays hardcoded" "doc states the floor stays hardcoded"
assert_doc "$CHAMPION_REF_MD" "Resync waiver (#13096)" "champion-reference.md documents the waiver"
assert_doc "$CHAMPION_MD" "Step 3b: Close Superseded Resync PRs" "champion-pr-merge.md closes superseded resyncs"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
