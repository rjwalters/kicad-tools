#!/usr/bin/env bash
# test-champion-merge-queue-step-no-fallthrough.sh - Regression for #10256 (B3)
# and #10628.
#
# FAILURE MODES:
#  1. (#10256) champion-pr-merge.md Step 3 treated `unrecognized subcommand`
#     from `loom-daemon forge merge-queue step` as permission to run
#     merge-pr.sh. A daemon predating `step` can already be in queue mode, so
#     that bypassed the queue authorization.
#  2. (#10628) The fix for 1 blocked EVERY direct-mode merge on a host whose
#     loom-daemon predates `step` (prompts resync independently of the binary),
#     silently: stderr was discarded and nothing reached the forge.
#
# Contract pinned here: only a DIRECT first stdout line + rc 0 falls through,
# EXCEPT an anchored clap "unrecognized subcommand" error whose direct mode is
# proven (the binary's own `mode` says direct, or it has no `merge-queue` verb
# at all) - logged as LOOM-MERGE-QUEUE-COMPAT. Every other non-queue result is
# a surfaced stall: CHAMPION-MERGE-QUEUE-STALL + one PR notice per head.
# Hermetic: extracts the shipped Step 3 block and runs it with stubs.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
DOC="$REPO_ROOT/defaults/.claude/commands/loom/champion-pr-merge.md"
[[ -f "$DOC" ]] || DOC="$REPO_ROOT/.claude/commands/loom/champion-pr-merge.md"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
# check <desc> <condition...>
check() { local d="$1"; shift; if "$@"; then ok "$d"; else fail "$d"; echo "----- output -----"; echo "$out"; echo "------------------"; fi; }
has()    { [[ "$out" == *"$1"* ]]; }
hasnt()  { [[ "$out" != *"$1"* ]]; }
ncomments() { grep -c 'champion:merge-queue-stall' "$TMP/comments" 2>/dev/null || true; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/.loom/scripts"

# Step 3 block = the bash fence containing the `merge-queue step` call.
awk '/^```bash$/{buf="";inb=1;next} /^```$/{if(inb&&buf~/merge-queue step/){printf "%s",buf;exit} inb=0} inb{buf=buf $0 "\n"}' "$DOC" > "$TMP/step3.sh"
[[ -s "$TMP/step3.sh" ]] || { echo "FAIL: could not extract Step 3 block"; exit 1; }
bash -n "$TMP/step3.sh" || { echo "FAIL: Step 3 block is not valid bash"; exit 1; }

cat > "$TMP/bin/git" <<'S'
#!/usr/bin/env bash
exit 0
S
# gh: head read prints $FAKE_HEAD; `pr comment` appends to $TMP/comments;
# `pr view --json comments` returns what was posted (for the dedup check).
cat > "$TMP/bin/gh" <<S
#!/usr/bin/env bash
C="$TMP/comments"
case "\$*" in
  *"--json headRefOid"*) echo "\${FAKE_HEAD:-deadbeef}" ;;
  *"--json comments"*) cat "\$C" 2>/dev/null ;;
  "pr comment"*) shift 2; while [ \$# -gt 0 ]; do [ "\$1" = --body ] && { printf '%s\n' "\$2" >> "\$C"; shift; }; shift; done ;;
esac
exit 0
S
cat > "$TMP/.loom/scripts/merge-pr.sh" <<'S'
#!/usr/bin/env bash
echo DIRECT_MERGE_CALLED
exit 0
S
chmod +x "$TMP"/bin/* "$TMP/.loom/scripts/merge-pr.sh"

CLAP_STEP=$'error: unrecognized subcommand \'step\'\n\nUsage: loom-daemon forge merge-queue <COMMAND>\n\nFor more information, try \'--help\'.'
CLAP_MQ=$'error: unrecognized subcommand \'merge-queue\'\n\n  tip: a similar subcommand exists: \'merge-method\'\n\nUsage: loom-daemon forge <COMMAND>'

# run_case <step stdout> <step stderr> <step rc> [<mode stdout> <mode rc>]
#   -> $out = the block's combined output. Comments persist across cases
#   unless reset_comments is called.
run_case() {
  printf '%s' "$1" > "$TMP/step.out"; printf '%s' "$2" > "$TMP/step.err"
  printf '%s' "${4-}" > "$TMP/mode.out"
  cat > "$TMP/bin/loom-daemon" <<S
#!/usr/bin/env bash
case "\$*" in
  "forge merge-queue step "*) cat "$TMP/step.out"; cat "$TMP/step.err" >&2; exit $3 ;;
  "forge merge-queue mode") cat "$TMP/mode.out"; exit ${5:-0} ;;
esac
echo "unexpected loom-daemon \$*" >&2; exit 99
S
  chmod +x "$TMP/bin/loom-daemon"
  out=$( cd "$TMP" && PATH="$TMP/bin:$PATH" bash "$TMP/step3.sh" 42 2>&1 )
}
reset_comments() { rm -f "$TMP/comments"; }

echo "--- direct and queue-protocol outcomes ---"
reset_comments
run_case $'LOOM-MERGE-QUEUE-DIRECT\n' '' 0
check "DIRECT sentinel, rc 0 -> direct merge runs, nothing surfaced" \
  eval 'has DIRECT_MERGE_CALLED && hasnt STALL && hasnt COMPAT && [[ $(ncomments) -eq 0 ]]'

run_case $'LOOM-MERGE-QUEUE-QUEUED pr=42 head=deadbeef\n' '' 7
check "QUEUED -> no direct merge, silent (no stall, no notice), outcome rc=7" \
  eval 'hasnt DIRECT_MERGE_CALLED && hasnt STALL && [[ $(ncomments) -eq 0 ]] && has "CHAMPION-MERGE-OUTCOME pr=42 rc=7"'

run_case $'LOOM-MERGE-QUEUE-HANDED-OFF pr=42 head=deadbeef\n' '' 7
check "HANDED-OFF -> no direct merge, silent" \
  eval 'hasnt DIRECT_MERGE_CALLED && hasnt STALL && [[ $(ncomments) -eq 0 ]]'

run_case $'LOOM-MERGE-QUEUE-MERGED pr=42 recorded=true after_revocation=false\n' '' 0
check "MERGED (by the queue) -> merge-pr.sh not run, silent" \
  eval 'hasnt DIRECT_MERGE_CALLED && hasnt STALL && [[ $(ncomments) -eq 0 ]]'

echo "--- old daemon without \`step\` (#10628) ---"
run_case '' "$CLAP_STEP" 2 'merge-queue: mode=direct source=default execution=dormant' 0
check "no 'step', own mode says direct -> logged compat direct merge" \
  eval 'has DIRECT_MERGE_CALLED && has "LOOM-MERGE-QUEUE-COMPAT pr=42" && has "unrecognized subcommand '"'"'step'"'"'" && has "CHAMPION-MERGE-OUTCOME pr=42 rc=0" && hasnt STALL'

run_case '' "$CLAP_STEP" 2 'merge-queue: mode=queue source=config execution=dormant' 0
check "no 'step', mode says queue -> NO direct merge (Judge P1), stall surfaced" \
  eval 'hasnt DIRECT_MERGE_CALLED && hasnt COMPAT && has "CHAMPION-MERGE-QUEUE-STALL pr=42" && has "unrecognized subcommand '"'"'step'"'"'" && has "rc=2" && [[ $(ncomments) -eq 1 ]]'

reset_comments
run_case '' "$CLAP_STEP" 2 '' 2
check "no 'step', mode fails (invalid mode) -> no direct merge, stall surfaced" \
  eval 'hasnt DIRECT_MERGE_CALLED && has CHAMPION-MERGE-QUEUE-STALL && [[ $(ncomments) -eq 1 ]]'

reset_comments
run_case '' "$CLAP_STEP" 2 'merge-queue: mode=direct source=default execution=dormant' 1
check "no 'step', mode prints direct but rc!=0 -> no direct merge" \
  eval 'hasnt DIRECT_MERGE_CALLED && has CHAMPION-MERGE-QUEUE-STALL'

run_case '' "$CLAP_MQ" 2
check "binary predates merge modes (no 'merge-queue') -> logged compat direct merge" \
  eval 'has DIRECT_MERGE_CALLED && has "LOOM-MERGE-QUEUE-COMPAT pr=42" && has "unrecognized subcommand '"'"'merge-queue'"'"'"'

echo "--- the compat path is anchored ---"
reset_comments
run_case '' "warning: x"$'\n'"$CLAP_STEP" 2 'merge-queue: mode=direct source=default execution=dormant' 0
check "unrecognized-subcommand not on stderr's FIRST line -> no compat, no merge" \
  eval 'hasnt DIRECT_MERGE_CALLED && hasnt COMPAT && has CHAMPION-MERGE-QUEUE-STALL'

run_case $'LOOM-MERGE-QUEUE-QUEUED pr=42 head=deadbeef\n' "$CLAP_STEP" 2 'merge-queue: mode=direct source=default execution=dormant' 0
check "unrecognized-subcommand with non-empty stdout -> no compat, no merge" \
  eval 'hasnt DIRECT_MERGE_CALLED && hasnt COMPAT'

run_case '' "$CLAP_STEP" 1 'merge-queue: mode=direct source=default execution=dormant' 0
check "unrecognized-subcommand with rc!=2 -> no compat, no merge" \
  eval 'hasnt DIRECT_MERGE_CALLED && hasnt COMPAT'

echo "--- other fail-closed results are surfaced, once per head ---"
reset_comments
run_case '' 'error: LOOM-MERGE-QUEUE-DIRECT' 0
check "sentinel only on stderr -> no direct merge, stall" \
  eval 'hasnt DIRECT_MERGE_CALLED && has CHAMPION-MERGE-QUEUE-STALL'

run_case $'LOOM-MERGE-QUEUE-DIRECT\n' '' 2
check "DIRECT with nonzero rc -> no direct merge, stall" \
  eval 'hasnt DIRECT_MERGE_CALLED && has CHAMPION-MERGE-QUEUE-STALL'

run_case '' '' 0
check "empty output -> no direct merge, stall" \
  eval 'hasnt DIRECT_MERGE_CALLED && has CHAMPION-MERGE-QUEUE-STALL'

run_case '' 'bash: loom-daemon: command not found' 127
check "loom-daemon missing (rc 127) -> no direct merge, stall names it" \
  eval 'hasnt DIRECT_MERGE_CALLED && has "command not found (rc=127)"'

reset_comments
UND=$'LOOM-MERGE-QUEUE-UNDETERMINED\nmerge-queue: forge read failed — not merging or enqueuing on this pass (fail closed)\n'
run_case "$UND" '' 3
check "UNDETERMINED -> no merge; stall line carries stdout reason + rc; one notice" \
  eval 'hasnt DIRECT_MERGE_CALLED && has "STALL pr=42: LOOM-MERGE-QUEUE-UNDETERMINED merge-queue: forge read failed" && has "(rc=3)" && [[ $(ncomments) -eq 1 ]] && grep -q "sha=deadbeef" "$TMP/comments" && has "CHAMPION-MERGE-OUTCOME pr=42 rc=7"'

run_case "$UND" '' 3
check "same head again -> no second notice (dedup on head-keyed marker)" \
  eval '[[ $(ncomments) -eq 1 ]] && has CHAMPION-MERGE-QUEUE-STALL'

export FAKE_HEAD=cafef00d
run_case "$UND" '' 3
check "new head -> a fresh notice" \
  eval '[[ $(ncomments) -eq 2 ]] && grep -q "sha=cafef00d" "$TMP/comments"'
unset FAKE_HEAD

echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
