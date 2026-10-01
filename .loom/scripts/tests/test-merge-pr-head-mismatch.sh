#!/usr/bin/env bash
# test-merge-pr-head-mismatch.sh - Unit tests for the merge head-SHA
# optimistic-concurrency precondition (#5579).
#
# Root cause: neither forge_merge_pr() nor the (since-retired, #8427)
# forge_auto_merge() (lib/forge-helpers.sh) passed the forge's optional
# head-SHA precondition (GitHub REST's `sha` field / GraphQL's `expectedHeadOid` input / Gitea's
# `head_commit_id` field) to the merge API, so Champion (or merge-pr.sh
# directly) could squash-merge a PR whose branch received new commits after
# the approving review — silently stranding those commits (squash-merge makes
# this invisible to an ancestry check afterward).
#
# This suite exercises three layers:
#   1. forge_merge_pr (lib/forge-helpers.sh): the optional 3rd
#      EXPECTED_HEAD_SHA argument is threaded into the right API call shape
#      for both GitHub (REST `sha`) and Gitea (`head_commit_id`), and omitted
#      entirely when not supplied (backward compatibility for any other
#      caller). The shell forge_auto_merge arm this part also used to cover
#      was retired by #8427; the native `loom-daemon forge auto-merge`
#      verb's `expectedHeadOid` threading is covered by forge_cmd.rs's own
#      unit tests.
#   2. The merge-pr.sh classifier (_classify_merge_response) and exit-code
#      helper (error_head_moved): extracted and unit-tested directly, the
#      same "extract from source" strategy test-merge-pr-auto-reconcile.sh
#      uses, so the test stays in lockstep with the script. Confirms the
#      classifier routes the verified GitHub REST / Gitea strings and a
#      best-effort GraphQL pattern to `head-mismatch`, and does NOT route the
#      pre-existing, semantically distinct "Base branch was modified" string
#      there (that one means "rebase onto base and retry" — conflating the two
#      would either retry forever against a moving target or silently merge a
#      different diff than the one Judge approved).
#
#      Since #8191 the classification itself is Rust
#      (loom-daemon/src/merge_pr/response.rs) and `_classify_merge_response` is
#      the shell seam onto it. Every fixture below is UNCHANGED; only the
#      assertion's shape moved, from "the extracted `grep` predicate returns
#      true" to "the extracted shell function, driving the REAL binary, prints
#      the head-mismatch route". That is strictly stronger — it exercises the
#      shipped implementation through its real caller rather than a `grep` copy
#      (defaults/docs/verification-recipes.md §6, "verify the CALL SHAPE") —
#      and it additionally pins the three routes the old boolean could not
#      distinguish at all, since `false` used to mean "405, base-modified, or
#      nothing" indiscriminately.
#   3. Source-wiring: merge-pr.sh threads $MERGE_PRECONDITION_SHA into the
#      merge call, and the head-mismatch route is taken before the
#      base-modified one. Since #8191 that precedence is no longer a property
#      of this file's statement ORDER — it is the classifier's own ordered
#      match — so the `awk` source-order scan that used to assert it is retired
#      here with a named successor; see the `retired()` record in Part 3. Since
#      #8410 there is only ONE merge call — the server-side auto-merge arm,
#      whose precondition expired the moment it was armed, is gone — and
#      `--auto` additionally re-reads the head after its check-settle wait
#      (_revalidate_merge_guards) so a mid-wait force-push re-queues (exit 3)
#      instead of being merged over.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-head-mismatch.sh

# SC2034: FORGE_TYPE (read by the sourced forge_merge_pr) and
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
YELLOW_LABEL='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# An assertion that CANNOT survive the port, retired under the three-part test
# in defaults/docs/verification-recipes.md §6 (the convention #8184 introduced).
# Printed, not deleted: a reader must be able to see what was removed, why it
# can never be true again, and what proves the property now. Counted as run so
# the totals stay honest.
retired() { # <what> <property> <why-structural> <successor>
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${YELLOW_LABEL}RETIRED${NC}: $1"
    echo "      property:   $2"
    echo "      structural: $3"
    echo "      successor:  $4"
}

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
# Part 1: forge_merge_pr threads the expected head SHA
# ============================================================================
echo "Testing forge_merge_pr head-SHA threading..."

# shellcheck source=../lib/forge-helpers.sh
source "$FORGE_HELPERS_SRC"

STUB_DIR="$(mktemp -d)"
GH_ARGS_FILE="$(mktemp)"
# #9548: forge_merge_pr vets its repo through the write scope first. The suite
# runs from a checkout registered as owner/repo (origin, .loom/, push reported
# to the permission probe), so the real decision admits it.
# shellcheck source=lib/write-scope-fixture.sh
source "$SCRIPT_DIR/lib/write-scope-fixture.sh"
write_scope_register "$STUB_DIR/checkout" owner/repo
cd "$STUB_DIR/checkout"
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

# --- forge_merge_pr: Gitea ---
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

rm -f "$CURL_ARGS_FILE"

# ============================================================================
# Part 2: the merge-pr.sh classifier + exit-code helper, extracted and
# unit-tested directly (same strategy as test-merge-pr-auto-reconcile.sh).
# ============================================================================
echo ""
echo "Testing _classify_merge_response / error_head_moved (extracted)..."

# Pin the REAL binary the extracted seam shells out to. Fatal rather than a
# skip, per lib/require-daemon-bin.sh's own rationale: the fixtures below are
# the evidence that the port preserved a deleted `grep` predicate's behaviour,
# and a suite that SKIPped itself would delete that evidence while reporting
# green.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"

CLASSIFIER_FILE="$(mktemp)"
# `_classify_merge_response` is a single dense line (merge-pr.sh is
# ratchet-frozen and `shell-budget --check` refuses any growth of the portable
# pool), so it is captured by `print; next` rather than the brace-matching
# error_head_moved needs — the same split test-merge-pr-head-sync-retry.sh makes
# for _refresh_precondition_sha.
awk '
  /^error_head_moved\(\) \{/ { capture_error=1; capture_error_open=1 }
  capture_error { print; if (/^}/) capture_error=0 }
  /^_classify_merge_response\(\) \{/ { print; next }
' "$MERGE_PR_SRC" > "$CLASSIFIER_FILE"

if ! grep -q '_classify_merge_response()' "$CLASSIFIER_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _classify_merge_response from $MERGE_PR_SRC" >&2
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

# Positive fixtures: MUST route to head-mismatch. Fixtures verbatim from before
# the #8191 port; only the assertion's shape changed (see the header's Part 2).
positive_fixtures=(
    "github_rest:Error: Head branch was modified. Review and try the merge again. (HTTP 409)"
    "gitea:{\"message\":\"head out of date\",\"url\":\"https://gitea.example.com/api/v1/...\"}"
    "graphql_best_effort:could not enable auto-merge: expectedHeadOid does not match current head"
)
for entry in "${positive_fixtures[@]}"; do
    name="${entry%%:*}"
    value="${entry#*:}"
    assert_eq "head-mismatch" "$(_classify_merge_response "$value")" \
      "_classify_merge_response routes $name to head-mismatch"
done

# Negative fixtures: MUST NOT route to head-mismatch — especially the
# pre-existing "Base branch was modified" string, which triggers the
# retry-and-update-branch path. Conflating the two would either retry forever
# against a moving head or silently merge a diff different from the one Judge
# approved.
#
# Each now asserts the EXACT route rather than merely "not head-mismatch". The
# retired boolean could not tell these four apart at all — `false` meant "405,
# base-modified, or no marker" indiscriminately — so this is discriminating
# power the port makes available, not a weakening.
negative_fixtures=(
    "base_modified:base-modified:Error: Base branch was modified. Review and try the merge again. (HTTP 409)"
    "merge_in_progress:merge-in-progress:Merge already in progress"
    "clean_status:other:Pull request Pull request is in clean status (enablePullRequestAutoMerge)"
    "unstable_status:other:Pull request Pull request is in unstable status (enablePullRequestAutoMerge)"
)
for entry in "${negative_fixtures[@]}"; do
    name="${entry%%:*}"
    rest="${entry#*:}"
    want="${rest%%:*}"
    value="${rest#*:}"
    assert_eq "$want" "$(_classify_merge_response "$value")" \
      "_classify_merge_response routes $name to $want, not head-mismatch"
done

# The route the two SHA-shaped arms cannot both take. A response naming BOTH
# branches must go to head-mismatch: routing it to base-modified would answer a
# head that moved past the approved SHA with forge_update_branch and another
# merge attempt. In the shell this was a property of which `grep` appeared first
# in merge-pr.sh; it is now the classifier's own ordered match, and this drives
# that through the real seam rather than reading source text.
assert_eq "head-mismatch" \
  "$(_classify_merge_response "Error: Base branch was modified.
Error: Head branch was modified. (HTTP 409)")" \
  "a response naming BOTH branches routes to head-mismatch (precedence, through the real binary)"
assert_eq "head-mismatch" \
  "$(_classify_merge_response "Error: Head branch was modified. (HTTP 409)
Error: Base branch was modified.")" \
  "...and in the other textual order, so the answer is not an artifact of which marker comes first"

# The fail-CLOSED contract of the seam itself. A binary that cannot answer must
# make `_classify_merge_response` return non-zero, so merge-pr.sh's caller
# refuses and SAYS it was a helper failure — never silently yield the terminal
# `other` route, which would be indistinguishable from "no marker matched".
#
# Exercised at the real `set -euo pipefail` the production script runs under
# (verification-recipes.md §6, "verify the CALL SHAPE"): a unit test of a
# refusal proves the function refuses, not that anything refuses. The hazard is
# specific — `x="$(cmd)"` adopts cmd's status under `set -e`, and the seam is a
# PIPELINE, so pipefail decides whether the `|| _k=""` fallback is even reached.
for shape in "absent:$STUB_DIR/does-not-exist" "stale:$STUB_DIR/stale-daemon"; do
    label="${shape%%:*}"
    binpath="${shape#*:}"
    if [[ "$label" == "stale" ]]; then
        # Knows --version, does not know the subcommand — exactly what clap
        # does on a binary predating the port.
        printf '%s\n' '#!/usr/bin/env bash' \
            '[[ "${1:-}" == "--version" ]] && { echo "loom-daemon 0.19.161"; exit 0; }' \
            'echo "error: unrecognized subcommand" >&2; exit 2' > "$binpath"
        chmod +x "$binpath"
    fi
    rc=0
    out="$(
        set -euo pipefail
        # shellcheck disable=SC1090
        source "$CLASSIFIER_FILE"
        LOOM_DAEMON_BIN="$binpath" _classify_merge_response "Error: Base branch was modified."
    )" || rc=$?
    assert_eq "3" "$rc" "_classify_merge_response fails CLOSED on a $label daemon (rc 3, not a route)"
    assert_eq "" "$out" "_classify_merge_response prints no route on a $label daemon (silence is never 'other')"
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

# #8410 removed the server-side auto-merge arm, so there is no second merge
# call left to thread the precondition into — the synchronous one above is the
# only one. That is strictly stronger than the old "both call sites pass it"
# contract: an armed server-side merge honoured the precondition only at ARM
# time and then merged whatever the head had become (PR #8220: force-push at
# 07:12, server merge at 07:21). Assert the arm stays gone.
TESTS_RUN=$((TESTS_RUN + 1))
if grep -Eq 'forge_auto_merge "\$REPO_NWO"|loom-daemon forge auto-merge' "$MERGE_PR_SRC"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: a server-side auto-merge arm is back — it cannot honour \$MERGE_PRECONDITION_SHA past arm time (#8410)"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no server-side auto-merge arm — forge_merge_pr is the only merge call (#8410)"
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

retired "the awk source-order scan asserting _is_head_mismatch_response appeared before the 'Base branch was modified' grep inside the MERGE_ATTEMPT loop" \
  "A head-SHA-mismatch response must never be routed into the base-modified arm. That arm answers the failure with forge_update_branch and another merge attempt — i.e. it spends an irreversible operation on a head that has moved past the SHA the approving review described (#5579)." \
  "Both greps are gone. There is no longer a pair of 'if' blocks whose relative order decides the route: merge-pr.sh classifies ONCE into a route token, and the precedence lives in a single ordered 'match' in loom-daemon/src/merge_pr/response.rs::classify. The two arms in the loop now test disjoint string values, so reordering them cannot change any outcome — the property this scan protected has become unrepresentable rather than merely guarded. (A source scan could not have asserted it anyway once the matchers left the file.)" \
  "loom-daemon/src/merge_pr/response/tests.rs::head_mismatch_wins_over_base_modified and ::merge_in_progress_wins_over_everything assert the precedence directly, in both textual orders; loom-daemon/tests/merge_pr_response_differential.rs replays every ORDERED PAIR of the five markers under three separators against the frozen retired grep ladder (tests/fixtures/merge-pr-response-retired.sh) and additionally proves, in swapping_the_two_sha_routes_would_be_caught, that its corpus distinguishes the correct ladder from a reordered one. Part 2 of THIS suite drives the same precedence through the real binary via _classify_merge_response. Strictly stronger: the scan checked where a line SAT; these check what the classifier ANSWERS."

# The `--auto` path no longer has a retry loop of its own (#8410); instead it
# detects a head that moved DURING its check-settle wait, proactively, before
# the merge call — a window the armed queue could not see at all. Assert that
# re-validation exists and routes to the same exit-3 re-queue signal.
_reval_body="$(awk '/^_revalidate_merge_guards\(\) \{/{f=1} f; f && /^}/{exit}' "$MERGE_PR_SRC")"
TESTS_RUN=$((TESTS_RUN + 1))
# Here-strings, never pipes: `grep -q` exits on first match and would SIGPIPE
# the producer under `set -o pipefail` (#7771 class).
if grep -qF -- 'fresh_sha" != "$MERGE_PRECONDITION_SHA' <<<"$_reval_body" && \
   grep -qF -- 'error_head_moved' <<<"$_reval_body"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: --auto re-reads the head after its wait and routes a move to error_head_moved (exit 3, #8410)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: --auto must compare the post-wait head against \$MERGE_PRECONDITION_SHA and call error_head_moved"
fi

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
if [[ -f "$EXIT_CODE_DOC" ]] && grep -qi 'merge-ancestry detection trap' "$EXIT_CODE_DOC" \
   && [[ -f "$CHAMPION_MD" ]] && grep -q 'merge-pr-exit-code-exceptions.md' "$CHAMPION_MD"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: the merge-ancestry detection trap is documented and linked from champion-pr-merge.md"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: the merge-ancestry detection trap note or its link from champion-pr-merge.md is missing"
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

# #8896's exit 5 (--auto's settle-wait timed out) joins the same family: it must
# be branched on in Step 3 AND carved out of the "Merge Failed" flow in the
# Error Handling exception, or Champion posts "a human will need to investigate"
# on a PR whose only problem was that CI outran LOOM_AUTO_MERGE_TIMEOUT.
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$CHAMPION_MD" ]] && grep -q '"\$MERGE_RC" -eq 5' "$CHAMPION_MD" \
   && grep -q 'Exception: exit codes 3, 4 and 5' "$CHAMPION_MD"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: champion-pr-merge.md branches on exit 5 and its exception section covers it (#8896)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: champion-pr-merge.md does not wire merge-pr.sh's exit 5 (#8896)"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '^#   5 = --auto' "$MERGE_PR_SRC" \
   && [[ -f "$EXIT_CODE_DOC" ]] && grep -q '^| `5` |' "$EXIT_CODE_DOC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: exit 5 is documented in merge-pr.sh's header table and the exceptions doc (#8896)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: exit 5 is missing from merge-pr.sh's 'Exit codes' comment or the exceptions doc"
fi

# ============================================================================
# Part 5 (#5589, superseded by #8410): the native `loom-daemon forge
# auto-merge` call site used to carry the same --expected-head-sha
# precondition, with exit 4 routed to error_head_moved(). #8410 removed that
# call site with the rest of the server-side arm, because the precondition it
# carried only ever guarded the ARM, never the merge the forge performed
# minutes later. What replaces it is asserted in Part 3: a post-wait head
# re-read inside _revalidate_merge_guards, which guards the moment that
# actually matters. loom-daemon's own forge_cmd.rs tests are unaffected.
# ============================================================================
echo ""
echo "Testing that the native auto-merge call site is gone (#5589 -> #8410)..."

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '_AM_RC' "$MERGE_PR_SRC"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: the native auto-merge dispatch (_AM_RC) is back in merge-pr.sh (#8410)"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no native auto-merge dispatch remains in merge-pr.sh"
fi

# _head_moved_or_resync() (#8164) is still live — not on the retired native
# dispatch site, but on the synchronous-merge path every --auto AND plain
# invocation now shares (#8410 made that path the ONLY merge path). Assert
# its refusal half directly: the non-retry exit is still error_head_moved()
# with both SHAs, i.e. exit 3, i.e. a re-queue, never a silent overwrite.
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
