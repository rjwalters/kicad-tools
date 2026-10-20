#!/usr/bin/env bash
# test-champion-markerless-park.sh - #11932: a `loom:pr` + `loom:blocked` PR
# with no Champion hold marker (another actor's park) gets no merge attempt and
# no comment, and the "Merge Failed" comment posts at most once per head.
# #11970: a hold the fleet re-labelled `loom:operator` (`state=stray-operator`)
# loses that label and nothing else.
#
# Extract-and-execute (#7979): the SHIPPED bash blocks are lifted out of
# champion-pr-merge.md and run with `gh`, `loom-daemon`, `$GH_READ`,
# `merge-pr.sh` and `post-comment.sh` stubbed. The classification itself
# (marker trust, foreign park vs. hold vs. contradiction) is Rust, pinned by
# `champion_hold::classify`'s unit tests; here the stub answers what the verb
# would print, and the test pins what each block DOES with that answer:
#   1. Verdict-State Janitor Part 1;
#   2. Step 3 (the merge);
#   3. Error Handling's "Merge Failed" comment, run on repeated passes.
#
# Hermetic: no network, no forge, no tokens.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-markerless-park.sh

# SC2016: check conditions are eval'd later, single-quoted on purpose. SC2034:
# PR_NUMBER / GH_READ are read by the eval'd shipped blocks. SC2043:
# the one-pass loop gives the janitor's `continue` a loop.
# shellcheck disable=SC2016,SC2034,SC2043

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
# Installed (.loom/scripts) vs. source (defaults/scripts) layouts (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"

TESTS_RUN=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }
check() { local d="$1"; shift; if "$@"; then pass "$d"; else fail "$d"; echo "    output: ${out:-}"; fi; }
has() { [[ "$out" == *"$1"* ]]; }
hasnt() { [[ "$out" != *"$1"* ]]; }
log_count() { grep -c -- "$1" "$STUB_LOG" 2>/dev/null || true; }

# Print the first ```bash block after the first line containing $1.
extract_block() {
    awk -v m="$1" '
        f == 0 && index($0, m) { f = 1; next }
        f == 1 && /^```bash$/  { f = 2; next }
        f == 2 && /^```$/      { exit }
        f == 2                 { print }
    ' "$CHAMPION_MD" | grep -v '^PR_NUMBER=<number>$'
}

JANITOR="$(extract_block '**Verification / resolution command**')"
FAILED_NOTICE="$(extract_block 'The comment, marker-guarded')"
STEP3="$(awk '/^```bash$/{buf="";inb=1;next} /^```$/{if(inb&&buf~/merge-queue step/){printf "%s",buf;exit} inb=0} inb{buf=buf $0 "\n"}' "$CHAMPION_MD")"
for b in JANITOR FAILED_NOTICE STEP3; do
    [[ -n "${!b}" ]] || { echo "FAIL: could not extract the $b block"; exit 1; }
    bash -n <<<"${!b}" || { echo "FAIL: the $b block is not valid bash"; exit 1; }
done

WORK="$(mktemp -d)" && trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin" "$WORK/.loom/scripts" "$WORK/gitdir"
export STUB_LOG="$WORK/log"

# ---- stubs ------------------------------------------------------------------
# FIX_HC        the `champion-hold classify` line; FIX_HC_RC its exit code
# FIX_HEAD      the PR head SHA
# The merge-failed-marker stub models the forge: the notice for a head is due
# until a POST carrying that head's marker is in the log.
cat >"$WORK/bin/loom-daemon" <<'S'
#!/usr/bin/env bash
case "$1 $2" in
  "champion-hold classify")
    echo "CLASSIFY $*" >>"$STUB_LOG"
    [ "${FIX_HC_RC:-0}" -eq 0 ] || exit "$FIX_HC_RC"
    printf '%s\n' "$FIX_HC" ;;
  "champion-hold merge-failed-marker")
    [ "${FIX_MF_RC:-0}" -eq 0 ] || exit "$FIX_MF_RC"
    head=""; while [ $# -gt 0 ]; do [ "$1" = --head ] && head="$2"; shift; done
    m="<!-- champion:merge-failed head=$head -->"
    grep -qF -- "POST-BODY $m" "$STUB_LOG" 2>/dev/null && exit 1
    printf '%s\n' "$m" ;;
  "forge merge-queue") echo "QUEUE $*" >>"$STUB_LOG"; echo LOOM-MERGE-QUEUE-DIRECT ;;
  *) echo "unexpected loom-daemon: $*" >&2; exit 99 ;;
esac
S
cat >"$WORK/bin/gh" <<'S'
#!/usr/bin/env bash
case "$1 $2" in
  "pr view") printf '%s\n' "${FIX_HEAD:-1111111111111111111111111111111111111111}" ;;
  "pr edit") echo "EDIT $*" >>"$STUB_LOG" ;;
  *) echo "unexpected gh: $*" >&2; exit 99 ;;
esac
S
cat >"$WORK/bin/git" <<S
#!/usr/bin/env bash
[ "\$*" = "rev-parse --git-common-dir" ] && echo "$WORK/gitdir"
exit 0
S
cat >"$WORK/bin/gh_read" <<'S'
#!/usr/bin/env bash
case "$1 $2" in
  "pr view") echo false ;;
  "--clear-cache ") ;;
  *) echo "unexpected GH_READ: $*" >&2; exit 99 ;;
esac
S
cat >"$WORK/.loom/scripts/post-comment.sh" <<'S'
#!/usr/bin/env bash
echo "POST $1" >>"$STUB_LOG"
while [ $# -gt 0 ]; do [ "$1" = --body ] && printf 'POST-BODY %s\n' "$(tr '\n' ' ' <<<"$2")" >>"$STUB_LOG"; shift; done
S
cat >"$WORK/.loom/scripts/merge-pr.sh" <<'S'
#!/usr/bin/env bash
echo "MERGE $*" >>"$STUB_LOG"; exit "${MERGE_STUB_RC:-0}"
S
chmod +x "$WORK"/bin/* "$WORK/.loom/scripts/"*.sh

reset() { : >"$STUB_LOG"; }
line() { echo "LOOM-CHAMPION-HOLD state=$1 pr=42 detail=${2:--}"; }

run_janitor() { # <classify line> [rc]
    out=$(cd "$WORK" && FIX_HC="$1" FIX_HC_RC="${2:-0}" PATH="$WORK/bin:$PATH" \
        PR_NUMBER=42 GH_READ=gh_read bash -c "for _ in 1; do
$JANITOR
echo JANITOR-FELL-THROUGH; done" 2>&1)
}
run_step3() { # <classify line> [rc]
    out=$(cd "$WORK" && FIX_HC="$1" FIX_HC_RC="${2:-0}" PATH="$WORK/bin:$PATH" \
        env -u EXPECTED_HEAD bash -c "$STEP3" step3 42 2>&1)
}
run_notice() { # <head> [merge-failed-marker rc]
    out=$(cd "$WORK" && FIX_HEAD="$1" FIX_MF_RC="${2:-0}" PATH="$WORK/bin:$PATH" \
        PR_NUMBER=42 bash -c "$FAILED_NOTICE" 2>&1)
}

# ---------------------------------------------------------------------------
echo "--- 1. Janitor Part 1 ---"
reset; run_janitor "$(line foreign-park)"
check "markerless loom:pr + loom:blocked: skipped (continue), not fallen through" \
    eval 'has "SKIP #42" && hasnt JANITOR-FELL-THROUGH'
check "markerless pair: no comment, no label change" eval '[[ $(log_count POST) == 0 && $(log_count EDIT) == 0 ]]'

reset; run_janitor "" 3
check "an unreadable classification (exit 3) skips too (never read as clear)" \
    eval 'has "SKIP #42" && hasnt JANITOR-FELL-THROUGH && [[ $(log_count POST) == 0 ]]'

reset; run_janitor "" 2
check "a loom-daemon older than the verb (clap exit 2): on to the criteria" \
    eval 'has JANITOR-FELL-THROUGH && [[ $(log_count POST) == 0 && $(log_count EDIT) == 0 ]]'

reset; run_janitor "$(line champion-hold merge-risk)"
check "Champion's own hold: falls through to the criteria, untouched" \
    eval 'has JANITOR-FELL-THROUGH && [[ $(log_count POST) == 0 && $(log_count EDIT) == 0 ]]'

reset; run_janitor "$(line stray-operator critical-file)"
check "stray-operator: removes only loom:operator, no comment, on to the criteria" \
    eval 'has JANITOR-FELL-THROUGH && grep -q "EDIT pr edit 42 --remove-label loom:operator" "$STUB_LOG" && [[ $(log_count EDIT) == 1 && $(log_count POST) == 0 ]]'

reset; run_janitor "$(line clear)"
check "clear: falls through to the criteria" eval 'has JANITOR-FELL-THROUGH && [[ $(log_count EDIT) == 0 ]]'

reset; run_janitor "$(line contradiction loom:changes-requested)"
check "contradiction: the janitor notice names the verdict label" \
    eval 'grep -q "POST-BODY .*both \`loom:pr\` and \`loom:changes-requested\`" "$STUB_LOG"'
check "contradiction: loom:pr is stripped" eval 'grep -q "EDIT pr edit 42 --remove-label loom:pr" "$STUB_LOG"'

# ---------------------------------------------------------------------------
echo "--- 2. Step 3 (merge) ---"
for state in foreign-park champion-hold contradiction stray-operator; do
    reset; run_step3 "$(line "$state")"
    check "$state: no merge attempt (no merge-pr.sh, no merge-queue step)" \
        eval '[[ $(log_count MERGE) == 0 && $(log_count QUEUE) == 0 ]]'
    check "$state: exit 10 re-queues silently, never 'Merge failed'" \
        eval 'has "CHAMPION-HOLD-SKIP pr=42" && has "CHAMPION-MERGE-OUTCOME pr=42 rc=10" && hasnt "Merge failed"'
done
reset; run_step3 "" 3
check "classify unreadable (exit 3): no merge attempt" eval '[[ $(log_count MERGE) == 0 ]] && has "rc=10"'
reset; run_step3 "" 2
check "a loom-daemon older than the verb: today's merge (merge-pr.sh's label guard decides)" \
    eval '[[ $(log_count MERGE) == 1 ]] && hasnt CHAMPION-HOLD-SKIP'
reset; run_step3 "$(line clear)"
check "clear: merges" eval '[[ $(log_count MERGE) == 1 ]] && has "CHAMPION-MERGE-OUTCOME pr=42 rc=0"'
reset; MERGE_STUB_RC=1 run_step3 "$(line clear)"
check "clear + merge-pr.sh exit 1: still a failure" eval 'has "Merge failed for PR #42"'

# ---------------------------------------------------------------------------
echo "--- 3. 'Merge Failed' once per head ---"
A=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
B=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
reset
run_notice "$A"; run_notice "$A"; run_notice "$A"
check "three failing passes on one head post one comment" eval '[[ $(log_count "^POST 42") == 1 ]]'
check "the comment opens with the head-keyed marker" \
    eval 'grep -qF "POST-BODY <!-- champion:merge-failed head=$A -->" "$STUB_LOG"'
run_notice "$B"
check "a new head may post again" eval '[[ $(log_count "^POST 42") == 2 ]]'
reset; run_notice "$A" 3
check "an unreadable comment stream posts nothing" eval '[[ $(log_count POST) == 0 ]]'

echo
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
