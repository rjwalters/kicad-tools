#!/usr/bin/env bash
# test-merge-pr-head-mismatch.sh - Unit tests for the merge head-SHA
# optimistic-concurrency precondition (#5579).
#
# Root cause: neither forge_merge_pr() nor forge_auto_merge()
# (lib/forge-helpers.sh) passed the forge's optional head-SHA precondition
# (GitHub REST's `sha` field / GraphQL's `expectedHeadOid` input / Gitea's
# `head_commit_id` field) to the merge API, so Champion (or merge-pr.sh
# directly) could squash-merge a PR whose branch received new commits after
# the approving review — silently stranding those commits (squash-merge makes
# this invisible to an ancestry check afterward).
#
# This suite exercises three layers:
#   1. forge_merge_pr / forge_auto_merge (lib/forge-helpers.sh): the optional
#      3rd EXPECTED_HEAD_SHA argument is threaded into the right API call
#      shape for both GitHub (REST `sha` / GraphQL `expectedHeadOid`) and
#      Gitea (`head_commit_id`), and omitted entirely when not supplied
#      (backward compatibility for any other caller).
#   2. The merge-pr.sh classifier (_is_head_mismatch_response) and exit-code
#      helper (error_head_moved): extracted and unit-tested directly, the
#      same "extract from source" strategy test-merge-pr-auto-reconcile.sh
#      uses, so the test stays in lockstep with the script. Confirms the
#      classifier fires on the verified GitHub REST / Gitea strings, a
#      best-effort GraphQL pattern, and does NOT fire on the pre-existing,
#      semantically distinct "Base branch was modified" string (that one
#      means "rebase onto base and retry" — conflating the two would either
#      retry forever against a moving target or silently merge a different
#      diff than the one Judge approved).
#   3. Source-wiring: merge-pr.sh threads $MERGE_PRECONDITION_SHA into both
#      merge calls, and both retry loops check the new classifier BEFORE the
#      existing "Base branch was modified" retry branch (ordering matters:
#      the two matchers must stay mutually exclusive, but a future edit that
#      accidentally reorders them could still cause the "Base branch was
#      modified" retry to eat a head-mismatch case first).
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-head-mismatch.sh

# SC2034: FORGE_TYPE (read by the sourced forge_merge_pr/forge_auto_merge) and
# YELLOW (read by error_head_moved, extracted+sourced below) are only
# consumed by code shellcheck can't see is a reader — both look "unused" to
# the linter.
# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"
FORGE_HELPERS_SRC="$HELPERS_DIR/lib/forge-helpers.sh"

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

# ============================================================================
# Part 1: forge_merge_pr / forge_auto_merge thread the expected head SHA
# ============================================================================
echo "Testing forge_merge_pr / forge_auto_merge head-SHA threading..."

# shellcheck source=../lib/forge-helpers.sh
source "$FORGE_HELPERS_SRC"

STUB_DIR="$(mktemp -d)"
GH_ARGS_FILE="$(mktemp)"
trap 'rm -rf "$STUB_DIR"; rm -f "$GH_ARGS_FILE"' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_ARGS_FILE"
if [[ "$1" == "api" && "$2" == "graphql" ]]; then
  echo '{"data":{}}'
  exit 0
fi
# PUT .../merge
echo '{"merged":true}'
exit 0
STUB
chmod +x "$STUB_DIR/gh"

FORGE_TYPE="github"

# --- forge_merge_pr: GitHub REST ---
: > "$GH_ARGS_FILE"
GH_ARGS_FILE="$GH_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_merge_pr "owner/repo" "42" "deadbeef123" >/dev/null
if grep -q -- "-f sha=deadbeef123" "$GH_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_merge_pr (GitHub) passes -f sha=<EXPECTED_HEAD_SHA> when supplied"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_merge_pr (GitHub) did not pass sha= (argv: $(cat "$GH_ARGS_FILE"))"
fi

: > "$GH_ARGS_FILE"
GH_ARGS_FILE="$GH_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_merge_pr "owner/repo" "42" >/dev/null
if grep -q -- "sha=" "$GH_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_merge_pr (GitHub) passed sha= even though no EXPECTED_HEAD_SHA was given"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_merge_pr (GitHub) omits sha= when EXPECTED_HEAD_SHA is not supplied (backward compatible)"
fi

# --- forge_auto_merge: GitHub GraphQL ---
: > "$GH_ARGS_FILE"
GH_ARGS_FILE="$GH_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_auto_merge "owner/repo" "42" "feedface456" >/dev/null
if grep -q -- "expectedHeadOid=feedface456" "$GH_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_auto_merge (GitHub) passes -F expectedHeadOid=<EXPECTED_HEAD_SHA> when supplied"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_auto_merge (GitHub) did not pass expectedHeadOid= (argv: $(cat "$GH_ARGS_FILE"))"
fi

: > "$GH_ARGS_FILE"
GH_ARGS_FILE="$GH_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_auto_merge "owner/repo" "42" >/dev/null
if grep -q -- "expectedHeadOid=" "$GH_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_auto_merge (GitHub) passed expectedHeadOid= even though no EXPECTED_HEAD_SHA was given"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_auto_merge (GitHub) omits expectedHeadOid= when EXPECTED_HEAD_SHA is not supplied"
fi

# --- forge_auto_merge: MERGE_METHOD threading (#7754) ---
: > "$GH_ARGS_FILE"
GH_ARGS_FILE="$GH_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_auto_merge "owner/repo" "42" "" "rebase" >/dev/null
if grep -q -- "mergeMethod=REBASE" "$GH_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_auto_merge (GitHub) uppercases an explicit non-squash MERGE_METHOD for the GraphQL enum"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_auto_merge (GitHub) did not send mergeMethod=REBASE (argv: $(cat "$GH_ARGS_FILE"))"
fi

: > "$GH_ARGS_FILE"
GH_ARGS_FILE="$GH_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_auto_merge "owner/repo" "42" >/dev/null
if grep -q -- "mergeMethod=SQUASH" "$GH_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_auto_merge (GitHub) still defaults to SQUASH when no method is supplied (backward compatible)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_auto_merge (GitHub) default-method behavior regressed (argv: $(cat "$GH_ARGS_FILE"))"
fi

# --- forge_merge_pr / forge_auto_merge: Gitea ---
echo ""
echo "Testing Gitea head_commit_id threading..."

CURL_ARGS_FILE="$(mktemp)"
cat > "$STUB_DIR/curl" <<'SHIM'
#!/usr/bin/env bash
: > "$CURL_ARGS_FILE"
for a in "$@"; do
  printf '%s\n' "$a" >> "$CURL_ARGS_FILE"
done
printf '{"ok":true}\n200\n'
SHIM
chmod +x "$STUB_DIR/curl"

FORGE_TYPE="gitea"
_GITEA_BASE_URL="https://gitea.example.com"
_GITEA_TOKEN="tok-abc"
_GITEA_USERNAME=""

CURL_ARGS_FILE="$CURL_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_merge_pr "owner/repo" "42" "gitea-sha-1" >/dev/null
if grep -q '"head_commit_id":"gitea-sha-1"' "$CURL_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_merge_pr (Gitea) includes head_commit_id in the POST body when supplied"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_merge_pr (Gitea) missing head_commit_id (body: $(tr '\n' ' ' < "$CURL_ARGS_FILE"))"
fi

CURL_ARGS_FILE="$CURL_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_merge_pr "owner/repo" "42" >/dev/null
if grep -q 'head_commit_id' "$CURL_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_merge_pr (Gitea) included head_commit_id even though no EXPECTED_HEAD_SHA was given"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_merge_pr (Gitea) omits head_commit_id when EXPECTED_HEAD_SHA is not supplied"
fi

CURL_ARGS_FILE="$CURL_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_auto_merge "owner/repo" "42" "gitea-sha-2" >/dev/null
if grep -q '"head_commit_id":"gitea-sha-2"' "$CURL_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_auto_merge (Gitea) includes head_commit_id in the POST body when supplied"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_auto_merge (Gitea) missing head_commit_id (body: $(tr '\n' ' ' < "$CURL_ARGS_FILE"))"
fi

CURL_ARGS_FILE="$CURL_ARGS_FILE" PATH="$STUB_DIR:$PATH" \
  forge_auto_merge "owner/repo" "42" "" "merge" >/dev/null
if grep -q '"Do":"merge"' "$CURL_ARGS_FILE"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_auto_merge (Gitea) sends Do:merge when explicitly requested (#7754)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_auto_merge (Gitea) did not send Do:merge (body: $(tr '\n' ' ' < "$CURL_ARGS_FILE"))"
fi

rm -f "$CURL_ARGS_FILE"

# ============================================================================
# Part 2: the merge-pr.sh classifier + exit-code helper, extracted and
# unit-tested directly (same strategy as test-merge-pr-auto-reconcile.sh).
# ============================================================================
echo ""
echo "Testing _is_head_mismatch_response / error_head_moved (extracted)..."

CLASSIFIER_FILE="$(mktemp)"
awk '
  /^error_head_moved\(\) \{/ { capture_error=1; capture_error_open=1 }
  capture_error { print; if (/^}/) capture_error=0 }
  /^_is_head_mismatch_response\(\) \{/ { capture=1 }
  capture { print }
  /^}/ && capture { capture=0 }
' "$MERGE_PR_SRC" > "$CLASSIFIER_FILE"

if ! grep -q '_is_head_mismatch_response()' "$CLASSIFIER_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _is_head_mismatch_response from $MERGE_PR_SRC" >&2
    exit 2
fi
if ! grep -q 'error_head_moved()' "$CLASSIFIER_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract error_head_moved from $MERGE_PR_SRC" >&2
    exit 2
fi
# error_head_moved() references $YELLOW/$NC (merge-pr.sh's own color globals,
# not captured by the extraction above); this test runs under `set -u`, so an
# unset reference would abort the function before reaching its `exit 3` and
# masquerade as a wrong-exit-code failure. $YELLOW is unused by the rest of
# this test file, so define it once, permanently, as an empty string (output
# is discarded at every call site below anyway) — cheaper and less fragile
# than saving/restoring it around each error_head_moved call.
YELLOW=''
# shellcheck disable=SC1090
source "$CLASSIFIER_FILE"

# Positive fixtures: MUST fire.
positive_fixtures=(
    "github_rest:Error: Head branch was modified. Review and try the merge again. (HTTP 409)"
    "gitea:{\"message\":\"head out of date\",\"url\":\"https://gitea.example.com/api/v1/...\"}"
    "graphql_best_effort:could not enable auto-merge: expectedHeadOid does not match current head"
)
for entry in "${positive_fixtures[@]}"; do
    name="${entry%%:*}"
    value="${entry#*:}"
    TESTS_RUN=$((TESTS_RUN + 1))
    if _is_head_mismatch_response "$value"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: _is_head_mismatch_response fires on $name"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: _is_head_mismatch_response missed $name ('$value')"
    fi
done

# Negative fixtures: MUST NOT fire — especially the pre-existing "Base branch
# was modified" string, which triggers the OLD retry-and-update-branch path.
# Conflating the two would either retry forever against a moving head or
# silently merge a diff different from the one Judge approved.
negative_fixtures=(
    "base_modified:Error: Base branch was modified. Review and try the merge again. (HTTP 409)"
    "merge_in_progress:Merge already in progress"
    "clean_status:Pull request Pull request is in clean status (enablePullRequestAutoMerge)"
    "unstable_status:Pull request Pull request is in unstable status (enablePullRequestAutoMerge)"
)
for entry in "${negative_fixtures[@]}"; do
    name="${entry%%:*}"
    value="${entry#*:}"
    TESTS_RUN=$((TESTS_RUN + 1))
    if _is_head_mismatch_response "$value"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: _is_head_mismatch_response false-fired on $name (would misroute the pre-existing retry path)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: _is_head_mismatch_response correctly ignores $name"
    fi
done

# error_head_moved must exit 3 (distinct from error()'s exit 1), per the
# script's own documented exit-code contract. Guarded with `|| head_moved_rc=$?`
# so the nonzero exit doesn't trip this test script's own `set -e`.
head_moved_rc=0
( error_head_moved "test" >/dev/null 2>&1 ) || head_moved_rc=$?
assert_eq "3" "$head_moved_rc" "error_head_moved exits 3 (distinct from error()'s exit 1)"

rm -f "$CLASSIFIER_FILE"

# ============================================================================
# Part 3: source-wiring — merge-pr.sh threads MERGE_PRECONDITION_SHA into
# both merge calls, and both retry loops check the classifier BEFORE the
# pre-existing "Base branch was modified" branch.
# ============================================================================
echo ""
echo "Testing merge-pr.sh source wiring..."

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'forge_merge_pr "\$REPO_NWO" "\$PR_NUMBER" "\$MERGE_PRECONDITION_SHA"' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh threads \$MERGE_PRECONDITION_SHA into the synchronous forge_merge_pr call"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: synchronous forge_merge_pr call does not pass \$MERGE_PRECONDITION_SHA"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'forge_auto_merge "\$REPO_NWO" "\$PR_NUMBER" "\$MERGE_PRECONDITION_SHA"' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh threads \$MERGE_PRECONDITION_SHA into the shell forge_auto_merge call"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: shell forge_auto_merge call does not pass \$MERGE_PRECONDITION_SHA"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'MERGE_PRECONDITION_SHA="\$PR_HEAD_SHA"' "$MERGE_PR_SRC" \
   && grep -q 'forge_get_pr_nocache "\$REPO_NWO" "\$PR_NUMBER" "\$GH"' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: MERGE_PRECONDITION_SHA is derived from a fresh (uncached) PR read, not the cached \$GH read alone"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: could not confirm MERGE_PRECONDITION_SHA's uncached-read derivation"
fi

# Ordering: in BOTH loops, the new classifier must appear BEFORE the existing
# "Base branch was modified" grep, so a future accidental reorder can't let
# the base-modified retry path eat a head-mismatch response first.
sync_loop_order=$(awk '
  /^for MERGE_ATTEMPT in \$\(seq 1 \$MAX_MERGE_RETRIES\); do/ { infor=1 }
  infor && /_is_head_mismatch_response "\$MERGE_RESPONSE"/ { print "mismatch"; exit }
  infor && /grep -q "Base branch was modified"/ { print "base_modified"; exit }
' "$MERGE_PR_SRC")
assert_eq "mismatch" "$sync_loop_order" "synchronous retry loop checks the head-mismatch classifier before 'Base branch was modified'"

auto_loop_order=$(awk '
  /^  for MERGE_ATTEMPT in \$\(seq 1 \$MAX_MERGE_RETRIES\); do/ { infor=1 }
  infor && /_is_head_mismatch_response "\$AUTO_MERGE_OUTPUT"/ { print "mismatch"; exit }
  infor && /grep -q "Base branch was modified"/ { print "base_modified"; exit }
' "$MERGE_PR_SRC")
assert_eq "mismatch" "$auto_loop_order" "auto-merge retry loop checks the head-mismatch classifier before 'Base branch was modified'"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '^#   3 = PR head moved' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh's header 'Exit codes' comment documents exit 3"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: header 'Exit codes' comment does not document exit 3"
fi

# ============================================================================
# Part 4: champion-pr-merge.md wires the exit-3 re-queue outcome distinctly
# from the failure path, and documents the squash-merge detection trap.
# ============================================================================
echo ""
echo "Testing champion-pr-merge.md wiring..."

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (HELPERS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (HELPERS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#447).
if [[ -f "$HELPERS_DIR/../../.claude/commands/loom/champion-pr-merge.md" ]]; then
    CHAMPION_MD="$HELPERS_DIR/../../.claude/commands/loom/champion-pr-merge.md"
else
    CHAMPION_MD="$HELPERS_DIR/../.claude/commands/loom/champion-pr-merge.md"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$CHAMPION_MD" ]] && grep -q 'MERGE_RC' "$CHAMPION_MD" && grep -q '"\$MERGE_RC" -eq 3' "$CHAMPION_MD"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: champion-pr-merge.md Step 3 captures merge-pr.sh's exit code and branches on 3 (re-queue)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: champion-pr-merge.md does not branch on exit code 3"
fi

# The trap note itself moved to the exit-code exceptions reference doc (#8508,
# to keep champion-pr-merge.md inside its markdown-token ratchet), so assert
# what actually matters: the note still exists, and the Champion prompt still
# points at it from the same section. A link with no target, or a target with
# no note, both fail. Probe the installed (.loom/docs) and source
# (defaults/docs) layouts, same two-depth reason as CHAMPION_MD above.
if [[ -f "$HELPERS_DIR/../docs/merge-pr-exit-code-exceptions.md" ]]; then
    EXIT_CODE_DOC="$HELPERS_DIR/../docs/merge-pr-exit-code-exceptions.md"
else
    EXIT_CODE_DOC="$HELPERS_DIR/../../defaults/docs/merge-pr-exit-code-exceptions.md"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$EXIT_CODE_DOC" ]] && grep -qi 'squash-merge detection trap' "$EXIT_CODE_DOC" \
   && [[ -f "$CHAMPION_MD" ]] && grep -q 'merge-pr-exit-code-exceptions.md' "$CHAMPION_MD"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: the squash-merge detection trap is documented and linked from champion-pr-merge.md"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: the squash-merge detection trap note or its link from champion-pr-merge.md is missing"
fi

# #8508's exit 4 shares exit 3's caller contract, so the same wiring must be
# present: Champion has to branch on it instead of falling into the generic
# failure path, and must pass the flag that can produce it in the first place.
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$CHAMPION_MD" ]] && grep -q '"\$MERGE_RC" -eq 4' "$CHAMPION_MD" \
   && grep -q -- '--redate-stale-checks' "$CHAMPION_MD"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: champion-pr-merge.md passes --redate-stale-checks and branches on exit 4 (#8508)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: champion-pr-merge.md does not wire merge-pr.sh's exit 4 (#8508)"
fi

# ============================================================================
# Part 5 (#5589): the native `loom-daemon forge auto-merge` call site passes
# --expected-head-sha and its _AM_RC dispatch routes the new distinct
# head-mismatch exit code (4) to error_head_moved(), not the generic
# failure/retry branch. loom-daemon itself is Rust-tested separately
# (loom-daemon/src/forge_cmd.rs); this only verifies merge-pr.sh's own
# source-level wiring of the exit code it already knows about (matching this
# file's existing "source-wiring" grep strategy, not an executable stub —
# there is no `loom-daemon` binary available in this shell-only test harness).
# ============================================================================
echo ""
echo "Testing native loom-daemon forge auto-merge wiring (#5589)..."

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'loom-daemon forge auto-merge "\$PR_NUMBER" --method "\$REPO_MERGE_METHOD" --expected-head-sha "\$MERGE_PRECONDITION_SHA"' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh passes --expected-head-sha \$MERGE_PRECONDITION_SHA to the native loom-daemon forge auto-merge call"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: native loom-daemon forge auto-merge call does not pass --expected-head-sha \$MERGE_PRECONDITION_SHA"
fi

# The native dispatch's `_AM_RC -eq 4` branch must call error_head_moved
# BEFORE the `_AM_RC -ne 3` (Gitea-decline) check further down, so a 4 never
# falls through and gets misclassified as a generic native failure.
#
# The anchor tolerates the optional `forge_cmd_perm_safe ` prefix the native
# call carries since #6752 (the App-token 403 escalation ladder): the wrapper
# preserves the exit code verbatim, so this ordering contract is unchanged.
native_dispatch_order=$(awk '
  /AUTO_MERGE_OUTPUT=\$\((forge_cmd_perm_safe )?loom-daemon forge auto-merge/ { indispatch=1 }
  indispatch && /_AM_RC -eq 4/ { print "mismatch"; exit }
  indispatch && /_AM_RC -ne 3/ { print "decline_check"; exit }
' "$MERGE_PR_SRC")
assert_eq "mismatch" "$native_dispatch_order" "native _AM_RC dispatch checks the head-mismatch exit code (4) before the Gitea-decline check (-ne 3)"

# ANCHOR UPDATED by #8164 (NOT retired — the property is unchanged and still
# asserted, in two halves instead of one). The branch no longer calls
# error_head_moved() inline; it calls _head_moved_or_resync(), whose only
# non-retry exit IS error_head_moved(). Asserting both halves is strictly
# stronger than the old single grep: the old one could not have noticed a
# wrapper that forgot to exit 3 on the refusal path, and this one does.
TESTS_RUN=$((TESTS_RUN + 1))
if awk '
  /AUTO_MERGE_OUTPUT=\$\((forge_cmd_perm_safe )?loom-daemon forge auto-merge/ { indispatch=1; next }
  indispatch && /_AM_RC -eq 4/ { found=1 }
  found && /_head_moved_or_resync "\$AUTO_MERGE_OUTPUT"/ { print "ok"; exit }
  indispatch && /^    fi$/ { exit }
' "$MERGE_PR_SRC" | grep -q ok; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: the native _AM_RC -eq 4 branch routes through _head_moved_or_resync() (re-queue, not a generic failure)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: could not confirm the native _AM_RC -eq 4 branch routes to the head-moved path"
fi

# The other half of that property: _head_moved_or_resync()'s refusal path is
# still error_head_moved() with both SHAs, i.e. exit 3, i.e. a re-queue.
TESTS_RUN=$((TESTS_RUN + 1))
_hmr_refusal_probe=$(awk '
  /^_head_moved_or_resync\(\) \{/ { infn=1 }
  infn && /error_head_moved "PR #\$PR_NUMBER: \$1" "\$MERGE_PRECONDITION_SHA" "\$_CURRENT_HEAD_SHA"/ { print "ok"; exit }
  infn && /^}$/ { exit }
' "$MERGE_PR_SRC")
if [[ "$_hmr_refusal_probe" == "ok" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: _head_moved_or_resync()'s non-retry path is error_head_moved() with both SHAs (exit 3)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: _head_moved_or_resync() does not end in error_head_moved() — a head move could escape the re-queue path"
fi

# ============================================================================
# Part 6: error_head_moved() includes both stale and current SHA values
# in its diagnostic output (#5714)
# ============================================================================
echo ""
echo "Testing error_head_moved() diagnostic output with SHA values (#5714)..."

# Verify that the error_head_moved function signature now accepts 3 parameters
# and conditionally displays them
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'error_head_moved() {$' "$MERGE_PR_SRC" && \
   grep -A 5 'error_head_moved()' "$MERGE_PR_SRC" | grep -q 'local msg="\$1" stale_sha="\${2:-}" current_sha="\${3:-}"'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: error_head_moved() accepts both stale and current SHA parameters"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: error_head_moved() does not have the correct signature for SHA parameters"
fi

# Verify that error_head_moved includes SHA display logic
TESTS_RUN=$((TESTS_RUN + 1))
if grep -A 15 'error_head_moved()' "$MERGE_PR_SRC" | grep -q 'Merge gated on (stale)'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: error_head_moved() displays stale SHA in diagnostic output"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: error_head_moved() missing stale SHA display"
fi

# Verify that error_head_moved displays current head SHA
TESTS_RUN=$((TESTS_RUN + 1))
if grep -A 15 'error_head_moved()' "$MERGE_PR_SRC" | grep -q 'Current head SHA'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: error_head_moved() displays current head SHA in diagnostic output"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: error_head_moved() missing current head SHA display"
fi

# Verify graceful degradation: error_head_moved still works without SHAs
TESTS_RUN=$((TESTS_RUN + 1))
if grep -A 15 'error_head_moved()' "$MERGE_PR_SRC" | grep -q 'echo -e.*\${YELLOW}.*PR head moved.*\$msg'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: error_head_moved() gracefully degrades when SHAs not provided"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: error_head_moved() does not gracefully degrade"
fi

# Verify that all three call sites in merge-pr.sh fetch the current head SHA
# before calling error_head_moved with both SHAs
TESTS_RUN=$((TESTS_RUN + 1))
call_site_count=$(grep -c 'error_head_moved "PR #\$PR_NUMBER:' "$MERGE_PR_SRC" | grep -c '"\$MERGE_PRECONDITION_SHA" "\$_CURRENT_HEAD_SHA"' || echo 0)
# Instead of the complex grep above, let's verify that _CURRENT_HEAD_SHA is used
if grep -q '_CURRENT_HEAD_SHA=""' "$MERGE_PR_SRC" && \
   grep -q '_CHR_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER"' "$MERGE_PR_SRC" && \
   grep -q 'error_head_moved "PR #\$PR_NUMBER:.*" "\$MERGE_PRECONDITION_SHA" "\$_CURRENT_HEAD_SHA"' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: error_head_moved() call sites fetch and pass current head SHA"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: error_head_moved() call sites do not properly fetch/pass current head SHA"
fi

# Verify that the current head SHA fetch uses forge_get_pr_nocache
# (not cached, to ensure freshness in diagnostics)
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '_CHR_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER"' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: current head SHA fetch uses forge_get_pr_nocache (fresh read)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: current head SHA fetch does not use forge_get_pr_nocache"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
